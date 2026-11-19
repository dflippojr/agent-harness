"""Unmodified provider CLIs used as session backends: what the backends share.

Each backend is an adapter in `harness/backends/<vendor>.py` (#991): a transport that owns the CLI's process and its
stdin/stdout, and a pure translator from the vendor's records to the neutral events in `harness/backends/events.py`,
which `Runner._apply` applies. This module keeps what they share: the container conventions, the error type, the
session's key and domain, the per-turn process of the print-mode CLIs, and the route to the local model. Provider
credentials and each CLI's own state stay in Docker volumes, one state volume per domain (Web or one App, see
cli_domains); nothing here opens those volumes or handles a login flow.

The transports and their constants used to live here. Their old names are re-exported (`__getattr__` below) for one
release; import them from `harness.backends.<vendor>`. The transports call `run_cmd` and `ready_domain` through this
module, so a test that patches them here still reaches every backend.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import claude_token, cli_domains, credential_sources, end_users, member_keys  # noqa: F401 - these register their sources
from .config import BackendConfig
from .sandbox import run_cmd

# The bind-mount target every provider CLI runs in, and the proxy env every container needs.
WORKSPACE = "/workspace"
NO_PROXY = "NO_PROXY=localhost,127.0.0.1"
# Keep this at runtime as well as in cli.Dockerfile so sessions still
# work if the daemon is briefly paired with an older cached image.
NODE_USE_ENV_PROXY = "NODE_USE_ENV_PROXY=1"
EMPTY_MCP_CONFIG = '{"mcpServers": {}}'


SPLIT_NOTE = ("\n\nYour shell and file tools are the harness tools mcp__harness__run_shell, read_file, write_file, "
              "edit_file, search and list_files. They act on the project workspace; this container has no "
              "workspace of its own.")
# Split mode under the bash-only tool profile (#638): the harness serves run_shell and no file tools.
BASH_ONLY_SPLIT_NOTE = ("\n\nYour only workspace tool is the harness tool mcp__harness__run_shell. It acts on the "
                        "project workspace; read, search and edit files with shell commands. This container has no "
                        "workspace of its own.")


def split_note(tool_profile: str) -> str:
    return BASH_ONLY_SPLIT_NOTE if tool_profile == "bash-only" else SPLIT_NOTE


def workspace_args(workspace: Path, split: bool) -> list[str]:
    """The docker flags for the workspace. In split mode (#427) the CLI container has none: it keeps the provider
    token and the state volume, and the files only exist in the session sandbox."""
    if split:
        return ["-w", WORKSPACE]
    return ["--mount", f"type=bind,source={workspace},target=/workspace", "-w", WORKSPACE]


class CliBackendError(Exception):
    """The provider CLI exited or stopped speaking valid JSONL. `code` is a stable failure code when there is one."""

    def __init__(self, message: str = "", code: str = ""):
        super().__init__(message)
        self.code = code


def session_key(end_user: str, api_key: str) -> str:
    """The API key a session runs on. An App's end user never runs on a key of the App's or the owner's (#365); a
    member's session runs on the member's own key and nothing else (#393)."""
    return api_key if not end_user or member_keys.member_of(end_user) else ""


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


class TurnProcess:
    """One print-mode process per turn (Cursor Agent, kind: cli backends): its reader threads, the follow-ups queued
    while it runs, and closing it. A subclass sets `process`, `_events`, `_stderr`, `_threads` and `_followups`."""

    def _spawn(self, argv: list[str], env: dict) -> None:
        loop = asyncio.get_running_loop()
        self._events = asyncio.Queue()
        self._stderr = []
        process = self._popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env,
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

    async def _next_line(self, timeout: float | None) -> str | None | bool:
        """The next stdout line, None once the process has ended, or False on a timeout."""
        try:
            return await asyncio.wait_for(self._events.get(), timeout) if timeout else await self._events.get()
        except asyncio.TimeoutError:
            return False

    async def send(self, item: dict) -> None:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str):
            raise CliBackendError("follow-up was not text")
        self._followups.append(content)

    @property
    def has_followups(self) -> bool:
        return bool(self._followups)

    async def _take_followups(self) -> str:
        """Close the finished turn and return the queued messages as the next turn's prompt."""
        prompts, self._followups = self._followups, []
        await self._close_process(terminate=False)
        if self._command_override is None:
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
        return "Messages received while the prior turn was running:\n\n" + "\n\n".join(prompts)

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


# --- config-declared print-mode CLIs (#168) -------------------------------------------------------------------------

def forwarder(backend: BackendConfig) -> str:
    """The container on a kind: cli backend's internal network that forwards one port to the host's llama-server
    (ops/cli-llm/compose.yaml). It is the network's only other member."""
    return f"{backend.network}-llm"


def forwarder_url(backend: BackendConfig, model_base_url: str) -> str:
    """The model's OpenAI-compatible URL as the CLI container reaches it: the forwarder, on the model's own scheme and
    port (it passes TCP through, so the CLI speaks to the server as the harness does). A URL without a port uses its
    scheme's default, as the installer's forwarder does."""
    parts = urlsplit(model_base_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return urlunsplit((parts.scheme, f"{forwarder(backend)}:{port}", "/v1", "", ""))


def cli_route_ready(backend: BackendConfig) -> bool:
    """Whether the backend's internal network exists and its forwarder runs (Q9: otherwise it is unavailable)."""
    def ok(args: list[str], expect: str = "") -> bool:
        try:
            result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and (not expect or result.stdout.strip() == expect)
    return (ok(["network", "inspect", "--format", "{{.Internal}}", backend.network], "true")
            and ok(["inspect", "--format", "{{.State.Running}}", forwarder(backend)], "true"))


# Names that moved to harness/backends/<vendor>.py (#991), re-exported for one release.
_MOVED = {
    **dict.fromkeys(("ClaudeSession", "ClaudeTranslator", "CLAUDE_SPLIT_TOOLS", "CLAUDE_BASH_ONLY_TOOLS",
                     "PROJECT_MEMORY_FILES", "PROJECT_MEMORY_LIMIT", "project_memory"), "claude"),
    **dict.fromkeys(("CodexSession", "CodexTranslator", "CODEX_TOOLS_ONLY_OVERRIDES", "CODEX_SPLIT_OVERRIDES",
                     "CODEX_TOOLS_ONLY_ITEMS", "ELICITATION"), "codex"),
    **dict.fromkeys(("CursorSession", "CursorTranslator"), "cursor"),
    **dict.fromkeys(("PrintModeSession", "PrintModeTranslator", "PRINT_MODE_PARSERS"), "print_mode"),
    "OpencodeJsonl": "opencode",
}


def __getattr__(name: str):
    module = _MOVED.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__package__}.backends.{module}"), name)
