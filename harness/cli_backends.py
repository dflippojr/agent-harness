"""Unmodified provider CLIs used as session backends.

The daemon owns stdin/stdout and translates the provider's JSONL protocol into
the harness's ordinary session events. Provider credentials and each CLI's
own state stay in Docker volumes, one state volume per domain (Web or one App, see cli_domains); this module never
opens those volumes or handles a login flow.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import claude_token, cli_domains, credential_sources, end_users  # noqa: F401 - end_users registers its source
from .config import BackendConfig, SandboxConfig
from .mcp_server import TOKEN_ENV, McpRelay, McpRelayError, codex_mcp_overrides, mcp_config
from .sandbox import run_cmd

# The bind-mount target every provider CLI runs in, and the proxy env every container needs.
WORKSPACE = "/workspace"
NO_PROXY = "NO_PROXY=localhost,127.0.0.1"
# Keep this at runtime as well as in cli.Dockerfile so sessions still
# work if the daemon is briefly paired with an older cached image.
NODE_USE_ENV_PROXY = "NODE_USE_ENV_PROXY=1"
EMPTY_MCP_CONFIG = '{"mcpServers": {}}'


class CliBackendError(Exception):
    """The provider CLI exited or stopped speaking valid JSONL. `code` is a stable failure code when there is one."""

    def __init__(self, message: str = "", code: str = ""):
        super().__init__(message)
        self.code = code


async def ready_domain(name: str, backend: BackendConfig, app_id: str, api_key: str, end_user: str = "") -> None:
    """Prepare the session's domain volumes; refuse an App on a CLI whose login is per App until it has one. An end
    user's session runs on that person's own login or is refused (#365): no other credential is tried."""
    if end_user:
        source = credential_sources.select(app_id, end_user)
        try:
            await source.ready(name, backend, app_id, end_user)
        except credential_sources.CredentialRefused as e:
            raise CliBackendError(str(e), e.code) from e
        return
    try:
        await cli_domains.prepare(name, backend, app_id)
    except RuntimeError as e:
        raise CliBackendError(str(e)) from e
    if name == "claude":
        reason = claude_token.refusal(backend, app_id, api_key)
        if reason:
            raise CliBackendError(reason)
    if app_id and not api_key and cli_domains.needs_app_login(name):
        from .backend_state import app_login_ready
        if not await asyncio.to_thread(app_login_ready, name, backend, app_id):
            raise CliBackendError(f"{name.title()} has no login for this App yet: the owner runs "
                                  f"`ops/backends/login.ps1 {name} -App {app_id}` on the server")


class ClaudeSession:
    """One long-lived ``claude -p`` stream-json process."""

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None,
                 mcp: McpRelay | None = None, mcp_token: str = "", tools_only: bool = False, app_id: str = "", end_user: str = ""):
        self.session_id = session_id
        self.app_id = app_id  # the session's domain: "" for Web, else its App (#371)
        self.end_user = end_user  # the App's end user whose own login this session runs on (#365)
        self.workspace = workspace.resolve()
        self.tools_only = tools_only  # an App-tools-only session (#329): no built-in tools, only the MCP server's
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.backend_session_id = backend_session_id
        self.api_key = "" if end_user else api_key  # an end user's session never runs on a key of the App's or owner's
        self.container = f"harness-{session_id}-claude"
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
            "--mount", f"type=bind,source={self.workspace},target=/workspace",
            "-w", WORKSPACE,
            "--memory", self.sandbox.memory,
            "--cpus", str(self.sandbox.cpus),
            "--pids-limit", str(self.sandbox.pids),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
            self.backend.image,
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio",
            "--permission-mode", "default" if self.tools_only else self.backend.permission_mode, "--model", self.model,
            "--system-prompt" if self.tools_only else "--append-system-prompt", self.system_prompt,
        ]
        if self.tools_only:
            # "" turns off every built-in tool (Bash, Read, Edit, WebFetch, Task...); MCP tools stay. Skills and slash
            # commands go too. can_use_tool still sees every call and denies anything but the App's tools.
            args += ["--tools", "", "--disable-slash-commands"]
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
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            await ready_domain("claude", self.backend, self.app_id, self.api_key, self.end_user)
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
        decision = {"behavior": behavior}
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
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            if self.mcp is not None:
                await self.mcp.stop()


# Every Codex 0.154.0 built-in tool an App-tools-only session (#329) turns off, besides the ones that need an
# environment (exec_command, write_stdin, apply_patch, view_image, request_permissions), which `environments: []`
# removes. Source notes in docs/phase8a-design.md "Harness tools over MCP"; recheck them when the Codex pin moves.
CODEX_TOOLS_ONLY_OVERRIDES = (
    'web_search="disabled"',                          # hosted web_search and the standalone web.run
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    *(f"features.{name}=false" for name in (
        "shell_tool", "unified_exec", "view_image", "code_mode", "multi_agent", "apps", "plugins",
        "tool_suggest", "image_generation", "goals", "sleep_tool", "memories", "browser_use", "computer_use",
        "skill_mcp_dependency_install")),
)
# Thread items an App-tools-only Codex session may produce. Anything else means a built-in tool ran: the run stops.
CODEX_TOOLS_ONLY_ITEMS = ("userMessage", "hookPrompt", "agentMessage", "plan", "reasoning", "contextCompaction",
                          "mcpToolCall")
# Answers Codex takes for an MCP tool call approval (an MCP elicitation), as opposed to command and file approvals.
ELICITATION = "mcpServer/elicitation/request"


class CodexSession:
    """One long-lived ``codex app-server`` JSON-RPC process."""

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None,
                 mcp: McpRelay | None = None, mcp_token: str = "", tools_only: bool = False, app_id: str = "", end_user: str = ""):
        self.session_id = session_id
        self.app_id = app_id
        self.end_user = end_user
        self.workspace = workspace.resolve()
        # With MCP Codex shares the relay's network namespace, as Claude Code does (#373). An App-tools-only session
        # starts with no environment and every other built-in tool off, so only the harness server's tools remain.
        self.mcp = mcp
        self.mcp_token = mcp_token
        self.tools_only = tools_only
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.backend_session_id = backend_session_id
        self.api_key = "" if end_user else api_key
        self.container = f"harness-{session_id}-codex"
        self._popen = popen
        self._command_override = command
        self.process: subprocess.Popen | None = None
        self._events: asyncio.Queue[str | None] = asyncio.Queue()
        self._stderr: list[str] = []
        self._write_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._next_request_id = 1
        self._pending: list[dict] = []
        self._approval_methods: dict[str, str] = {}
        self.items: dict[str, dict] = {}
        self.asked: set = set()  # mcpToolCall item ids an MCP approval was already asked for
        self.last_answer = ""
        self.token_usage: dict = {}
        self.active_turn_id = ""
        self._turn_requests: set[int] = set()

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
            *cli_domains.docker_args("codex", self.backend, self.app_id, end_user=self.end_user),
            "--mount", f"type=bind,source={self.workspace},target=/workspace",
            "-w", WORKSPACE,
            "--memory", self.sandbox.memory,
            "--cpus", str(self.sandbox.cpus),
            "--pids-limit", str(self.sandbox.pids),
            # Docker's default seccomp profile blocks the unprivileged user
            # namespace that Linux Codex uses for bubblewrap. The outer
            # container still has no-new-privileges, dropped capabilities,
            # a workspace-only bind mount, and a provider-only network.
            "--security-opt", "seccomp=unconfined",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
            self.backend.image, "codex", "app-server", "--stdio",
        ]
        if self.mcp:
            args += codex_mcp_overrides()
        if self.tools_only:
            for override in CODEX_TOOLS_ONLY_OVERRIDES:
                args += ["-c", override]
        env_names = (["OPENAI_API_KEY"] if self.api_key else []) + ([TOKEN_ENV] if self.mcp else [])
        for name in env_names:  # by name only: the value comes from the docker client's environment
            at = args.index(self.backend.image)
            args[at:at] = ["-e", name]
        return args

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            await ready_domain("codex", self.backend, self.app_id, self.api_key, self.end_user)
            if self.mcp is not None:
                try:
                    await self.mcp.start()
                except McpRelayError as e:
                    raise CliBackendError(str(e)) from e
        child_env = os.environ.copy()
        if self.api_key:
            child_env["OPENAI_API_KEY"] = self.api_key
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

    def _id(self) -> int:
        value = self._next_request_id
        self._next_request_id += 1
        return value

    async def _write(self, item: dict) -> None:
        def write() -> None:
            if self.process is None or self.process.stdin is None or self.process.poll() is not None:
                raise CliBackendError("Codex app-server is not running")
            with self._write_lock:
                self.process.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
        try:
            await asyncio.to_thread(write)
        except OSError as e:
            raise CliBackendError(f"could not write to Codex app-server: {e}") from e

    async def _receive_raw(self, timeout: float | None = None) -> dict | None:
        try:
            line = await asyncio.wait_for(self._events.get(), timeout) if timeout else await self._events.get()
        except asyncio.TimeoutError:
            return None
        if line is None:
            code = self.process.poll() if self.process is not None else None
            error = "".join(self._stderr).strip()
            raise CliBackendError(f"Codex app-server exited with code {code}: {error}".rstrip())
        try:
            value = json.loads(line)
        except json.JSONDecodeError as e:
            raise CliBackendError(f"Codex app-server wrote invalid JSONL: {line[:300].rstrip()}") from e
        if not isinstance(value, dict):
            raise CliBackendError("Codex app-server JSONL event was not an object")
        return value

    async def _request(self, method: str, params: dict) -> dict:
        request_id = self._id()
        await self._write({"id": request_id, "method": method, "params": params})
        while True:
            event = await self._receive_raw(timeout=30)
            if event is None:
                raise CliBackendError(f"Codex app-server timed out answering {method}")
            if event.get("id") == request_id:
                if event.get("error"):
                    raise CliBackendError(f"Codex app-server {method} failed: {event['error']}")
                result = event.get("result")
                if not isinstance(result, dict):
                    raise CliBackendError(f"Codex app-server returned an invalid {method} response: {event!r}"[:500])
                return result
            self._pending.append(event)

    async def initialize(self, prompt: str) -> None:
        init = {"clientInfo": {"name": "agent-harness", "version": "0.1.0"}}
        if self.tools_only:  # `environments` is an experimental field in 0.154.0; nothing else here opts in
            init["capabilities"] = {"experimentalApi": True}
        await self._request("initialize", init)
        await self._write({"method": "initialized"})
        common = {
            "cwd": WORKSPACE, "model": self.model, "approvalPolicy": self._approval_policy(),
            "approvalsReviewer": "user", "sandbox": "read-only" if self.tools_only else "workspace-write",
        }
        if self.backend_session_id:
            result = await self._request("thread/resume", {**common, "threadId": self.backend_session_id})
        elif self.tools_only:
            # The App's prompt replaces Codex's coding-agent instructions, as --system-prompt does for Claude Code.
            result = await self._request("thread/start", {**common, "baseInstructions": self.system_prompt,
                                                          "environments": []})
        else:
            result = await self._request("thread/start", {
                **common, "developerInstructions": self.system_prompt,
            })
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or self.backend_session_id)
        if not thread_id:
            raise CliBackendError("Codex app-server did not return a thread id")
        self.backend_session_id = thread_id
        result = await self._request("turn/start", self._turn_start_params(prompt))
        turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
        self.active_turn_id = str(turn.get("id") or "")

    def _approval_policy(self) -> str:
        # Codex itself refuses every MCP call under "never", so an App-tools-only session always asks (the harness
        # answers from AppToolsPolicy; nobody is prompted).
        if self.tools_only:
            return "on-request"
        return self.backend.permission_mode if self.backend.permission_mode in ("on-request", "never") else "on-request"

    def _turn_start_params(self, content: str) -> dict:
        params = {"threadId": self.backend_session_id, "input": [{"type": "text", "text": content}],
                  "cwd": WORKSPACE, "model": self.model, "effort": self.backend.effort,
                  "approvalPolicy": self._approval_policy(), "approvalsReviewer": "user"}
        if self.tools_only:
            # No environment for this turn and the ones after it: no shell, apply_patch, view_image or
            # request_permissions. Every turn/start says so again, so a resumed thread can't get one back.
            params["environments"] = []
        return params

    def user_message(self, content: str) -> dict:
        request_id = self._id()
        input_ = [{"type": "text", "text": content}]
        if self.active_turn_id:
            return {"id": request_id, "method": "turn/steer", "params": {
                "threadId": self.backend_session_id, "expectedTurnId": self.active_turn_id, "input": input_}}
        self._turn_requests.add(request_id)
        return {"id": request_id, "method": "turn/start", "params": self._turn_start_params(content)}

    async def send(self, item: dict) -> None:
        await self._write(item)

    async def receive(self, timeout: float | None = None) -> dict | None:
        event = self._pending.pop(0) if self._pending else await self._receive_raw(timeout)
        if event and event.get("error") and event.get("id") is not None:
            raise CliBackendError(f"Codex app-server request failed: {event['error']}")
        if event and event.get("id") in self._turn_requests:
            self._turn_requests.discard(event["id"])
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
            self.active_turn_id = str(turn.get("id") or self.active_turn_id)
        if event and event.get("method") in (
                "item/commandExecution/requestApproval", "item/fileChange/requestApproval", ELICITATION):
            self._approval_methods[str(event.get("id"))] = str(event["method"])
        return event

    async def respond_permission(self, request_id, behavior: str, _args: dict, _message: str = "") -> None:
        # App-server's stable approval response is deliberately narrower than
        # Claude's: notes remain in the harness transcript, while Codex gets the
        # accept/decline decision.
        accept = behavior == "allow"
        if self._approval_methods.pop(str(request_id), "") == ELICITATION:
            # An MCP tool call approval. A plain accept approves this one call; no "persist" in _meta, so Codex
            # never remembers it for the session and asks again next time.
            result = {"action": "accept" if accept else "decline", "content": None, "_meta": None}
        else:
            result = {"decision": "accept" if accept else "decline"}
        await self._write({"id": request_id, "result": result})

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
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            if self.mcp is not None:
                await self.mcp.stop()


class CursorSession:
    """A sequence of Cursor Agent print-mode processes sharing one chat id.

    Cursor's headless CLI has no live stdin protocol. Messages received while a
    turn is running are queued and started with ``--resume`` after the current
    terminal result.
    """

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None,
                 app_id: str = ""):
        self.session_id = session_id
        self.app_id = app_id
        self.workspace = workspace.resolve()
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.backend_session_id = backend_session_id
        self.api_key = api_key
        self.container = f"harness-{session_id}-cursor"
        self._popen = popen
        self._command_override = command
        self.process: subprocess.Popen | None = None
        self._events: asyncio.Queue[str | None] = asyncio.Queue()
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []
        self._followups: list[str] = []
        self._result_usage: dict[str, int] = {}
        self._result_turns = 0
        self._result_cost = 0.0
        self.last_answer = ""

    def command(self, prompt: str = "") -> list[str]:
        if self._command_override is not None:
            args = list(self._command_override)
            if self.backend_session_id:
                args += ["--resume", self.backend_session_id]
            return [*args, prompt]
        args = [
            "docker", "run", "--rm", "-i", "--name", self.container,
            "--label", f"agent-harness.session={self.session_id}",
            "--network", self.backend.network,
            "-e", f"HTTPS_PROXY={self.backend.proxy}",
            "-e", f"HTTP_PROXY={self.backend.proxy}",
            "-e", NO_PROXY,
            "-e", NODE_USE_ENV_PROXY,
            # The login is $XDG_CONFIG_HOME/cursor/auth.json; HOME stays the container's own (#371).
            *cli_domains.docker_args("cursor", self.backend, self.app_id),
            "--mount", f"type=bind,source={self.workspace},target=/workspace",
            "-w", WORKSPACE,
            "--memory", self.sandbox.memory,
            "--cpus", str(self.sandbox.cpus),
            "--pids-limit", str(self.sandbox.pids),
            # Cursor's Linux sandbox creates its own unprivileged user
            # namespace. Docker's default seccomp and AppArmor profiles block
            # that setup; relax only those outer profiles while keeping the
            # inner Cursor sandbox, no-new-privileges, dropped capabilities,
            # resource limits, workspace-only mount and provider-only network.
            "--security-opt", "seccomp=unconfined",
            "--security-opt", "apparmor=unconfined",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
            self.backend.image,
            "agent", "-p", "--output-format", "stream-json", "--stream-partial-output",
            # Cursor exposes no host approval protocol in print mode. This is
            # the user's explicit Phase 8a choice, confined by the outer
            # workspace-only container, provider-only network and branch review.
            "--force", "--sandbox", "enabled", "--trust", "--workspace", WORKSPACE,
            "--model", self.model,
        ]
        if self.backend_session_id:
            args += ["--resume", self.backend_session_id]
        args.append(prompt)
        if self.api_key:
            at = args.index(self.backend.image)
            args[at:at] = ["-e", "CURSOR_API_KEY"]
        return args

    async def start(self) -> None:
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            await ready_domain("cursor", self.backend, self.app_id, self.api_key)

    def _spawn(self, prompt: str) -> None:
        loop = asyncio.get_running_loop()
        self._events = asyncio.Queue()
        self._stderr = []
        child_env = os.environ.copy()
        if self.api_key:
            child_env["CURSOR_API_KEY"] = self.api_key
        process = self._popen(
            self.command(prompt), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env,
        )
        self.process = process
        events = self._events

        def stdout_reader() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    loop.call_soon_threadsafe(events.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(events.put_nowait, None)

        def stderr_reader() -> None:
            assert process.stderr is not None
            self._stderr.extend(process.stderr)

        self._threads = [threading.Thread(target=stdout_reader, daemon=True),
                         threading.Thread(target=stderr_reader, daemon=True)]
        for thread in self._threads:
            thread.start()

    async def initialize(self, prompt: str) -> None:
        first_prompt = prompt if self.backend_session_id else f"{self.system_prompt}\n\nUser task:\n{prompt}"
        self._spawn(first_prompt)

    def user_message(self, content: str) -> dict:
        return {"type": "cursor_followup", "content": content}

    async def send(self, item: dict) -> None:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str):
            raise CliBackendError("Cursor follow-up was not text")
        self._followups.append(content)

    @property
    def has_followups(self) -> bool:
        return bool(self._followups)

    async def continue_followups(self) -> None:
        prompts, self._followups = self._followups, []
        await self._close_process(terminate=False)
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
        prompt = "Messages received while the prior turn was running:\n\n" + "\n\n".join(prompts)
        self._spawn(prompt)

    def combined_result(self, result: dict) -> dict:
        raw = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        cache_read = int(raw.get("cacheReadTokens") or raw.get("cache_read_input_tokens") or 0)
        cache_write = int(raw.get("cacheWriteTokens") or raw.get("cache_creation_input_tokens") or 0)
        total_input = int(raw.get("inputTokens") or raw.get("input_tokens") or 0)
        usage = {
            "input_tokens": max(0, total_input - cache_read - cache_write),
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": int(raw.get("outputTokens") or raw.get("output_tokens") or 0),
        }
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._result_usage[key] = self._result_usage.get(key, 0) + int(value)
        self._result_turns += int(result.get("num_turns") or 1)
        self._result_cost += float(result.get("total_cost_usd") or 0)
        self.last_answer = str(result.get("result") or self.last_answer)
        return {**result, "result": self.last_answer, "usage": dict(self._result_usage),
                "num_turns": self._result_turns, "total_cost_usd": self._result_cost}

    async def receive(self, timeout: float | None = None) -> dict | None:
        try:
            line = await asyncio.wait_for(self._events.get(), timeout) if timeout else await self._events.get()
        except asyncio.TimeoutError:
            return None
        if line is None:
            code = self.process.poll() if self.process is not None else None
            error = "".join(self._stderr).strip()
            raise CliBackendError(f"Cursor Agent exited with code {code} before a result: {error}".rstrip())
        try:
            value = json.loads(line)
        except json.JSONDecodeError as e:
            raise CliBackendError(f"Cursor Agent wrote invalid JSONL: {line[:300].rstrip()}") from e
        if not isinstance(value, dict):
            raise CliBackendError("Cursor Agent JSONL event was not an object")
        return value

    async def _close_process(self, terminate: bool) -> None:
        proc = self.process
        self.process = None
        if proc is not None and proc.poll() is None:
            if terminate:
                proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)
        for thread in self._threads:
            await asyncio.to_thread(thread.join, 0.5)
        self._threads = []

    async def stop(self) -> None:
        await self._close_process(terminate=True)
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
