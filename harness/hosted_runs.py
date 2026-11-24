"""The Runner's hosted half (#991): a hosted CLI session's run, from its admission to its end, and the one applier.

Every hosted backend is an adapter in `harness/backends/<vendor>.py`: a transport and a pure translator to the neutral
events in `harness/backends/events.py`. `HostedRuns` is what the Runner does with them, the same for every vendor:
admission under the member's or App's running cap, the backend slot and the credential, starting or resuming the CLI,
feeding its records through the translator to `_apply`, the approval path for its permission requests, rate-limit
stops and the API-key fallback, and the run's end. Nothing here reads a vendor's format. `Runner` mixes it in; the
state and helpers it shares with the native loop are declared below and defined in `harness/runner.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import claude_token, credential_sources, member_keys, taint, telemetry, tool_profiles
from .backend_state import billing_warning
from .backends.claude import ClaudeSession
from .backends.codex import CodexSession
from .backends.cursor import CursorSession
from .backends.events import (Assistant, Delta, Error, Event, PermissionRequest, RateLimits, Result, Session,
                              ToolResult, obj, result_of)
from .backends.print_mode import PrintModeSession
from .cli_backends import CliBackendError, forwarder_url
from .config import BUILTIN_BACKENDS, ModelConfig
from .mcp_server import MCP_BACKENDS
from .policy import ALLOW, ASK, DENY, TOOLS_ONLY, Decision, mcp_harness_tool
from .tools import truncate_middle

if TYPE_CHECKING:
    from .bus import EventBus
    from .config import Config
    from .db import Database
    from .mcp_server import McpServer, McpTokens
    from .policy import AppToolsPolicy, ChatPolicy, Policy
    from .scheduler import GpuScheduler, InferenceGate

Transport = ClaudeSession | CodexSession | CursorSession | PrintModeSession


class ToolsOnlyUnsupported(CliBackendError):
    """This backend can't be limited to an App's tools, so an App-tools-only session refuses to run on it (#329)."""


class CliLimitError(Exception):
    def __init__(self, reset_at: float = 0):
        self.reset_at = reset_at


DEVICE_KEY = "device_key"  # `_backend_credential` source: a device key's session on a built-in backend (#786)


def hosted_refused(key: dict | None, backend_cfg) -> bool:
    """Is a session under `key` refused `backend_cfg`? A key that is no App (a device's) never reaches a built-in hosted
    backend (#786); a kind: cli backend runs on the local model and keeps its own grant rule. `key` is None for the
    owner's own sessions. `create`, `send`, the backend list and the run itself all ask this, so they agree."""
    return key is not None and key.get("kind") != "app" and backend_cfg is not None and not backend_cfg.is_cli


class _HeldSlot:
    """A hosted backend slot held for one run, which can give it up while the run waits on an approval (#524)."""

    def __init__(self, sem: asyncio.Semaphore):
        self.sem, self.held = sem, False

    async def __aenter__(self):
        await self.sem.acquire()
        self.held = True
        return self

    async def __aexit__(self, *exc):
        if self.held:
            self.held = False
            self.sem.release()

    @contextlib.asynccontextmanager
    async def released(self, on_wait=None):
        """Release the slot for the body; take it back after, awaiting `on_wait()` first if it is taken. A body that
        raises leaves it released (and the exit above does not release it twice)."""
        if self.held:
            self.held = False
            self.sem.release()
        body_done = False
        try:
            yield
            body_done = True
        finally:
            if body_done:  # only after a body that returned: one that raised leaves the slot released
                if self.sem.locked() and on_wait is not None:
                    await on_wait()
                await self.sem.acquire()
                self.held = True


class HostedRuns:
    """The hosted CLI half of `Runner` (harness/runner.py)."""

    if TYPE_CHECKING:  # the Runner's state and helpers this half uses, defined in harness/runner.py
        cfg: Config
        db: Database
        bus: EventBus
        scheduler: GpuScheduler
        gate: InferenceGate
        guard: Any
        warmer: Any
        modules: Any
        member_keys: Any
        generating: set[str]
        admitted: dict[str, str]
        approval_events: dict[str, asyncio.Event]
        mcp_tokens: McpTokens
        mcp_server: McpServer
        mcp_relay_factory: Callable
        cli_factory: Callable
        codex_factory: Callable
        cursor_factory: Callable
        print_mode_factory: Callable
        cli_route_ready: Callable
        gpu_poll_seconds: float
        _recovery_unanswered: dict[str, set[str]]
        _cli_sessions: dict[str, Transport]
        _backend_slots: dict[str, asyncio.Semaphore]
        _held_slots: dict[str, _HeldSlot]
        _cli_turns: dict
        _mcp_grants: dict[str, list[dict]]
        _locks: credential_sources.KeyedLocks
        _app_waits: dict[str, int]

        async def aset_status(self, sid: str, status: str, **fields) -> None: ...
        def _status_writer(self, sid: str, status: str, fields: dict) -> Callable[[], None]: ...
        def _flag_end_pending(self, sid: str, status: str) -> None: ...
        async def _end_run(self, sid: str) -> None: ...
        async def _prepare_repo(self, s: dict) -> None: ...
        async def _memory_gate(self, sid: str, what: str, *, load: bool = False) -> None: ...
        def note_gpu_pause(self, sid: str) -> None: ...
        @staticmethod
        async def _model_reachable(model: ModelConfig) -> bool: ...
        def split_mode(self, s: dict) -> bool: ...
        def mcp_tool_schemas(self, sid: str) -> list[dict]: ...
        def _grant_mcp(self, sid: str, name: str, args: dict, call_id: str, approval_call_id: str = "") -> None: ...
        def policy(self, s: dict) -> Policy | ChatPolicy | AppToolsPolicy: ...
        def _taint_layer(self, s: dict, name: str, args: dict, decision) -> Any: ...
        def _add_taint(self, sid: str, kind: str, origin: str) -> None: ...
        def _context_banner(self, s: dict, name: str, tainted: bool = False) -> str: ...
        async def _ask_details(self, s: dict, name: str, args: dict, ws: Any,
                               reason: str) -> tuple[str, str, str | None]: ...
        async def _review_ask(self, s: dict, name: str, args: dict, decision) -> dict: ...
        def _persist_ask(self, sid: str, existing: dict, extra: dict) -> dict: ...
        async def _wait_approval(self, aid: str) -> dict: ...

    def _live(self, sid: str) -> dict:
        """Session `sid` while its hosted run is live: its row exists until the run ends (a missing one fails the
        run, as reading it would)."""
        s = self.db.sessions.get_session(sid)
        if s is None:
            raise TypeError(f"session {sid} is gone")
        return s

    async def _run_cli(self, sid: str, recovered: bool = False) -> None:
        s = self._live(sid)
        backend_name = s["backend"]
        backend = self.cfg.backends.get(backend_name)
        if backend is None or not (backend.is_cli or backend_name in BUILTIN_BACKENDS):
            raise CliBackendError(f"backend {backend_name!r} is not implemented")
        if recovered:
            await self.bus.aemit(sid, "resumed", {"status": s["status"]})
        await self._prepare_repo(s)
        slot = self._backend_slots[backend_name]
        while True:
            if recovered and sid not in self._recovery_unanswered:
                self._note_unanswered(sid)  # a CLI restarted within this daemon, or a run resumed without the Manager
            s = self._live(sid)
            if s["status"] == "waiting_limit":
                delay = max(0, float(s["run"].get("limit_resets_at") or 0) - time.time())
                if delay:
                    await asyncio.sleep(delay)
                await self.aset_status(sid, "queued")  # still holding its place (HOLDS_PLACE)
            backend_session_id = str(s["run"].get("backend_session_id") or "")
            credential, use_api_key, api_key = self._cli_credentials(sid, s, backend_name)
            await self._memory_gate(sid, f"{backend_name} worker container")
            held = _HeldSlot(slot)
            try:
                async with contextlib.AsyncExitStack() as stack:
                    await self._admit_hosted(sid, s, stack)
                    await stack.enter_async_context(held)
                    if self.admitted.get(sid) and self._live(sid)["status"] == "queued":
                        await self.aset_status(sid, "running")
                    self._unreserve(sid)  # holds its place from here on
                    self._held_slots[sid] = held
                    with telemetry.span("hosted_cli_turn", {"harness.backend": backend_name,
                                                            "gen_ai.request.model": backend.model}):
                        cli = await self._start_cli(sid, backend_name, backend, credential, use_api_key, api_key,
                                                    backend_session_id, recovered)
                        await self._pump_cli(sid, cli, credential, use_api_key, recovered)
                    return
            except CliLimitError as limit:
                await self._cli_limit_reached(sid, backend_name, limit, use_api_key)
                recovered = True
            finally:
                self._recovery_unanswered.pop(sid, None)
                self._held_slots.pop(sid, None)
                self._unreserve(sid)
                await self._release_cli_turn(sid)  # a local-model CLI turn's leases on the model (#168)

    async def _admit_hosted(self, sid: str, s: dict, stack: contextlib.AsyncExitStack) -> None:
        """Take the session's credential lock (onto `stack`) once its member or App is under its running cap (#524),
        and reserve its place under that cap: it counts against the cap while it waits for a backend slot, until its
        `running` status counts instead. The check and the reservation happen with no await between them, so two
        sessions of one member or App can't both pass the check while neither counts yet. Nothing is held while
        waiting on the cap, so a parked session that holds it can still come back; and a session waiting for the
        credential (another of the same end user's runs) holds no place under the cap."""
        from .runner import admission_key  # runner.py imports this module
        key = admission_key(self.db, s)
        while True:
            if key:
                await self.scheduler.wait_eligible(sid)
            attempt = contextlib.AsyncExitStack()
            await attempt.enter_async_context(self._credential_lock(s))
            if not key or self.scheduler.eligible(sid):
                if key:
                    self.admitted[sid] = key
                await stack.enter_async_context(attempt)
                return
            await attempt.aclose()  # the cap filled while it waited for the credential

    def _unreserve(self, sid: str) -> None:
        """Drop `sid`'s reservation under its cap; a session that could not run while it held one may now."""
        if self.admitted.pop(sid, None) is not None:
            self.scheduler.recheck()

    def _credential_lock(self, s: dict):
        """Held for a CLI attempt on a credential that two sessions must not use at once (#365 decision 5: one end
        user's sessions share one refresh token, so the next session queues until the running one ends). Taken before
        the backend's slot, so a queued session holds none."""
        end_user = s.get("end_user") or ""
        source = credential_sources.select(s.get("app_id") or "", end_user) if end_user else None
        key = source.lock_key(s["backend"], s.get("app_id") or "", end_user) if source else ""
        if not key:
            return contextlib.nullcontext()
        return self._locks.hold(key)

    def _cli_credentials(self, sid: str, s: dict, backend_name: str) -> tuple[dict, bool, str]:
        """The credential for this attempt, whether it is an API key, and the key itself."""
        credential = self._backend_credential(s)
        if credential["policy"] == "denied" and credential["source"] == "member_api_key":
            raise CliBackendError(f"add your API key in your settings to use {backend_name.title()}",
                                  member_keys.REQUIRED)
        if credential["policy"] == "denied" and credential["source"] == DEVICE_KEY:
            raise CliBackendError(f"device tokens cannot use {backend_name}; they run on the local model")
        if credential["policy"] == "denied":
            raise CliBackendError(f"{backend_name} credential policy was revoked for this app")
        use_api_key = credential["policy"] == "api_key" or s["run"].get("backend_auth") == "api_key"
        api_key = credential["key"] if use_api_key else ""
        if use_api_key and not api_key:
            self.bus.emit(sid, "backend_auth_required", {"backend": backend_name, "auth": "api_key"})
            raise CliBackendError(f"{backend_name} API-key credential is configured but no key is available")
        return credential, use_api_key, api_key

    async def _start_cli(self, sid: str, backend_name: str, backend, credential: dict, use_api_key: bool,
                         api_key: str, backend_session_id: str, recovered: bool):
        """Start (or resume) the hosted CLI for this attempt and record its billing mode."""
        s = self._live(sid)
        if s["status"] != "waiting_approval":
            await self.aset_status(sid, "running")
        source = (credential["source"] if use_api_key or credential["source"] == "end_user_login"
                  else "subscription")
        run = self.db.sessions.patch_run(sid, billing_mode="api_key" if use_api_key else backend.billing,
                                credential_source=source, credential_assignment=credential["assignment_id"]) or {}
        warning = billing_warning(backend, run.get("rate_limits"), using_api_key=use_api_key)
        if warning and not run.get("billing_warned"):
            self.db.sessions.patch_run(sid, billing_warned=True)
            await self.bus.aemit(sid, "billing_warning", {"backend": backend_name, "message": warning})
        end_user = s.get("end_user") or ""
        if backend_name == "claude" and not end_user and claude_token.uses_token(backend, s.get("app_id") or "", api_key):
            note = claude_token.reminder(backend.oauth_token_file)
            if note and not run.get("token_expiry_warned"):
                self.db.sessions.patch_run(sid, token_expiry_warned=True)
                await self.bus.aemit(sid, "billing_warning", {"backend": backend_name, "message": note})
        frozen = replace(backend, model=s["model"], effort=s.get("effort") or backend.effort)
        if backend.is_cli:
            factory, extra = self.print_mode_factory, await self._print_mode_extra(sid, s, backend_name, frozen)
        else:
            factory, extra = {"claude": self.cli_factory, "codex": self.codex_factory,
                              "cursor": self.cursor_factory}[backend_name], {}
        if not backend.is_cli and backend_name in MCP_BACKENDS and backend.mcp and self.mcp_tool_schemas(sid):
            extra = {"mcp": self.mcp_relay_factory(session_id=sid, backend=frozen, server=self.mcp_server),
                     "mcp_token": self.mcp_tokens.mint(sid)}
        if self.split_mode(s):
            # Never fall back to the CLI's own shell next to its token: without the MCP server the run refuses.
            if not extra:
                raise CliBackendError(f"{backend_name} split mode needs the harness MCP server, which is off for this "
                                      "session", "split_unavailable")
            extra["split"] = True
        if s.get("tool_profile", tool_profiles.DEFAULT) != tool_profiles.DEFAULT:
            # Checked at creation; checked again here because the backend's config can change while a session waits.
            # Never run the CLI with its full tools in place of the profile it was started with.
            reason = tool_profiles.refusal(backend_name, backend, s["tool_profile"], s.get("kind", "agent"))
            if reason or (backend_name == "codex" and not self.split_mode(s)):
                raise CliBackendError(reason or f"tool_profile {s['tool_profile']} needs Codex in split mode",
                                      tool_profiles.UNSUPPORTED)
            extra["tool_profile"] = s["tool_profile"]
        if s.get("kind") == TOOLS_ONLY:
            # Never run a hosted CLI with its built-in tools here: Claude Code gets --tools "", Codex no environment
            # and every other built-in tool off, and only the App's tools over MCP (the approval path still denies
            # anything else); a CLI without both refuses (#329, #373).
            if backend_name not in MCP_BACKENDS or not extra:
                raise ToolsOnlyUnsupported(f"{backend_name} can't reach App tools without its built-in tools")
            extra["tools_only"] = True
            Path(s["workspace"]).mkdir(parents=True, exist_ok=True)  # emptied at the end of every run
        cli = factory(session_id=sid, workspace=Path(s["workspace"]), backend=frozen,
                      sandbox=self.cfg.sandbox, system_prompt=s["context"][0]["content"],
                      model=s["model"], backend_session_id=backend_session_id, api_key=api_key,
                      app_id=s.get("app_id") or "", **({"end_user": end_user} if end_user else {}), **extra)
        self._cli_sessions[sid] = cli
        await cli.start()
        prompt = ("The harness restarted; continue the task." if recovered else
                  next((m["content"] for m in reversed(s["context"][1:])
                        if m.get("role") == "user" and isinstance(m.get("content"), str)), ""))
        await cli.initialize(prompt)
        if cli.backend_session_id and cli.backend_session_id != backend_session_id:
            self.db.sessions.patch_run(sid, backend_session_id=cli.backend_session_id)
        return cli

    async def _print_mode_extra(self, sid: str, s: dict, name: str, backend) -> dict:
        """What a kind: cli session needs besides the common arguments: its local model, the forwarder's URL, where
        its generated config goes, and the per-turn admission. Refuses when the route to llama-server is missing."""
        model = self.cfg.models.get(s["model"])
        if model is None:
            raise CliBackendError(f"{name} runs on the local model; {s['model']!r} is not in models",
                                  "model_unavailable")
        if s.get("end_user"):
            raise CliBackendError(f"{name} has no per-person login")
        if not await asyncio.to_thread(self.cli_route_ready, backend):
            raise CliBackendError(f"{name} can't reach the local model: the {backend.network} network or its "
                                  "llama-server forwarder is missing (ops/cli-llm/compose.yaml)", "backend_unavailable")
        return {"name": name, "model_config": model, "base_url": forwarder_url(backend, model.base_url),
                "config_dir": self.cfg.data_dir / "cli-config" / name,
                "admit": lambda: self._admit_local_cli_turn(sid, model)}

    async def _admit_local_cli_turn(self, sid: str, model: ModelConfig) -> None:
        """Before each turn of a CLI that runs on the local model (#168 Q7, Q10): the GPU hold, RAM and model load a
        queued native turn waits on, through the local-model module. Without that module, the server must answer."""
        await self._release_cli_turn(sid)  # between turns it holds no lease, so a hold isn't kept waiting on it
        admit = self.modules.local_turn_admission() if self.modules is not None else None
        while True:
            if admit is None:
                if not await self._model_reachable(model):
                    raise CliBackendError(f"the local model server at {model.base_url} isn't reachable",
                                          "model_unavailable")
            else:
                refusal = await admit(sid, model)
                if refusal:
                    raise CliBackendError(*refusal)
            # The whole turn is one agent turn on the inference gate: an image job (exclusive) waits for it instead
            # of stopping llama-server under it. A hold or an unload while it waited for the gate: admit again.
            turn = await self.gate.agent_turn()
            if (self.guard is None or not self.guard.active) and not self.warmer.parked(model):
                break
            await turn.release()
            self.generating.discard(sid)
        self._cli_turns[sid] = turn
        if self._live(sid)["status"] == "queued":
            await self.aset_status(sid, "running")

    async def _release_cli_turn(self, sid: str) -> None:
        self.generating.discard(sid)
        turn = self._cli_turns.pop(sid, None)
        if turn is not None:
            await turn.release()

    async def wait_gpu_clear(self, sid: str) -> None:
        """Wait in `queued` while the GPU guard holds the GPU; the session hears gpu_paused and gpu_resumed."""
        if self.guard is None or not self.guard.active:
            return
        if self._live(sid)["status"] != "queued":
            await self.aset_status(sid, "queued")
        self.note_gpu_pause(sid)
        while self.guard is not None and self.guard.active:
            await asyncio.sleep(self.gpu_poll_seconds)

    def _check_cli_credential(self, credential: dict, use_api_key: bool) -> None:
        if not credential["assignment_id"]:
            return
        current = self.db.credentials.app_provider_credential_by_id(credential["assignment_id"])
        if current is None or current.get("revoked_at") is not None:
            raise CliBackendError("the app provider credential was revoked")
        if use_api_key and self._secret_marker(credential["path"]) != credential["marker"]:
            raise CliBackendError("the app provider credential file changed")

    async def _pump_cli(self, sid: str, cli, credential: dict, use_api_key: bool, recovered: bool) -> None:
        """Feed the CLI's records through its translator to the applier until the run is complete."""
        while True:
            self._check_cli_credential(credential, use_api_key)
            await self._send_cli_inbox(sid, cli)
            record = await cli.receive(timeout=0.05)
            if record is None:
                continue
            for event in cli.translator.translate(record):
                if await self._apply(sid, cli, event, recovered):
                    await self._end_run(sid)
                    return

    async def _cli_limit_reached(self, sid: str, backend_name: str, limit: CliLimitError,
                                 use_api_key: bool) -> None:
        await self._stop_cli(sid)
        s = self._live(sid)
        credential = self._backend_credential(s)
        if credential["policy"] == "subscription_then_api_key" and credential["key"] and not use_api_key:
            def fall_back() -> None:  # queued for a slot again: that wait is not its running time (#524)
                self.db.sessions.patch_run(sid, backend_auth="api_key")
                self._status_writer(sid, "queued", {})()
                self.bus.emit(sid, "backend_fallback", {"backend": backend_name, "auth": "api_key"})
            await self.db.for_session(sid).awrite(fall_back)
            return
        reset = limit.reset_at or time.time() + 300

        def wait_for_limit() -> None:  # one commit, so nobody sees waiting_limit without when it resets
            self.db.sessions.patch_run(sid, limit_resets_at=reset)
            self._status_writer(sid, "waiting_limit", {})()
            self.bus.emit(sid, "limit_waiting", {"backend": backend_name, "resets_at": reset})
        await self.db.for_session(sid).awrite(wait_for_limit)

    @staticmethod
    def _secret_marker(path: str) -> tuple[int, int] | None:
        try:
            stat = Path(path).stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    @staticmethod
    def _read_secret(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8").strip() if path else ""
        except OSError:
            return ""

    def _backend_credential(self, s: dict) -> dict:
        backend = self.cfg.backends[s["backend"]]
        app_id = s.get("app_id") or ""
        assignment = self.db.credentials.app_provider_credential(app_id, s["backend"]) if app_id else None
        member = member_keys.member_of(s.get("end_user") or "") if not app_id else ""
        if app_id and hosted_refused(self.db.credentials.get_api_key(app_id), backend):
            # A device key's session never runs on a built-in hosted backend (#786), not even one it started before
            # `create` refused that, when it is resumed.
            return {"policy": "denied", "key": "", "source": DEVICE_KEY, "assignment_id": "", "path": "",
                    "marker": None}
        if member:
            # A household member's own API key (#393), whatever the owner has configured: never the owner's login,
            # token or key. Without a key the credential is denied and the session refused.
            key = self.member_keys.get(member, s["backend"]) if self.member_keys is not None else ""
            return {"policy": "api_key" if key else "denied", "key": key, "source": "member_api_key",
                    "assignment_id": "", "path": "", "marker": None}
        if s.get("end_user"):
            # The person's own subscription login, whatever the App or the owner has configured (#365): never a key,
            # a fallback to a key or the owner's token. An App the owner has not granted this backend is still denied.
            granted = not (assignment is None and self.db.credentials.app_provider_managed(app_id)) and (
                assignment is None or assignment["policy"] != "denied")
            return {"policy": "subscription" if granted else "denied", "key": "", "source": "end_user_login",
                    "assignment_id": assignment["id"] if assignment else "", "path": "", "marker": None}
        if assignment is not None:
            path = self.cfg.provider_secret_files.get(assignment["secret_ref"], "")
            return {"policy": assignment["policy"], "key": self._read_secret(path),
                    "source": "app_file" if assignment["secret_ref"] else "subscription",
                    "assignment_id": assignment["id"], "path": path, "marker": self._secret_marker(path)}
        if app_id and (backend.is_cli or self.db.credentials.app_provider_managed(app_id)):  # kind: cli needs a grant (#168)
            return {"policy": "denied", "key": "", "source": "app_file", "assignment_id": "",
                    "path": "", "marker": None}
        path = backend.api_key_file
        return {"policy": backend.auth, "key": self._read_secret(path), "source": "user_file",
                "assignment_id": "", "path": path, "marker": self._secret_marker(path)}

    async def _send_cli_inbox(self, sid: str, cli: Transport) -> None:
        """Forward messages received during a CLI run without racing a newer inbox append."""
        queued = list(self._live(sid)["inbox"])
        if not queued:
            return
        for content in queued:
            await cli.send(cli.user_message(content))

        def drain_inbox() -> None:
            current = self._live(sid)["inbox"]
            remaining = current[len(queued):] if current[:len(queued)] == queued else current
            self.db.sessions.update_session(sid, inbox=remaining)
        await self.db.for_session(sid).awrite(drain_inbox)

    def _bind_backend_session(self, sid: str, cli, backend_session_id: str) -> None:
        self.db.sessions.patch_run(sid, backend_session_id=backend_session_id)
        cli.backend_session_id = backend_session_id

    async def _apply(self, sid: str, cli, event: Event, recovered: bool = False) -> bool:
        """Apply one neutral event from a hosted CLI's translator (#991): the one place that records what a CLI did,
        asks the approval policy and stops on a limit, whatever the vendor. Returns true when the run is complete."""
        match event:
            case Delta():
                self.bus.ephemeral(sid, "delta", {"kind": event.kind, "text": event.text})
            case Assistant():
                self._emit_assistant(sid, event.content,
                                     [self._tool_call_entry(c.id, c.name, c.args) for c in event.tool_calls])
            case ToolResult():
                self._record_cli_tool_result(sid, event.id, event.name, event.ok, event.output, event.seconds)
            case Session():
                if event.id and event.id != cli.backend_session_id:
                    self._bind_backend_session(sid, cli, event.id)
            case PermissionRequest():
                await self._permission_request(sid, cli, event, recovered)
            case RateLimits():
                self._rate_limits(sid, event)
            case Result():
                return await self._cli_result(sid, cli, event)
            case Error():
                raise CliBackendError(event.message)
        return False

    async def _permission_request(self, sid: str, cli, event: PermissionRequest, recovered: bool) -> None:
        if not event.decidable:  # nobody is there to fill in a server's form
            await cli.respond_permission(event.request_id, "deny", {})
            return
        request = {"tool_name": event.tool, "input": event.args, "tool_use_id": event.call_id,
                   "description": event.description}
        await self._authorize_cli(sid, cli, event.request_id, request, recovered=recovered)

    def _rate_limits(self, sid: str, event: RateLimits) -> None:
        """Keep the provider's latest windows on the run and the backend, and stop the run at its limit, or at the
        backend's `stop_at_utilization`, until the window resets."""
        s = self._live(sid)
        backend = self.cfg.backends[s["backend"]]
        self.db.sessions.patch_run(sid, rate_limits=event.info)
        self.db.usage.set_backend_usage(s["backend"], event.info)
        self.bus.emit(sid, "rate_limit", event.info)
        if event.billing:
            warning = billing_warning(backend, event.info, using_api_key=s["run"].get("billing_mode") == "api_key")
            if warning:
                self.bus.emit(sid, "billing_warning", {"backend": s["backend"], "message": warning})
        stop = float(backend.stop_at_utilization or 0)
        if event.limit_reached or (stop and float(event.info.get("utilization") or 0) >= stop):
            raise CliLimitError(event.resets_at)

    async def _cli_result(self, sid: str, cli, result: Result) -> bool:
        """The CLI's turn ended: wait out a usage limit, run the messages queued meanwhile as the next turn (the
        turn-based CLIs), or finish the run."""
        if result.limit_reached:
            latest = self._live(sid)["run"].get("rate_limits") or {}
            raise CliLimitError(float(latest.get("resetsAt") or 0))
        if getattr(cli, "has_followups", False):
            await cli.continue_followups()
            return False
        if result.show_answer and result.answer:
            self._emit_assistant(sid, result.answer)
        self._finish_cli_result(sid, result)
        return True

    def _emit_assistant(self, sid: str, content: str = "", tool_calls: list | None = None) -> None:
        self.bus.emit(sid, "assistant", {"content": content, "reasoning": "", "tool_calls": tool_calls or [],
                                           "finish_reason": "", "prompt_tokens": 0, "completion_tokens": 0,
                                           "prompt_tps": 0, "gen_tps": 0})

    @staticmethod
    def _tool_call_entry(call_id: str, name: str, args: dict) -> dict:
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    def _record_cli_tool_result(self, sid: str, call_id: str, name: str, ok: bool, output: str,
                                seconds: float = 0) -> None:
        bare = mcp_harness_tool(name)
        if ok and bare:  # mcp_call's banner is for the model only: the event keeps the plain result (#653)
            # `tainted`: a session_search result's banner, when it has one (#798); its text never opens with it
            output = output.removeprefix(self._context_banner(self._live(sid), bare, tainted=True))

        def record_cli_tool_result() -> None:  # the counters are read and bumped in one transaction
            run = self._live(sid)["run"]
            counts = {"tool_calls": run.get("tool_calls", 0) + 1}
            if not ok:
                counts["tool_errors"] = run.get("tool_errors", 0) + 1
            self.db.sessions.patch_run(sid, **counts)
            self.bus.emit(sid, "tool_result", {"id": call_id, "name": name, "ok": ok, "seconds": seconds,
                                               "output_chars": len(output),
                                               "output": truncate_middle(output, 20000)})
        self.db.for_session(sid).write(record_cli_tool_result)

    async def _authorize_cli(self, sid: str, cli: ClaudeSession | CodexSession, request_id, request: dict,
                              recovered: bool = False) -> None:
        name = str(request.get("tool_name") or "")
        args = obj(request, "input")
        call_id = str(request.get("tool_use_id") or request_id)
        s = self._live(sid)
        existing = self.db.sessions.approval_for_exact_call(sid, call_id, name, args)
        if existing is None and recovered and s["status"] == "waiting_approval":
            existing = self._recovery_approval(sid, name, args)
        if existing is None:
            existing = await self._ask_cli_policy(s, cli, request_id, request, name, args, call_id)
            if existing is None:
                return
        if existing["status"] == "pending":
            await self.aset_status(sid, "waiting_approval")
            held = self._held_slots.get(sid)
            # Another session gets the backend slot while this one waits on a person (#524).
            # Decided while the slot is taken: it waits for one as `queued`, still holding its place (HOLDS_PLACE).
            async with (held.released(lambda: self.aset_status(sid, "queued")) if held is not None
                        else contextlib.nullcontext()):
                existing = await self._wait_approval(existing["id"])
        await self.aset_status(sid, self._hosted_status(sid))
        if existing["status"] == "approved":
            await self._allow_cli(sid, cli, request_id, name, args, call_id, existing.get("tool_call_id") or "")
            return
        note = f" User note: {existing['note']}" if existing.get("note") else ""
        await cli.respond_permission(request_id, "deny", args, f"The user denied this {name} call.{note}")

    def _note_unanswered(self, sid: str) -> None:
        """Before a recovered run's CLI starts: the approvals still pending are the ones the restart left unanswered."""
        self._recovery_unanswered[sid] = {approval["id"] for approval in self.db.sessions.pending_approvals(sid)}

    def _recovery_approval(self, sid: str, name: str, args: dict | None) -> dict | None:
        """The approval a regenerated CLI request binds to after a restart, or None to ask anew. Claude regenerates
        the interrupted tool request with a new tool_use_id after --resume; it binds to the one durable approval with
        identical semantics so the user sees and decides it once. Only an approval the restart left unanswered, that
        no live request waits on, can bind; it keeps a decision made since (the owner may answer the card before the
        request comes back) and binds once. The same command asked and decided before the restart is history (#925)."""
        unanswered = self._recovery_unanswered.get(sid, set())
        matches = [approval for approval in self.db.sessions.approvals(sid)
                   if approval["id"] in unanswered and approval["id"] not in self.approval_events
                   and approval["tool"] == name and approval["args"] == args]
        if len(matches) != 1:
            return None
        unanswered.discard(matches[0]["id"])
        return matches[0]

    def _hosted_status(self, sid: str) -> str:
        """A hosted session's status while it runs: parked on an approval or an App's reply while any is pending (its
        clock stops), else `running`. Its MCP calls and permission requests can overlap (#524)."""
        if self.db.sessions.pending_approvals(sid):
            return "waiting_approval"
        return "waiting_app" if self._app_waits.get(sid) else "running"

    async def _ask_cli_policy(self, s: dict, cli, request_id, request: dict, name: str, args: dict,
                              call_id: str) -> dict | None:
        """Apply the policy to a new CLI tool request: answer it now (None) or persist an approval to wait on."""
        sid = s["id"]
        bare = mcp_harness_tool(name)  # a harness tool over MCP is taint-checked and previewed as the native tool
        decision = self._taint_layer(s, bare or name, args, self.policy(s).decide(name, args))
        if bare is not None and bare not in {t["function"]["name"] for t in self.mcp_tool_schemas(sid)}:
            decision = Decision(DENY, f"{bare} is not a harness tool this session can use over MCP")
        await self.bus.aemit(sid, "tool_call", {"id": call_id, "name": name, "args": args,
                                         "decision": decision.action, "reason": decision.reason})
        if decision.action == ALLOW:
            await self._allow_cli(sid, cli, request_id, name, args, call_id)
            return None
        if decision.action != ASK:
            reason = decision.reason or "not allowed"
            await cli.respond_permission(request_id, "deny", args,
                                         f"Blocked by harness policy: {reason}. Don't retry this.")
            return None
        detail, reason = str(request.get("description") or ""), decision.reason
        if bare is not None:
            # The memory library only applies a change whose approved detail carries its exact diff.
            detail, reason, error = await self._ask_details(s, bare, args, None, decision.reason)
            if error is not None:
                await cli.respond_permission(request_id, "deny", args, error)
                return None
        existing = {"id": "a-" + uuid.uuid4().hex[:8], "session_id": sid, "tool_call_id": call_id,
                    "tool": name, "args": args, "reason": reason, "detail": detail}
        extra = await self._review_ask(s, name, args, decision)
        return self._persist_ask(sid, existing, extra)

    async def _allow_cli(self, sid: str, cli, request_id, name: str, args: dict, call_id: str,
                         approval_call_id: str = "") -> None:
        self._taint_allowed_cli_request(sid, name, args)
        self._grant_mcp(sid, name, args, call_id, approval_call_id)
        await cli.respond_permission(request_id, "allow", args)

    def _taint_allowed_cli_request(self, sid: str, name: str, args: dict) -> None:
        """Taint for a hosted CLI web request at the moment it is allowed to run. The CLI's tool_result only carries
        the call id, not the URL, so unlike the daemon path (which taints on a successful result) this taints on
        allow; a denied request never ran and leaves the session untouched."""
        source = taint.source_for(name, args)
        if source is not None:
            self._add_taint(sid, *source)

    def _finish_cli_result(self, sid: str, result: Result | dict) -> None:
        """Commit the run's end from the CLI's result (a Result, or a result record in Claude Code's shape): its usage
        on the run, the session totals and the usage log, and its final status, in one transaction."""
        if isinstance(result, dict):
            result = result_of(result)
        usage = result.usage
        prompt_tokens = sum(int(usage.get(key) or 0) for key in
                            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        completion_tokens = int(usage.get("output_tokens") or 0)
        status, reason = ("failed", "provider_error") if result.is_error else ("done", "final_message")

        def finish_cli_result() -> None:
            s = self._live(sid)
            fields = {"turns": result.turns, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "usage": usage, "total_cost_usd": result.cost, "executing": None}
            failure = None
            if result.is_error:
                failure = {"code": "provider_error", "provider": s["backend"],
                           "message": result.answer or result.subtype or "provider failed", "retryable": True}
                fields["failure"] = failure
            run = self.db.sessions.patch_run(sid, **fields) or {}
            totals = dict(s["totals"])
            totals["turns"] = totals.get("turns", 0) + result.turns
            totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + prompt_tokens
            totals["completion_tokens"] = totals.get("completion_tokens", 0) + completion_tokens
            totals["total_cost_usd"] = round(float(totals.get("total_cost_usd", 0)) + result.cost, 10)
            self.db.sessions.update_session(sid, totals=totals, status=status, stop_reason=reason, answer=result.answer)
            self._flag_end_pending(sid, status)
            self.db.usage.record_usage(
                s["backend"], sid, s.get("app_id", ""), prompt_tokens, completion_tokens, result.cost,
                str(run.get("billing_mode") or self.cfg.backends[s["backend"]].billing),
                str(run.get("credential_source") or "subscription"), s.get("end_user") or "",
                s.get("tool_profile") or tool_profiles.DEFAULT)
            if failure is not None:
                self.bus.emit(sid, "error", failure)
            self.bus.emit(sid, "status", {"status": status, "stop_reason": reason, "answer": result.answer})
        self.db.for_session(sid).write(finish_cli_result)

    async def _stop_cli(self, sid: str) -> None:
        self.mcp_tokens.revoke(sid)
        self._mcp_grants.pop(sid, None)
        cli = self._cli_sessions.pop(sid, None)
        if cli is not None:
            await cli.stop()
