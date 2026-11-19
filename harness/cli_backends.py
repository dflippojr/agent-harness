"""Unmodified provider CLIs used as session backends: what the backends share.

Each backend is an adapter in `harness/backends/<vendor>.py` (#991): a transport that owns the CLI's process and its
stdin/stdout, and a pure translator from the vendor's records to the neutral events in `harness/backends/events.py`,
which `Runner._apply` applies. This module keeps what they share: the container conventions, the error type, the
session's key and domain, the long-lived JSONL process (Claude Code, Codex), the per-turn process (Cursor and the
print-mode CLIs), and the route to the local model. Provider
credentials and each CLI's own state stay in Docker volumes, one state volume per domain (Web or one App, see
cli_domains); nothing here opens those volumes or handles a login flow.

The transports and their constants used to live here. Their old names are re-exported (`__getattr__` below) for one
release; import them from `harness.backends.<vendor>`. The transports call `run_cmd` and `ready_domain` through this
module, so a test that patches them here still reaches every backend.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from . import claude_token, cli_domains, credential_sources, end_users, member_keys  # noqa: F401 - these register their sources
from .config import BackendConfig, SandboxConfig
from .mcp_server import McpRelay, McpRelayError
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


def provider_run(session_id: str, container: str, network: str, proxy: str) -> list[str]:
    """`docker run` for a provider CLI: interactive, removed on exit, named and labelled for its session, on `network`
    with the backend's provider proxy."""
    return ["docker", "run", "--rm", "-i", "--name", container,
            "--label", f"agent-harness.session={session_id}",
            "--network", network,
            "-e", f"HTTPS_PROXY={proxy}",
            "-e", f"HTTP_PROXY={proxy}",
            "-e", NO_PROXY,
            "-e", NODE_USE_ENV_PROXY]


def container_limits(sandbox: SandboxConfig) -> list[str]:
    """A CLI container's memory, CPU and process limits."""
    return ["--memory", sandbox.memory, "--cpus", str(sandbox.cpus), "--pids-limit", str(sandbox.pids)]


# Every CLI container: no new privileges, and the capabilities a CLI never needs dropped.
HARDENING = ("--security-opt", "no-new-privileges", "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD",
             "--cap-drop", "AUDIT_WRITE")


def jsonl_record(line: str, label: str) -> dict:
    """One stdout line of a JSONL CLI as its record. Anything but a JSON object fails the run."""
    try:
        value = json.loads(line)
    except json.JSONDecodeError as e:
        raise CliBackendError(f"{label} wrote invalid JSONL: {line[:300].rstrip()}") from e
    if not isinstance(value, dict):
        raise CliBackendError(f"{label} JSONL event was not an object")
    return value


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


class JsonlProcess:
    """A long-lived CLI process that speaks JSONL on stdin and stdout (Claude Code, Codex's app-server): getting its
    container, domain and MCP relay ready, starting it with its reader threads, writing a record, reading the next
    one, and stopping it. `name` is the backend (its container and its domain), `label` the CLI in errors."""

    name = "cli"
    label = "CLI"

    def __init__(self, *, session_id: str, workspace: Path, backend: BackendConfig, sandbox: SandboxConfig,
                 system_prompt: str, model: str = "", backend_session_id: str = "", api_key: str = "",
                 popen: Callable = subprocess.Popen, command: list[str] | None = None, mcp: McpRelay | None = None,
                 mcp_token: str = "", tools_only: bool = False, app_id: str = "", end_user: str = "",
                 split: bool = False, tool_profile: str = "default"):
        self.session_id = session_id
        self.tool_profile = tool_profile  # "bash-only" (#638): each backend's command says what it leaves
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
        self.container = f"harness-{session_id}-{self.name}"
        # With MCP the CLI shares the relay's network namespace: the relay's loopback port is reachable from this
        # container only, and egress still goes through the same network and proxy (#373).
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
        raise NotImplementedError

    async def _prepare(self) -> None:
        """Before the process starts: remove this session's leftover container, ready its domain (refusing a login
        it may not use) and start its MCP relay."""
        if self._command_override is None:
            # The Windows daemon restart script force-stops Python. That skips
            # our finally block and can leave `docker run`'s container behind.
            # Remove only this session's deterministic container before reuse.
            await run_cmd(["docker", "rm", "-f", self.container], timeout=30)
            await ready_domain(self.name, self.backend, self.app_id, self.api_key, self.end_user)
            if self.mcp is not None:
                try:
                    await self.mcp.start()
                except McpRelayError as e:
                    raise CliBackendError(str(e)) from e

    def _open(self, env: dict) -> None:
        """Start the process with `env`, and the threads that queue its stdout lines and keep its stderr."""
        loop = asyncio.get_running_loop()
        process = self._popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env,
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

    async def _write(self, item: dict) -> None:
        def write() -> None:
            if self.process is None or self.process.stdin is None or self.process.poll() is not None:
                raise CliBackendError(f"{self.label} is not running")
            with self._write_lock:
                self.process.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
        try:
            await asyncio.to_thread(write)
        except OSError as e:
            raise CliBackendError(f"could not write to {self.label}: {e}") from e

    async def _read(self, timeout: float | None = None) -> dict | None:
        """The next record, or None when none came within `timeout`. A process that ended, or wrote something that is
        not a JSON object, fails the run."""
        try:
            line = await asyncio.wait_for(self._events.get(), timeout) if timeout else await self._events.get()
        except asyncio.TimeoutError:
            return None
        if line is None:
            code = self.process.poll() if self.process is not None else None
            error = "".join(self._stderr).strip()
            raise CliBackendError(f"{self.label} exited with code {code}: {error}".rstrip())
        return jsonl_record(line, self.label)

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
