"""Claude Code (`claude -p --input-format stream-json --output-format stream-json`): the adapter (#991).

`ClaudeSession` is the transport: one long-lived process in its container, stdin and stdout. `ClaudeTranslator`
turns its records into neutral events.

The verified protocol is in docs/phase8a-design.md ("Claude Code"). The records a session turns into events:

- `system`/`init`: the session id, kept for `--resume`;
- `stream_event`: a partial message; a `delta` with `text` streams it (`thinking_delta` and `reasoning_delta` as
  reasoning);
- `assistant`: a message's text and its `tool_use` blocks;
- `user`: the `tool_result` blocks, which name only the call id;
- `control_request` with subtype `can_use_tool`: the CLI asks the host (`--permission-prompt-tool stdio`);
- `rate_limit_event`: `rate_limit_info`, already in the shared shape (`status`, `utilization`, `resetsAt`, ...);
- `result`: the turn's end, with usage and cost.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .. import claude_token, cli_domains
from .. import cli_backends as shared  # run_cmd and ready_domain are looked up there at call time
from ..cli_backends import (EMPTY_MCP_CONFIG, NO_PROXY, NODE_USE_ENV_PROXY, WORKSPACE, CliBackendError, session_key,
                            split_note, workspace_args)
from ..config import BackendConfig, SandboxConfig
from ..mcp_server import TOKEN_ENV, McpRelay, McpRelayError, mcp_config
from ..workspace_guidance import GUIDANCE_LIMIT, cap, read_workspace_file
from .events import (Assistant, Delta, Event, PermissionRequest, RateLimits, Session, ToolCall, ToolResult, obj,
                     result_of, text_of)

REASONING_DELTAS = ("thinking_delta", "reasoning_delta")

# Claude Code built-ins that don't touch the environment, kept in split mode (#427). Everything else (Bash, Read, Edit,
# Write, Glob, Grep, WebFetch, ...) is off: the harness MCP server does that work on the session sandbox.
CLAUDE_SPLIT_TOOLS = "Task,TodoWrite"
# Claude Code builtin mode under the bash-only tool profile (#638): Bash and nothing else.
CLAUDE_BASH_ONLY_TOOLS = "Bash"

# Project instructions Claude Code would load from the workspace itself. `--setting-sources user` drops them along with
# the project settings (#531), so the session's system prompt carries them instead.
PROJECT_MEMORY_FILES = ("CLAUDE.md", ".claude/CLAUDE.md")
PROJECT_MEMORY_LIMIT = GUIDANCE_LIMIT  # characters: the prompt travels on the docker command line


def project_memory(workspace: Path) -> str:
    """The workspace's CLAUDE.md files as a system prompt section, or "". A file reached through a link is skipped:
    it is read on the host, where a link could point anywhere."""
    sections = [f"Contents of {WORKSPACE}/{name} (project instructions):\n\n{text}"
                for name in PROJECT_MEMORY_FILES if (text := read_workspace_file(workspace, name))]
    body = cap("\n\n".join(sections), PROJECT_MEMORY_LIMIT, f"read the files in {WORKSPACE} for the rest")
    return "\n\n" + body if body else ""


class ClaudeTranslator:
    """One Claude Code process's stream-json records as neutral events. A tool result names only its call id, so the
    tool names that assistant messages and permission requests announced are kept by id."""

    def __init__(self) -> None:
        self.tool_names: dict[str, str] = {}

    def translate(self, record: object) -> list[Event]:
        if not isinstance(record, dict):
            return []
        type_ = record.get("type")
        if type_ == "system" and record.get("subtype") == "init":
            session_id = str(record.get("session_id") or "")
            return [Session(session_id)] if session_id else []
        if type_ == "stream_event":
            return self._delta(record)
        if type_ == "assistant":
            return [self._assistant(record)]
        if type_ == "user":
            return self._tool_results(record)
        if type_ == "control_request":
            return self._permission(record)
        if type_ == "rate_limit_event":
            info = obj(record, "rate_limit_info")
            return [RateLimits(info, limit_reached=info.get("status") == "rejected",
                               resets_at=float(info.get("resetsAt") or 0), billing=True)]
        if type_ == "result":
            result = result_of(record)
            return [replace(result, limit_reached=result.is_error and "rate limit" in result.answer.lower())]
        return []

    @staticmethod
    def _delta(record: dict) -> list[Event]:
        delta = obj(obj(record, "event"), "delta")
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return []
        return [Delta("reasoning" if delta.get("type") in REASONING_DELTAS else "content", text)]

    def _assistant(self, record: dict) -> Assistant:
        content = obj(record, "message").get("content") or []
        calls = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
            self.tool_names[call_id] = name
            calls.append(ToolCall(call_id, name, obj(block, "input")))
        return Assistant(text_of(content), tuple(calls))

    def _tool_results(self, record: dict) -> list[Event]:
        content = obj(record, "message").get("content") or []
        results: list[Event] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            results.append(ToolResult(call_id, self.tool_names.get(call_id, ""), not bool(block.get("is_error")),
                                      text_of(block.get("content"))))
        return results

    def _permission(self, record: dict) -> list[Event]:
        request = obj(record, "request")
        if request.get("subtype") != "can_use_tool":
            return []
        request_id = str(record.get("request_id") or "")
        tool = str(request.get("tool_name") or "")
        call_id = str(request.get("tool_use_id") or "")
        self.tool_names[call_id or request_id] = tool
        return [PermissionRequest(request_id, tool, obj(request, "input"), call_id,
                                  str(request.get("description") or ""))]


class ClaudeSession:
    """One long-lived ``claude -p`` stream-json process."""

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None,
                 mcp: McpRelay | None = None, mcp_token: str = "", tools_only: bool = False, app_id: str = "", end_user: str = "", split: bool = False,
                 tool_profile: str = "default"):
        self.session_id = session_id
        self.tool_profile = tool_profile  # "bash-only" (#638): --tools Bash, or run_shell alone in split mode
        self.app_id = app_id  # the session's domain: "" for Web, else its App (#371)
        self.end_user = end_user  # the App's end user whose own login this session runs on (#365)
        self.workspace = workspace.resolve()
        self.tools_only = tools_only  # an App-tools-only session (#329): no built-in tools, only the MCP server's
        self.split = split and not tools_only  # split mode (#427): token here, shell and files over MCP, no workspace
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.backend_session_id = backend_session_id
        self.api_key = session_key(end_user, api_key)
        self.container = f"harness-{session_id}-claude"
        self.translator = ClaudeTranslator()
        # With MCP the CLI shares the relay's network namespace: the relay's loopback port is reachable from this
        # container only, and egress still goes through the same network and proxy.
        self.mcp = mcp
        self.mcp_token = mcp_token
        self._popen = popen
        self._command_override = command
        self.process: subprocess.Popen | None = None
        self._events: asyncio.Queue[str | None] = asyncio.Queue()
        self._stderr: list[str] = []
        self._write_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def command(self) -> list[str]:
        if self._command_override is not None:
            return list(self._command_override)
        args = [
            "docker", "run", "--rm", "-i", "--name", self.container,
            "--label", f"agent-harness.session={self.session_id}",
            "--network", f"container:{self.mcp.container}" if self.mcp else self.backend.network,
            "-e", f"HTTPS_PROXY={self.backend.proxy}",
            "-e", f"HTTP_PROXY={self.backend.proxy}",
            "-e", NO_PROXY,
            "-e", NODE_USE_ENV_PROXY,
            *cli_domains.docker_args("claude", self.backend, self.app_id, token=self._uses_token(), end_user=self.end_user),
            *workspace_args(self.workspace, self.split),
            "--memory", self.sandbox.memory,
            "--cpus", str(self.sandbox.cpus),
            "--pids-limit", str(self.sandbox.pids),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
            self.backend.image,
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio",
            "--permission-mode", "default" if self.tools_only else self.backend.permission_mode, "--model", self.model,
            # User settings only (#531): the read-only state files and the managed settings. Project and local
            # settings in the workspace's .claude/ could set env, helpers or permissions the session never asked for.
            "--setting-sources", "user",
            "--system-prompt" if self.tools_only else "--append-system-prompt",
            self.system_prompt + (split_note(self.tool_profile) if self.split else project_memory(self.workspace)),
        ]
        if self.tools_only:
            # "" turns off every built-in tool (Bash, Read, Edit, WebFetch, Task...); MCP tools stay. Skills and slash
            # commands go too. can_use_tool still sees every call and denies anything but the App's tools.
            args += ["--tools", "", "--disable-slash-commands"]
        elif self.split:
            args += ["--tools", CLAUDE_SPLIT_TOOLS]  # under bash-only the harness MCP server narrows instead
        elif self.tool_profile == "bash-only":
            args += ["--tools", CLAUDE_BASH_ONLY_TOOLS]
        if self.backend_session_id:
            args += ["--resume", self.backend_session_id]
        # Only the harness server, or none: --strict-mcp-config ignores any .mcp.json the workspace brings along and
        # the user-scope servers in the state's .claude.json, which a session could have written (#371).
        args += ["--mcp-config", mcp_config() if self.mcp else EMPTY_MCP_CONFIG, "--strict-mcp-config"]
        env_names = ((["ANTHROPIC_API_KEY"] if self.api_key else []) + ([claude_token.ENV] if self._uses_token() else [])
                     + ([TOKEN_ENV] if self.mcp else []))
        for name in env_names:  # by name only: the value comes from the docker client's environment
            at = args.index(self.backend.image)
            args[at:at] = ["-e", name]
        return args

    def _uses_token(self) -> bool:
        return not self.end_user and claude_token.uses_token(self.backend, self.app_id, self.api_key)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._command_override is None:
            # The Windows daemon restart script force-stops Python. That skips
            # our finally block and can leave `docker run`'s container behind.
            # Remove only this session's deterministic container before reuse.
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            await shared.ready_domain("claude", self.backend, self.app_id, self.api_key, self.end_user)
            if self.mcp is not None:
                try:
                    await self.mcp.start()
                except McpRelayError as e:
                    raise CliBackendError(str(e)) from e
        child_env = os.environ.copy()
        if self.api_key:
            child_env["ANTHROPIC_API_KEY"] = self.api_key
        child_env.pop(claude_token.ENV, None)  # the daemon's own environment never leaks a token into a session
        if self._uses_token():
            token = claude_token.read_token(self.backend.oauth_token_file)
            if not token:
                raise CliBackendError("the Claude subscription token file is missing or empty; run "
                                      "ops/backends/login.ps1 claude -Token")
            child_env[claude_token.ENV] = token
        if self.mcp is not None:
            child_env[TOKEN_ENV] = self.mcp_token
        process = self._popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env,
        )
        self.process = process

        def stdout_reader() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    loop.call_soon_threadsafe(self._events.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(self._events.put_nowait, None)

        def stderr_reader() -> None:
            assert process.stderr is not None
            self._stderr.extend(process.stderr)

        self._threads = [threading.Thread(target=stdout_reader, daemon=True),
                         threading.Thread(target=stderr_reader, daemon=True)]
        for thread in self._threads:
            thread.start()

    async def send(self, item: dict) -> None:
        def write() -> None:
            if self.process is None or self.process.stdin is None or self.process.poll() is not None:
                raise CliBackendError("Claude CLI is not running")
            with self._write_lock:
                self.process.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
        try:
            await asyncio.to_thread(write)
        except OSError as e:
            raise CliBackendError(f"could not write to Claude CLI: {e}") from e

    async def receive(self, timeout: float | None = None) -> dict | None:
        try:
            line = await asyncio.wait_for(self._events.get(), timeout) if timeout else await self._events.get()
        except asyncio.TimeoutError:
            return None
        if line is None:
            code = self.process.poll() if self.process is not None else None
            error = "".join(self._stderr).strip()
            raise CliBackendError(f"Claude CLI exited with code {code}: {error}".rstrip())
        try:
            value = json.loads(line)
        except json.JSONDecodeError as e:
            raise CliBackendError(f"Claude CLI wrote invalid JSONL: {line[:300].rstrip()}") from e
        if not isinstance(value, dict):
            raise CliBackendError("Claude CLI JSONL event was not an object")
        return value

    async def initialize(self, prompt: str) -> None:
        await self.send({"type": "control_request", "request_id": "init-1",
                         "request": {"subtype": "initialize"}})
        reply = await self.receive(timeout=30)
        response = (reply or {}).get("response") or {}
        if (not reply or reply.get("type") != "control_response" or response.get("subtype") != "success"
                or response.get("request_id") != "init-1"):
            raise CliBackendError(f"Claude CLI returned an invalid initialize response: {reply!r}"[:500])
        await self.send(self.user_message(prompt))

    def user_message(self, content: str) -> dict:
        return {"type": "user", "message": {"role": "user", "content": content},
                "parent_tool_use_id": None, "session_id": ""}

    async def respond_permission(self, request_id: str, behavior: str, args: dict,
                                 message: str = "") -> None:
        decision: dict[str, object] = {"behavior": behavior}
        if behavior == "allow":
            decision["updatedInput"] = args
        elif message:
            decision["message"] = message
        await self.send({"type": "control_response", "response": {"subtype": "success",
                         "request_id": request_id, "response": decision}})

    async def stop(self) -> None:
        proc = self.process
        self.process = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)
        for thread in self._threads:
            await asyncio.to_thread(thread.join, 0.5)
        if self._command_override is None:
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            if self.mcp is not None:
                await self.mcp.stop()
