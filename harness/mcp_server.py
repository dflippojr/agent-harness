"""Daemon-side MCP server for hosted Claude Code (#300) and Codex (#373) sessions.

Network path: a small relay sidecar per session joins the provider's internal ``harness-cli-<backend>`` network and
listens on its own loopback. The CLI container joins the relay's network namespace (``--network
container:<relay>``), so the endpoint is reachable from that one container and nothing else: not from other
sessions on the same network, nor from any other container. The relay hands every HTTP request to the daemon over
its stdio pipe, so the daemon never listens on a port a container can reach, and the egress allowlists don't change.

Authentication: every request carries a token minted for one session at start, held only in this process's memory,
and revoked when the session's CLI stops. It reaches the CLI by environment-variable name only, so it never appears
in a command line, transcript, event or log.

Authorization is not decided here. The CLI asks the daemon before every call (Claude Code through ``can_use_tool``,
Codex through ``mcpServer/elicitation/request``), which takes the ordinary Policy and approval path; the runner
records a one-use grant for each call it allows, and the endpoint refuses a call without one. Results reach the
transcript through the CLI's own tool-result records, like any other tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import subprocess
import threading
from pathlib import Path
from typing import Awaitable, Callable

from .policy import MCP_SERVER
from .sandbox import run_cmd

log = logging.getLogger(__name__)

RELAY_PORT = 8790
TOKEN_ENV = "HARNESS_MCP_TOKEN"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
RELAY_SCRIPT = Path(__file__).with_name("mcp_relay.js")
# The CLIs that get the endpoint (#300 Claude Code, #373 Codex). Cursor has no host approval to hang a grant on.
MCP_BACKENDS = ("claude", "codex")
# The shell and file tools a split-mode session (#427) gets from the harness server in place of its CLI's own; they are
# the native loop's tools of these names, so policy rules, approvals and network grants apply to them unchanged.
SPLIT_TOOLS = ("run_shell", "read_file", "write_file", "edit_file", "search", "list_files")
# The id of the call the CLI is making, in the tools/call _meta; grants are matched on it when present. Claude Code
# sends its tool_use id, Codex its call id (the mcpToolCall item id the approval was asked under).
TOOL_USE_META = "claudecode/toolUseId"
CODEX_CALL_META = "callId"


class McpRelayError(Exception):
    """The relay sidecar didn't start or stopped answering."""


def mcp_config() -> str:
    """The --mcp-config JSON for Claude Code. The token is expanded from the container's environment by Claude Code."""
    return json.dumps({"mcpServers": {MCP_SERVER: {
        "type": "http", "url": f"http://127.0.0.1:{RELAY_PORT}/mcp",
        "headers": {"Authorization": f"Bearer ${{{TOKEN_ENV}}}"}}}})


def codex_mcp_overrides() -> list[str]:
    """The `-c` arguments that register the harness server with Codex for this process only (CODEX_HOME's files
    don't change). Codex reads the bearer token from the variable it names; "prompt" makes Codex ask the client
    (mcpServer/elicitation/request) before every call, whatever the tool's annotations say."""
    server = (f'{{url="http://127.0.0.1:{RELAY_PORT}/mcp", bearer_token_env_var="{TOKEN_ENV}", '
              'default_tools_approval_mode="prompt", startup_timeout_sec=30}')
    return ["-c", f"mcp_servers.{MCP_SERVER}={server}"]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class McpTokens:
    """Per-session bearer tokens, in memory only. Stored by digest, so a lookup doesn't compare secrets char by char."""

    def __init__(self):
        self._sessions: dict[str, str] = {}   # digest -> session id
        self._digests: dict[str, str] = {}    # session id -> digest

    def mint(self, sid: str) -> str:
        """A fresh token for this session, replacing (and so revoking) any earlier one."""
        self.revoke(sid)
        token = secrets.token_urlsafe(32)
        digest = _digest(token)
        self._sessions[digest] = sid
        self._digests[sid] = digest
        return token

    def session_for(self, token: str) -> str | None:
        return self._sessions.get(_digest(token)) if token else None

    def revoke(self, sid: str) -> None:
        digest = self._digests.pop(sid, None)
        if digest is not None:
            self._sessions.pop(digest, None)


def mcp_tool(schema: dict) -> dict:
    """An OpenAI-style function schema as an MCP tool."""
    fn = schema.get("function") or {}
    return {"name": fn.get("name", ""), "description": fn.get("description", ""),
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}}}


def _reply(status: int, payload: dict | list | None = None) -> dict:
    if payload is None:
        return {"status": status, "headers": {}, "body": ""}
    return {"status": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False)}


def _error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class McpServer:
    """Streamable-HTTP MCP (JSON responses, no server-initiated stream) over requests handed in by a relay."""

    def __init__(self, tokens: McpTokens, tools: Callable[[str], list[dict]],
                 call: Callable[[str, str, dict, str], Awaitable[tuple[str, bool]]], version: str = "1"):
        self.tokens = tokens
        self._tools = tools     # session id -> function schemas this session may call over MCP
        self._call = call       # (session id, tool, args, tool_use_id) -> (text, ok)
        self.version = version

    async def handle(self, sid: str, request: dict) -> dict:
        """One HTTP request from the relay bound to session ``sid``: {status, headers, body}."""
        headers = {str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()}
        if str(request.get("path") or "").split("?", 1)[0] != "/mcp":
            return _reply(404, {"error": "not found"})
        if headers.get("origin"):  # nothing legitimate here is a browser; refuse DNS-rebinding style requests
            return _reply(403, {"error": "origin not allowed"})
        scheme, _, token = headers.get("authorization", "").partition(" ")
        owner = self.tokens.session_for(token.strip()) if scheme.lower() == "bearer" else None
        if owner is None:
            return _reply(401, {"error": "invalid or revoked token"})
        if owner != sid:
            return _reply(403, {"error": "token belongs to another session"})
        if request.get("method") != "POST":
            return _reply(405, {"error": "only POST is supported"})
        try:
            message = json.loads(request.get("body") or "")
        except ValueError:
            return _reply(400, _error(None, -32700, "parse error"))
        if isinstance(message, list):
            replies = [r for r in [await self._rpc(sid, m) for m in message] if r is not None]
            return _reply(200, replies) if replies else _reply(202)
        reply = await self._rpc(sid, message)
        return _reply(200, reply) if reply is not None else _reply(202)

    async def _rpc(self, sid: str, message) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        if "method" not in message or "id" not in message:
            return None  # a notification, or a response to a request we never send
        id_, method = message["id"], message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_SERVER, "version": self.version},
                "instructions": "Agent-harness tools. Each call goes through the harness approval policy."}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": id_, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": id_, "result": {"tools": [mcp_tool(t) for t in self._tools(sid)]}}
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": id_, "result": await self._tools_call(sid, params)}
        return _error(id_, -32601, f"method not found: {method}")

    async def _tools_call(self, sid: str, params: dict) -> dict:
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        if name not in {(t.get("function") or {}).get("name") for t in self._tools(sid)}:
            text, ok = f"Error: unknown tool {name!r}", False
        else:
            text, ok = await self._call(sid, name, args,
                                        str(meta.get(TOOL_USE_META) or meta.get(CODEX_CALL_META) or ""))
        return {"content": [{"type": "text", "text": text}], "isError": not ok}


def relay_node_args(port: int = RELAY_PORT) -> list[str]:
    """The arguments the relay's `node` runs with; tests launch the relay with exactly these."""
    return ["-e", RELAY_SCRIPT.read_text(encoding="utf-8"), str(port)]


class McpRelay:
    """The per-session relay sidecar: an unprivileged node process in the CLI image, driven over stdio."""

    def __init__(self, *, session_id: str, backend, server: McpServer, popen: Callable = subprocess.Popen,
                 command: list[str] | None = None, ready_timeout: float = 30):
        self.session_id = session_id
        self.backend = backend
        self.server = server
        self.container = f"harness-{session_id}-mcp"
        self._popen = popen
        self._command_override = command
        self._ready_timeout = ready_timeout
        self.process: subprocess.Popen | None = None
        self._ready: asyncio.Future | None = None
        self._tasks: set[asyncio.Task] = set()
        self._write_lock = threading.Lock()
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []

    def command(self) -> list[str]:
        if self._command_override is not None:
            return list(self._command_override)
        return ["docker", "run", "--rm", "-i", "--name", self.container,
                "--label", f"agent-harness.session={self.session_id}",
                "--network", self.backend.network,
                "--memory", "128m", "--cpus", "0.25", "--pids-limit", "64",
                "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                "--entrypoint", "node", self.backend.image, *relay_node_args()]

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
        self._ready = loop.create_future()
        process = self._popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.process = process

        def stdout_reader() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    loop.call_soon_threadsafe(self._on_line, line)
            finally:
                loop.call_soon_threadsafe(self._on_exit)

        def stderr_reader() -> None:
            assert process.stderr is not None
            self._stderr.extend(process.stderr)

        self._threads = [threading.Thread(target=stdout_reader, daemon=True),
                         threading.Thread(target=stderr_reader, daemon=True)]
        for thread in self._threads:
            thread.start()
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self._ready_timeout)
        except asyncio.TimeoutError as e:
            raise McpRelayError("the MCP relay did not start in time") from e

    def _on_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if "ready" in message:
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(True)
            return
        if "id" in message:
            task = asyncio.ensure_future(self._serve(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _on_exit(self) -> None:
        if self._ready is not None and not self._ready.done():
            error = "".join(self._stderr).strip()[:300]
            self._ready.set_exception(McpRelayError(f"the MCP relay exited: {error}".rstrip(": ")))

    async def _serve(self, request: dict) -> None:
        try:
            response = await self.server.handle(self.session_id, request)
        except Exception:  # noqa: BLE001 - one bad request must not take the relay down
            log.exception("MCP request failed for session %s", self.session_id)
            response = _reply(500, {"error": "internal error"})
        line = json.dumps({"id": request["id"], **response}, ensure_ascii=False) + "\n"

        def write() -> None:
            process = self.process
            if process is None or process.stdin is None or process.poll() is not None:
                return
            with self._write_lock:
                process.stdin.write(line)
                process.stdin.flush()
        try:
            await asyncio.to_thread(write)
        except OSError:
            pass

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        proc = self.process
        self.process = None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()  # the relay exits when its stdin closes
                await asyncio.to_thread(proc.wait, 2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                await asyncio.to_thread(proc.wait)
        for thread in self._threads:
            await asyncio.to_thread(thread.join, 0.5)
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
