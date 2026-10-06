"""Bounded newline JSON-RPC stdio transport. Tests substitute a local process for Docker."""

import asyncio
import hashlib
import json
import queue
import re
import subprocess
import threading
from pathlib import Path

from jsonschema import Draft202012Validator, SchemaError

from harness.modules import ToolError, run_cmd

MAX_MESSAGE = 1_000_000
MAX_TOOLS = 128
RPC_TIMEOUT = 30


def container_name(sid, server):
    token = hashlib.sha256(sid.encode()).hexdigest()[:24]
    return f"harness-mcp-{token}-{server['name']}"


def container_argv(session, server, cfg):
    args = ["docker", "run", "--rm", "-i", "--init", "--pull=never", "--name",
            container_name(session["id"], server), "--label", f"agent-harness.session={session['id']}",
            "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", cfg.memory, "--cpus", str(cfg.cpus), "--pids-limit", str(cfg.pids)]
    mount = server.get("mount_workspace", False)
    if mount:
        workspace = str(Path(session["workspace"]).resolve())
        if "," in workspace:
            raise ToolError("MCP workspace path cannot contain a comma")
        args += ["--mount", f"type=bind,source={workspace},target=/workspace" + (",readonly" if mount == "ro" else ""),
                 "--workdir", "/workspace"]
    for key in server.get("env", {}):
        args += ["--env", key]  # Docker reads the value from its process environment; secrets never enter argv.
    return args + [server["image"], *server["command"]]


class StdioClient:
    def __init__(self, argv, secrets=None):
        import os
        # Do not pass the daemon's credentials to Docker or the server.
        env = {k: v for k, v in os.environ.items() if k.upper() in
               {"PATH", "SYSTEMROOT", "TEMP", "TMP", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}}
        env.update(secrets or {})
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.messages = queue.Queue(maxsize=16)
        self.lock = threading.Lock()
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                line = self.proc.stdout.readline(MAX_MESSAGE + 1)
                if not line or len(line) > MAX_MESSAGE:
                    raise ValueError("MCP server closed stdout or exceeded the message limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("MCP response must be an object")
                self.messages.put_nowait(message)
        except (ValueError, OSError, queue.Full) as exc:
            self.proc.kill()
            try:
                self.messages.put_nowait(ToolError(str(exc)))
            except queue.Full:
                pass

    def _send(self, message):
        data = (json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode()
        if len(data) > MAX_MESSAGE:
            raise ToolError("MCP request exceeded the message limit")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise ToolError("MCP server stdin is closed") from None

    def notify(self, method):
        self._send({"method": method})

    def request(self, method, params):
        # Covers blocked stdin writes as well as servers that never send a response.
        timer = threading.Timer(RPC_TIMEOUT, self._kill)
        timer.daemon = True
        timer.start()
        try:
            return self._request(method, params)
        finally:
            timer.cancel()

    def _kill(self):
        if self.proc.poll() is None:
            self.proc.kill()

    def _request(self, method, params):
        with self.lock:
            self.counter += 1
            self._send({"id": self.counter, "method": method, "params": params})
            # Notifications are ignored; server-initiated requests get a refusal (no sampling, roots or elicitation).
            import time
            deadline = time.monotonic() + RPC_TIMEOUT
            while True:
                try:
                    message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    raise ToolError("MCP server response timed out") from None
                if isinstance(message, Exception):
                    raise message
                if self._server_message(message):
                    continue
                return self._result(message)

    def _server_message(self, message):
        if "method" not in message:
            return False
        if "id" in message:
            self._send({"id": message["id"], "error": {"code": -32601, "message": "unsupported"}})
        return True

    def _result(self, message):
        if message.get("id") != self.counter or message.get("jsonrpc") != "2.0":
            raise ToolError("MCP response id or JSON-RPC version mismatch")
        if "error" in message:
            raise ToolError("MCP server returned a protocol error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ToolError("MCP result must be an object")
        return result

    def close(self):
        self._kill()
        self.proc.wait(timeout=10)
        self.reader.join(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()


class SessionTools:
    def __init__(self, session, servers, cfg):
        self.session, self.servers, self.cfg = session, servers, cfg
        self.clients = []
        self.tools = {}

    @property
    def tool_names(self):
        return tuple(self.tools)

    async def start(self):
        for server in self.servers:
            secrets = {key: Path(value["secret_file"]).read_text(encoding="utf-8").strip()
                       for key, value in server.get("env", {}).items()}
            # A daemon crash may have left this session's old sidecar running.
            await run_cmd(["docker", "rm", "-f", container_name(self.session["id"], server)], timeout=30)
            client = StdioClient(container_argv(self.session, server, self.cfg), secrets)
            self.clients.append((server, client))
            initialized = await asyncio.to_thread(client.request, "initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "agent-harness", "version": "1"}})
            if initialized.get("protocolVersion") != "2024-11-05":
                raise ToolError("unsupported MCP protocol version")
            client.notify("notifications/initialized")
            await self._list_tools(server, client)

    async def _list_tools(self, server, client):
        cursor = None
        seen_cursors = set()
        while True:
            page = await asyncio.to_thread(client.request, "tools/list", {"cursor": cursor} if cursor else {})
            tools = page.get("tools")
            if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
                raise ToolError("MCP tools must be a list of objects")
            for tool in tools:
                self._add_tool(server, client, tool)
            cursor = page.get("nextCursor")
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen_cursors or len(seen_cursors) >= MAX_TOOLS:
                raise ToolError("invalid MCP tools pagination")
            seen_cursors.add(cursor)

    def _add_tool(self, server, client, tool):
        name = tool.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ToolError("invalid MCP tool name")
        full = f"mcp__{server['name']}__{name}"
        schema = tool.get("inputSchema")
        if full in self.tools or len(self.tools) >= MAX_TOOLS or not isinstance(schema, dict):
            raise ToolError("duplicate MCP tool, invalid schema or too many tools")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise ToolError("invalid MCP input schema") from None
        self.tools[full] = (client, name, {"type": "function", "function": {
            "name": full, "description": str(tool.get("description", "")), "parameters": schema}})

    def schemas(self):
        return [value[2] for value in self.tools.values()]

    async def call(self, name, args):
        if name not in self.tools:
            raise ToolError("unknown MCP tool")
        client, bare, _ = self.tools[name]
        result = await asyncio.to_thread(client.request, "tools/call", {"name": bare, "arguments": args})
        if result.get("isError"):
            raise ToolError("MCP tool returned an error")
        return json.dumps(result, ensure_ascii=False)

    async def close(self):
        clients, self.clients = self.clients, []
        self.tools.clear()
        for server, client in clients:
            try:
                await run_cmd(["docker", "rm", "-f", container_name(self.session["id"], server)], timeout=30)
            finally:
                await asyncio.to_thread(client.close)
