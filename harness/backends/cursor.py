"""Cursor Agent (`agent -p --output-format stream-json --stream-partial-output`): the adapter (#991).

`CursorSession` is the transport: one print-mode process per turn on one chat. `CursorTranslator` turns its records
into neutral events.

Cursor's print mode has no host approval protocol (the session runs with `--force`, confined by its container) and
reports no rate limits; a usage limit only shows as a failed result. The records a session turns into events:

- `system`/`init`: the chat id, kept for `--resume`;
- `thinking`: reasoning deltas;
- `assistant`: with `timestamp_ms`, a streamed delta; without it, the turn's aggregate text, which is the answer;
- `tool_call`: `started` and `completed`, the tool named by the one key under `tool_call`;
- `result`: the turn's end. One process runs one turn, and messages that arrive meanwhile become the next turn on
  the same chat, so usage, turns and cost add up over the session's turns.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .. import cli_domains
from .. import cli_backends as shared  # run_cmd and ready_domain are looked up there at call time
from ..cli_backends import NO_PROXY, NODE_USE_ENV_PROXY, WORKSPACE, CliBackendError, TurnProcess
from ..config import BackendConfig, SandboxConfig
from .events import (FAILED_SUBTYPES, Assistant, Delta, Event, Result, Session, ToolCall, ToolResult, obj,
                     text_of)

LIMIT_MARKERS = ("rate limit", "quota", "usage limit")


def cursor_tool(record: dict) -> tuple[str, dict, dict]:
    """A tool_call record's tool: its name (the key under `tool_call`), arguments and result."""
    for name, value in obj(record, "tool_call").items():
        if isinstance(value, dict):
            return name, obj(value, "args"), obj(value, "result")
    return "cursorTool", {}, {}


class CursorTranslator:
    """One Cursor session's records over its turns, as neutral events. It keeps the answer (the latest aggregate
    assistant text, or the result's) and the usage, turns and cost the turns so far add up to."""

    def __init__(self) -> None:
        self.answer = ""
        self.usage: dict[str, int] = {}
        self.turns = 0
        self.cost = 0.0

    def translate(self, record: object) -> list[Event]:
        if not isinstance(record, dict):
            return []
        type_ = str(record.get("type") or "")
        if type_ == "system" and record.get("subtype") == "init":
            chat_id = str(record.get("session_id") or "")
            return [Session(chat_id)] if chat_id else []
        if type_ in ("assistant", "thinking"):
            return self._text(record, type_)
        if type_ == "tool_call":
            return self._tool(record)
        if type_ == "result":
            return [self._result(record)]
        return []

    def _text(self, record: dict, type_: str) -> list[Event]:
        text = text_of(obj(record, "message").get("content"))
        if not text and isinstance(record.get("text"), str):
            text = record["text"]
        if not text:
            return []
        # With --stream-partial-output Cursor emits timestamped deltas, then one untimestamped aggregate assistant
        # record. Stream the deltas and keep the aggregate without showing it twice.
        streamed = type_ == "thinking" or record.get("timestamp_ms") is not None
        if type_ == "assistant" and record.get("timestamp_ms") is None:
            self.answer = text
        return [Delta("reasoning" if type_ == "thinking" else "content", text)] if streamed else []

    @staticmethod
    def _tool(record: dict) -> list[Event]:
        call_id = str(record.get("call_id") or "")
        name, args, result = cursor_tool(record)
        if record.get("subtype") == "started":
            return [Assistant(tool_calls=(ToolCall(call_id, name, args),))]
        if record.get("subtype") == "completed":
            ok = "success" in result and "error" not in result
            return [ToolResult(call_id, name, ok, text_of(result.get("success") if ok else result.get("error", result)))]
        return []

    def _result(self, record: dict) -> Result:
        raw = obj(record, "usage")
        cache_read = int(raw.get("cacheReadTokens") or raw.get("cache_read_input_tokens") or 0)
        cache_write = int(raw.get("cacheWriteTokens") or raw.get("cache_creation_input_tokens") or 0)
        total_input = int(raw.get("inputTokens") or raw.get("input_tokens") or 0)
        usage = {"input_tokens": max(0, total_input - cache_read - cache_write),
                 "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
                 "output_tokens": int(raw.get("outputTokens") or raw.get("output_tokens") or 0)}
        for key, value in usage.items():
            self.usage[key] = self.usage.get(key, 0) + value
        self.turns += int(record.get("num_turns") or 1)
        self.cost += float(record.get("total_cost_usd") or 0)
        self.answer = str(record.get("result") or self.answer)
        subtype = str(record.get("subtype") or "")
        is_error = bool(record.get("is_error")) or subtype in FAILED_SUBTYPES
        return Result(answer=self.answer, usage=dict(self.usage), turns=self.turns, cost=self.cost,
                      is_error=is_error, subtype=subtype, show_answer=True,
                      limit_reached=is_error and any(m in self.answer.lower() for m in LIMIT_MARKERS))


class CursorSession(TurnProcess):
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
        self.translator = CursorTranslator()

    @property
    def last_answer(self) -> str:
        return self.translator.answer

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
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            await shared.ready_domain("cursor", self.backend, self.app_id, self.api_key)

    def _turn(self, prompt: str) -> None:
        child_env = os.environ.copy()
        if self.api_key:
            child_env["CURSOR_API_KEY"] = self.api_key
        self._spawn(self.command(prompt), child_env)

    async def initialize(self, prompt: str) -> None:
        first_prompt = prompt if self.backend_session_id else f"{self.system_prompt}\n\nUser task:\n{prompt}"
        self._turn(first_prompt)

    def user_message(self, content: str) -> dict:
        return {"type": "cursor_followup", "content": content}

    async def continue_followups(self) -> None:
        self._turn(await self._take_followups())

    async def receive(self, timeout: float | None = None) -> dict | None:
        line = await self._next_line(timeout)
        if line is None:
            code = self.process.poll() if self.process is not None else None
            error = "".join(self._stderr).strip()
            raise CliBackendError(f"Cursor Agent exited with code {code} before a result: {error}".rstrip())
        if not isinstance(line, str):  # False: nothing within the timeout
            return None
        try:
            value = json.loads(line)
        except json.JSONDecodeError as e:
            raise CliBackendError(f"Cursor Agent wrote invalid JSONL: {line[:300].rstrip()}") from e
        if not isinstance(value, dict):
            raise CliBackendError("Cursor Agent JSONL event was not an object")
        return value

    async def stop(self) -> None:
        await self._close_process(terminate=True)
        if self._command_override is None:
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
