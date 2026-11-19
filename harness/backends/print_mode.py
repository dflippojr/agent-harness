"""Config-declared print-mode CLIs (`kind: cli`, #168): the adapter (#991).

`PrintModeSession` is the transport (one `docker run --rm` per turn, argv from config); `PrintModeTranslator` maps a
registered parser's events onto neutral ones.

A parser registered in `PRINT_MODE_PARSERS` keeps the #168 contract: `parse(line) -> list[event]` over the CLI's
stdout, with `session`, `text`, `tool`, `step` and `error` events, and an `answer` property. The transport adds an
`exit` record when a turn's process ends. `PrintModeTranslator` maps both onto the neutral events, so a new CLI needs
a parser and nothing in the Runner.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .. import cli_domains
from .. import cli_backends as shared  # run_cmd is looked up there at call time
from ..cli_backends import WORKSPACE, CliBackendError, TurnProcess
from ..config import BackendConfig, SandboxConfig
from .events import Assistant, Delta, Error, Event, Result, Session, ToolCall, ToolResult
from .opencode import OpencodeJsonl

PRINT_MODE_PARSERS: dict[str, type] = {"opencode_jsonl": OpencodeJsonl}


class PrintModeTranslator:
    """One session's records: stdout lines through `parser`, and the transport's `{"type": "exit", "code", "stderr"}`
    at the end of each turn. `name` is the backend's, for the error messages. Steps are counted over the session's
    turns; they are its turn count."""

    def __init__(self, parser, name: str):
        self.parser = parser
        self.name = name
        self.steps = 0

    def translate(self, record: object) -> list[Event]:
        if isinstance(record, str):
            return [event for item in self.parser.parse(record) for event in self._event(item)]
        if isinstance(record, dict) and record.get("type") == "exit":
            return self._exit(record)
        return []

    def _event(self, item: dict) -> list[Event]:
        type_ = item.get("type")
        if type_ == "session":
            return [Session(str(item["id"]))]
        if type_ == "text":
            return [Delta("content", item["text"])]
        if type_ == "tool":
            # The CLI reports a tool once it has run (auto-approved in its container): the call and its result.
            call = ToolCall(item["id"], item["name"], item["input"])
            return [Assistant(tool_calls=(call,)), ToolResult(item["id"], item["name"], not item["error"],
                                                              item["output"])]
        if type_ == "step":
            self.steps += 1
        elif type_ == "error":
            return [Error(f"{self.name}: {item['message']}")]
        return []

    def _exit(self, record: dict) -> list[Event]:
        """A turn's process ended: an error, or the turn's result (the next queued turn, or the run's end). Usage is
        not reported."""
        code = record.get("code")
        if code != 0:
            detail = f": {record['stderr']}" if record.get("stderr") else ""
            return [Error(f"{self.name} exited with code {code}{detail}")]
        return [Result(answer=self.parser.answer, turns=self.steps, show_answer=True)]


class PrintModeSession(TurnProcess):
    """A config-declared headless CLI (`kind: cli`, #168): one ``docker run --rm`` per turn, as CursorSession.

    The argv comes from config: `command` with {model} and {session_id} filled in per element (never through a
    shell), `resume_args` once the CLI has a session id, then ``--`` and the prompt as the last element. Messages
    that arrive mid-turn are queued and become the next turn on the same CLI session. Auto-approve runs inside the
    container: a workspace-only mount, the backend's own internal network with no proxy and no route out, no key.
    `admit` (the runner's) runs before every turn: the GPU hold, RAM and the model load (Q7, Q10).
    """

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig, sandbox: SandboxConfig,
                 system_prompt: str, model: str = "", backend_session_id: str = "", api_key: str = "",
                 popen: Callable = subprocess.Popen, command: list[str] | None = None, app_id: str = "",
                 name: str = "", model_config=None, base_url: str = "", config_dir: Path | None = None,
                 admit: Callable | None = None):
        self.session_id = session_id
        self.app_id = app_id
        self.name = name
        self.workspace = workspace.resolve()
        self.backend = backend
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.model = model or backend.model
        self.model_config = model_config
        self.base_url = base_url
        self.backend_session_id = backend_session_id
        self.container = f"harness-{session_id}-{name}"
        self.parser = PRINT_MODE_PARSERS[backend.parser]()
        self.translator = PrintModeTranslator(self.parser, name)
        self._config_dir = config_dir
        self._admit = admit
        self._popen = popen
        self._command_override = command
        self.process: subprocess.Popen | None = None
        self._events: asyncio.Queue[str | None] = asyncio.Queue()
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []
        self._followups: list[str] = []

    @property
    def config_file(self) -> Path | None:
        """The generated CLI config on the host, mounted read-only (None when the parser has none)."""
        if self._config_dir is None or not getattr(self.parser, "config_target", ""):
            return None
        return self._config_dir / f"{self.session_id}.json"

    @property
    def last_answer(self) -> str:
        return self.parser.answer

    def _fill(self, item: str) -> str:
        return item.replace("{model}", self.model).replace("{session_id}", self.backend_session_id)

    def cli_args(self, prompt: str) -> list[str]:
        """The CLI's own argv: command, resume args on a follow-up turn, then `-- <prompt>`."""
        args = [self._fill(item) for item in self.backend.command]
        if self.backend_session_id:
            args += [self._fill(item) for item in self.backend.resume_args]
        return [*args, "--", prompt]

    def command(self, prompt: str = "") -> list[str]:
        if self._command_override is not None:
            return [*self._command_override, *self.cli_args(prompt)]
        args = [
            "docker", "run", "--rm", "--name", self.container,
            "--label", f"agent-harness.session={self.session_id}",
            "--network", self.backend.network,  # internal; its only other member forwards to llama-server
            *cli_domains.docker_args(self.name, self.backend, self.app_id),
            *[a for item in getattr(self.parser, "env", ()) for a in ("-e", item)],
        ]
        if self.config_file is not None:
            args += ["--mount", f"type=bind,source={self.config_file},target={self.parser.config_target},readonly"]
        return [
            *args,
            "--mount", f"type=bind,source={self.workspace},target=/workspace",
            "-w", WORKSPACE,
            "--memory", self.sandbox.memory,
            "--cpus", str(self.sandbox.cpus),
            "--pids-limit", str(self.sandbox.pids),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
            self.backend.image,
            *self.cli_args(prompt),
        ]

    async def start(self) -> None:
        if self._command_override is None:
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            try:
                await cli_domains.prepare(self.name, self.backend, self.app_id)
            except RuntimeError as e:
                raise CliBackendError(str(e)) from e
        target = self.config_file
        if target is not None and self.model_config is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.parser.config(self.model_config, self.base_url), encoding="utf-8")

    async def _turn(self, prompt: str) -> None:
        if self._admit is not None:
            await self._admit()
        self._spawn(self.command(prompt), os.environ.copy())

    async def initialize(self, prompt: str) -> None:
        first_prompt = prompt if self.backend_session_id else f"{self.system_prompt}\n\nUser task:\n{prompt}"
        await self._turn(first_prompt)

    def user_message(self, content: str) -> dict:
        return {"type": "print_mode_followup", "content": content}

    async def continue_followups(self) -> None:
        await self._turn(await self._take_followups())

    @property
    def steps(self) -> int:
        return self.translator.steps

    async def receive(self, timeout: float | None = None) -> str | dict | None:
        """The next stdout line for the translator, or an `exit` record once the turn's process has ended."""
        line = await self._next_line(timeout)
        if line is None:
            proc = self.process
            code = await asyncio.to_thread(proc.wait) if proc is not None else None
            for thread in self._threads:
                await asyncio.to_thread(thread.join, 0.5)
            return {"type": "exit", "code": code, "stderr": "".join(self._stderr).strip()[-2000:]}
        return line if isinstance(line, str) else None  # False: nothing within the timeout

    async def stop(self) -> None:
        await self._close_process(terminate=True)
        if self._command_override is None:
            await shared.run_cmd(["docker", "rm", "-f", self.container], timeout=30)
        target = self.config_file
        if target is not None:
            target.unlink(missing_ok=True)
