"""Unmodified provider CLIs used as session backends.

The daemon owns stdin/stdout and translates the provider's JSONL protocol into
the harness's ordinary session events. Provider credentials stay in Docker
volumes; this module never opens those volumes or handles a login flow.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .config import BackendConfig, SandboxConfig
from .sandbox import run_cmd

# The bind-mount target every provider CLI runs in, and the proxy env every container needs.
WORKSPACE = "/workspace"
NO_PROXY = "NO_PROXY=localhost,127.0.0.1"
# Keep this at runtime as well as in cli.Dockerfile so sessions still
# work if the daemon is briefly paired with an older cached image.
NODE_USE_ENV_PROXY = "NODE_USE_ENV_PROXY=1"


class CliBackendError(Exception):
    """The provider CLI exited or stopped speaking valid JSONL."""


class ClaudeSession:
    """One long-lived ``claude -p`` stream-json process."""

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None):
        self.session_id = session_id
        self.workspace = workspace.resolve()
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.backend_session_id = backend_session_id
        self.api_key = api_key
        self.container = f"harness-{session_id}-claude"
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
            "--network", self.backend.network,
            "-e", f"HTTPS_PROXY={self.backend.proxy}",
            "-e", f"HTTP_PROXY={self.backend.proxy}",
            "-e", NO_PROXY,
            "-e", NODE_USE_ENV_PROXY,
            "-e", "CLAUDE_CONFIG_DIR=/home/agent/.claude",
            "-v", f"{self.backend.volume}:/home/agent/.claude",
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
            "--permission-mode", self.backend.permission_mode, "--model", self.model,
            "--append-system-prompt", self.system_prompt,
        ]
        if self.backend_session_id:
            args += ["--resume", self.backend_session_id]
        if self.api_key:
            at = args.index(self.backend.image)
            args[at:at] = ["-e", "ANTHROPIC_API_KEY"]
        return args

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._command_override is None:
            # The Windows daemon restart script force-stops Python. That skips
            # our finally block and can leave `docker run`'s container behind.
            # Remove only this session's deterministic container before reuse.
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
        child_env = os.environ.copy()
        if self.api_key:
            child_env["ANTHROPIC_API_KEY"] = self.api_key
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
        except (BrokenPipeError, OSError) as e:
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


class CodexSession:
    """One long-lived ``codex app-server`` JSON-RPC process."""

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None):
        self.session_id = session_id
        self.workspace = workspace.resolve()
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.backend_session_id = backend_session_id
        self.api_key = api_key
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
            "--network", self.backend.network,
            "-e", f"HTTPS_PROXY={self.backend.proxy}",
            "-e", f"HTTP_PROXY={self.backend.proxy}",
            "-e", NO_PROXY,
            "-e", NODE_USE_ENV_PROXY,
            "-e", "CODEX_HOME=/home/agent/.codex",
            "-v", f"{self.backend.volume}:/home/agent/.codex",
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
        if self.api_key:
            at = args.index(self.backend.image)
            args[at:at] = ["-e", "OPENAI_API_KEY"]
        return args

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
        child_env = os.environ.copy()
        if self.api_key:
            child_env["OPENAI_API_KEY"] = self.api_key
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
        except (BrokenPipeError, OSError) as e:
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
        await self._request("initialize", {"clientInfo": {"name": "agent-harness", "version": "0.1.0"}})
        await self._write({"method": "initialized"})
        approval_policy = (self.backend.permission_mode if self.backend.permission_mode in
                           ("on-request", "never") else "on-request")
        common = {
            "cwd": WORKSPACE, "model": self.model, "approvalPolicy": approval_policy,
            "approvalsReviewer": "user", "sandbox": "workspace-write",
        }
        if self.backend_session_id:
            result = await self._request("thread/resume", {**common, "threadId": self.backend_session_id})
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

    def _turn_start_params(self, content: str) -> dict:
        approval_policy = (self.backend.permission_mode if self.backend.permission_mode in
                           ("on-request", "never") else "on-request")
        return {"threadId": self.backend_session_id, "input": [{"type": "text", "text": content}],
                "cwd": WORKSPACE, "model": self.model, "effort": self.backend.effort,
                "approvalPolicy": approval_policy, "approvalsReviewer": "user"}

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
                "item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            self._approval_methods[str(event.get("id"))] = str(event["method"])
        return event

    async def respond_permission(self, request_id, behavior: str, args: dict, message: str = "") -> None:
        # App-server's stable approval response is deliberately narrower than
        # Claude's: notes remain in the harness transcript, while Codex gets the
        # accept/decline decision.
        original_id = request_id
        await self._write({"id": original_id, "result": {
            "decision": "accept" if behavior == "allow" else "decline"}})

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


class CursorSession:
    """A sequence of Cursor Agent print-mode processes sharing one chat id.

    Cursor's headless CLI has no live stdin protocol. Messages received while a
    turn is running are queued and started with ``--resume`` after the current
    terminal result.
    """

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig,
                 sandbox: SandboxConfig, system_prompt: str, model: str = "", backend_session_id: str = "",
                 api_key: str = "", popen: Callable = subprocess.Popen, command: list[str] | None = None):
        self.session_id = session_id
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
            # Keep every home-relative Cursor auth path inside the provider
            # volume; recent releases do not keep browser auth solely in the
            # documented config directory.
            "-e", "HOME=/home/agent/.cursor/home",
            "-e", "CURSOR_CONFIG_DIR=/home/agent/.cursor/config",
            "-v", f"{self.backend.volume}:/home/agent/.cursor",
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

    async def _spawn(self, prompt: str) -> None:
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
        await self._spawn(first_prompt)

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
        await self._spawn(prompt)

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
