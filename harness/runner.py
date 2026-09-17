"""The agent loop.

The loop is re-entrant: every step reads the session from SQLite and commits its result before the next step,
so after a daemon restart `run()` picks up wherever the session stopped (a model call, a tool call waiting for
approval, or a tool call that was interrupted mid-run).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

from . import compaction, grounding, llm, projects
from .backend_state import billing_warning
from .bus import EventBus
from .cli_backends import ClaudeSession, CliBackendError, CodexSession, CursorSession
from .config import Config
from .db import Database
from .homelab import Homelab
from .principal import OWNER_USER_ID, session_user_id
from .policy import ALLOW, ASK, Policy
from .remote import RemoteSandbox, RemoteWorkspace, RunnerError, RunnerHub
from .sandbox import Sandbox, SandboxUnavailable
from .scheduler import GpuScheduler, InferenceGate
from .fileops import dir_size  # noqa: F401 - re-exported for maintenance
from .tools import ToolError, Workspace, truncate_middle, validate_args
from .warmup import EXPECTED_WAKE_SECONDS, SLEEPING, WAKING, ModelWarmer

log = logging.getLogger("harness.runner")


class CliLimitError(Exception):
    def __init__(self, reset_at: float = 0):
        self.reset_at = reset_at

SYSTEM_PROMPT = """You are a software agent working in a project workspace on the user's home server.
You act only through the provided tools. File paths are relative to the workspace root (/workspace in the sandbox).
Shell commands run in a Linux container with Python 3.12, pytest, and git, and no network access. Commands that need the network (package installs, downloads) must set network: true; the user approves those, and may also be asked to approve pushes and deletions. If the user denies an action, don't retry it: find another way or explain what you need.

Work methodically: look around before editing, prefer `search` over reading large files in full, and verify changes by running the relevant command or tests. On long tasks older conversation may be condensed, so keep intermediate results and progress in `update_notes`.
When the task is complete, reply with your final answer (or call `finish`). Don't answer until the work is done and verified. The user often reads answers on a phone, so lead with the result."""

REPO_PROMPT = """Project repository: `{repo_name}` is checked out at /workspace on branch `{branch}`, created from `{base_branch}`. Commit your work to this branch with clear messages. Don't switch branches and don't push: when the run ends the harness saves the branch (committing anything left uncommitted), and the user reviews and merges it. `origin/{base_branch}` is refreshed from the source at the start of every run; if the user asks you to catch up, merge it into your branch."""

MAC_SYSTEM_PROMPT = """You are a software agent working in a project workspace on the user's MacBook (macOS, Apple silicon). You run on the user's home server and act only through the provided tools, which run on the MacBook.
File paths are relative to the workspace root, which is {workspace} on the Mac; shell commands start there. They run natively with bash, using the Mac's own toolchains (git, Homebrew's python3, Swift, Java; use `which` before assuming anything else is installed). They run in a sandbox: writes are limited to the workspace, temp directories and build caches, personal folders and credentials are unreadable, and there is no network access. Commands that need the network (package installs, downloads) must set network: true; the user approves those, and may also be asked to approve pushes and deletions. If the user denies an action, don't retry it: find another way or explain what you need. The MacBook can go to sleep; if a tool call takes a while to start, it's waiting for the Mac to wake.

Work methodically: look around before editing, prefer `search` over reading large files in full, and verify changes by running the relevant command or tests. On long tasks older conversation may be condensed, so keep intermediate results and progress in `update_notes`.
When the task is complete, reply with your final answer (or call `finish`). Don't answer until the work is done and verified. The user often reads answers on a phone, so lead with the result."""

MAC_REPO_PROMPT = """Project repository: `{repo_name}` is checked out in the workspace (a separate clone of the user's repository, so their own checkout is never touched) on branch `{branch}`, created from `{base_branch}`. Commit your work to this branch with clear messages. Don't switch branches, don't change git config, and don't push: when the run ends the harness saves the branch (committing anything left uncommitted), and the user reviews and merges it. `origin/{base_branch}` is refreshed from the source at the start of every run; if the user asks you to catch up, merge it into your branch."""

HOMELAB_PROMPT = """Homelab access: you can inspect the allowlisted services on this server with homelab_services, container_logs, read_service_config, and prometheus_query, ask to restart one with restart_service, and, after a code or Dockerfile change has been merged into a stack, ask to rebuild it with rebuild_service (the user approves restarts and rebuilds). These run on the host; the Linux sandbox can't reach Docker or the services. Diagnose from state and logs before proposing a restart, and afterwards check that the service stayed up."""

ACTIVE = ("queued", "running", "waiting_approval", "waiting_target", "waiting_app", "waiting_limit")
INTERRUPTED = ("Error: the daemon restarted while this tool call was running, so its effects are unknown. "
               "Check the workspace state before retrying.")
QUOTA_CHECK_SECONDS = 30
PROGRESS_MIN_TOKENS = 4000   # show prompt-reading progress only when this much of the prompt isn't cached


def new_run(carry: dict | None = None) -> dict:
    """Counters for one run. Notes and the token calibration belong to the session, so they carry over."""
    run = {"turns": 0, "tool_calls": 0, "invalid_tool_calls": 0, "tool_errors": 0, "prompt_tokens": 0,
           "completion_tokens": 0, "idle": 0, "executing": None, "started_at": time.time()}
    for key in ("notes", "chars_per_token", "backend_session_id", "rate_limits"):
        if carry and key in carry:
            run[key] = carry[key]
    return run


def unresolved_calls(context: list[dict]) -> list[dict]:
    i = len(context) - 1
    while i >= 0 and context[i]["role"] == "tool":
        i -= 1
    if i < 0 or context[i]["role"] != "assistant" or not context[i].get("tool_calls"):
        return []
    done = {m.get("tool_call_id") for m in context[i + 1:]}
    return [c for c in context[i]["tool_calls"] if c["id"] not in done]


class Runner:
    def __init__(self, cfg: Config, db: Database, bus: EventBus, scheduler: GpuScheduler, chat=llm.chat,
                 warmer: ModelWarmer | None = None, hub: RunnerHub | None = None):
        self.cfg = cfg
        self.hub = hub or RunnerHub(cfg.runners)
        self.warmer = warmer or ModelWarmer()
        self.db = db
        self.bus = bus
        self.scheduler = scheduler
        self.chat = chat
        self.approval_events: dict[str, asyncio.Event] = {}
        self.user_cancelled: set[str] = set()
        self._sandboxes: dict[str, Sandbox] = {}
        self._cli_sessions: dict[str, ClaudeSession | CodexSession | CursorSession] = {}
        self._backend_slots = {name: asyncio.Semaphore(max(1, backend.max_sessions))
                               for name, backend in cfg.backends.items()}
        self.cli_factory = ClaudeSession
        self.codex_factory = CodexSession
        self.cursor_factory = CursorSession
        self._quota_checked: dict[str, float] = {}
        self.guard = None                       # gpu_guard.GpuGuard, set by the manager when enabled
        self.generating: set[str] = set()       # sessions with a model call in flight (the guard waits for them)
        self.gpu_paused_sessions: set[str] = set()
        self.memory = None                      # memory_library.MemoryLibrary, set by the manager when enabled
        self.web = None                         # web_tools.WebTools, set by the manager when enabled
        self.images = None                      # images.ImageService, set by the manager when enabled
        self.sessions = None                    # search.SessionSearch, set by the manager when enabled
        self.remote_control = None              # remote_control.RemoteControl, set by the manager when enabled
        self.app_tools = None                   # apps.AppToolBroker, set by the manager
        self.last_completion: dict = {}         # tok/s of the latest model turn, for /metrics
        self.gate = InferenceGate()             # shared with the inference endpoint (endpoint.py)

    # helpers
    def sandbox(self, s: dict) -> Sandbox | RemoteSandbox:
        if s["target"] != "tower":
            return RemoteSandbox(self.hub, s["target"], s["id"])
        if s["id"] not in self._sandboxes:
            project = self.project_for(s)
            sb_cfg = self.cfg.sandbox
            if project and project.sandbox:
                sb_cfg = type(sb_cfg)(**{**sb_cfg.__dict__, **project.sandbox})
            self._sandboxes[s["id"]] = Sandbox(s["id"], Path(s["workspace"]), sb_cfg)
        return self._sandboxes[s["id"]]

    def project_for(self, s: dict):
        from . import catalog
        return catalog.get_project(self.cfg, self.db, session_user_id(s), s.get("project") or "")

    def workspace(self, s: dict) -> Workspace | RemoteWorkspace:
        model = self.cfg.models[s["model"]]
        if s["target"] != "tower":
            return RemoteWorkspace(self.hub, s["target"], s["id"], model.context_tokens)
        project = self.project_for(s)
        homelab = Homelab(self.cfg.homelab) if project and project.homelab and session_user_id(s) == OWNER_USER_ID else None
        from . import storage
        user_id = session_user_id(s)
        repos = storage.repos_dir(self.cfg, user_id)
        member = user_id != OWNER_USER_ID
        return Workspace(Path(s["workspace"]), self.sandbox(s), repos, model.context_tokens, homelab,
                         public_clone_only=member)

    def daemon_toolkits(self, s: dict) -> list:
        """Tools that run in the daemon for every target (memory library, web, session search), as enabled for the
        project."""
        project = self.project_for(s)
        member = session_user_id(s) != OWNER_USER_ID
        kits = []
        if not member and self.memory is not None and (project is None or project.memory_library):
            kits.append(self.memory)
        if self.web is not None and (project is None or project.web):
            kits.append(self.web)
        if not member and self.images is not None and (project is None or project.images):
            kits.append(self.images)
        if self.sessions is not None and (project is None or project.session_search):
            kits.append(self.sessions)
        if (not member and self.remote_control is not None and s["target"] == "tower"
                and not s.get("app_id")):
            kits.append(self.remote_control)  # not for app sessions: apps launch through /api/v1/remote-control
        return kits

    def tool_schemas(self, s: dict, ws) -> list[dict]:
        schemas = ws.schemas()
        for kit in self.daemon_toolkits(s):
            schemas = schemas + kit.schemas()
        if self.app_tools is not None and s.get("app_tools") and session_user_id(s) == OWNER_USER_ID:
            schemas = schemas + self.app_tools.schemas(s)
        return schemas

    def policy(self, s: dict) -> Policy:
        project = self.project_for(s)
        return Policy(project.rules if project else [], repo=bool(project and project.repo))

    def quota_mb(self, s: dict) -> int:
        project = self.project_for(s)
        default = (self.cfg.runners[s["target"]].workspace_quota_mb if s["target"] in self.cfg.runners
                   else self.cfg.cleanup.workspace_quota_mb)
        return (project.quota_mb if project and project.quota_mb else 0) or default

    def set_status(self, sid: str, status: str, **fields) -> None:
        with self.db.tx():
            self.db.update_session(sid, status=status, **fields)
            self.bus.emit(sid, "status", {"status": status, **{k: v for k, v in fields.items()
                                                                 if k in ("stop_reason", "answer")}})

    async def _acquire(self, sid: str, front: bool = False) -> None:
        if self.scheduler.holder == sid:
            return
        s = self.db.get_session(sid)
        if s["status"] != "queued":
            self.set_status(sid, "queued")
        if self.guard is not None and self.guard.active:
            self.note_gpu_pause(sid)
        await self.scheduler.acquire(sid, front=front)
        self.set_status(sid, "running")

    # GPU contention (gpu_guard.py)
    def note_gpu_pause(self, sid: str) -> None:
        """Tell a session (and the phone) that the GPU is paused. Once per pause."""
        if sid in self.gpu_paused_sessions or self.guard is None:
            return
        from .gpu_guard import describe
        self.gpu_paused_sessions.add(sid)
        self.bus.emit(sid, "gpu_paused", {"reason": describe(self.guard.reasons), "reasons": self.guard.reasons,
                                          "resume_after_seconds": self.guard.cfg.resume_after_seconds})

    def gpu_resumed(self, seconds: float) -> None:
        for sid in sorted(self.gpu_paused_sessions):
            self.bus.emit(sid, "gpu_resumed", {"seconds": round(seconds)})
        self.gpu_paused_sessions.clear()

    async def _gpu_gate(self, sid: str) -> None:
        """Before a model call: while the guard has the GPU paused, step aside and wait first in line."""
        while self.guard is not None and self.guard.active:
            self.scheduler.release(sid)
            await self._acquire(sid, front=True)

    async def _model_call(self, sid: str, *args, **kwargs) -> llm.Completion:
        """self.chat, gated on the GPU guard. A call cut off because the guard stopped the model server (a game
        started and the turn outlasted the drain timeout) is retried after the pause instead of failing."""
        while True:
            await self._gpu_gate(sid)
            turn = await self.gate.agent_turn()  # endpoint requests (an editor, a script) go first
            self.generating.add(sid)
            try:
                return await self.chat(*args, **kwargs)
            except llm.LLMError as e:
                if self.guard is None or not self.guard.active:
                    raise
                self.bus.emit(sid, "llm_retry", {"attempt": 0, "error": f"model server paused for the GPU: {e}"[:500]})
            finally:
                self.generating.discard(sid)
                await turn.release()

    # runner targets (the MacBook)
    async def _wait_for_target(self, sid: str) -> None:
        """Hold a session whose target is offline in `waiting_target`, without the GPU, until it's back."""
        s = self.db.get_session(sid)
        target = s["target"]
        if target == "tower" or self.hub.online(target):
            return
        grace = self.hub.startup_grace()
        if grace > 0:  # just after a daemon restart the runner hasn't had a chance to reconnect yet
            try:
                await asyncio.wait_for(self.hub.wait_online(target), timeout=grace)
                return
            except asyncio.TimeoutError:
                pass
        held = self.scheduler.holder == sid
        previous = s["status"]
        self.scheduler.release(sid)
        since = time.monotonic()
        with self.db.tx():
            self.db.update_session(sid, status="waiting_target")
            self.bus.emit(sid, "status", {"status": "waiting_target"})
            self.bus.emit(sid, "target_waiting", {"target": target})
        await self.hub.wait_online(target)
        status = "waiting_approval" if previous == "waiting_approval" else "queued"
        with self.db.tx():
            self.db.update_session(sid, status=status)
            self.bus.emit(sid, "status", {"status": status})
            self.bus.emit(sid, "target_online", {"target": target, "seconds": round(time.monotonic() - since)})
        if held:
            await self._acquire(sid)

    async def _remote_call(self, sid: str, ws: RemoteWorkspace, name: str, args: dict) -> str:
        return await self._remote_await(sid, ws, name, ws.call(name, args))

    async def _remote_await(self, sid: str, ws: RemoteWorkspace, name: str, coro) -> str:
        """A runner call that may outlive the runner's connection (the lid closed mid-command): once the runner
        is offline the session gives up the GPU and shows as waiting; the call itself keeps waiting for its result."""
        task = asyncio.ensure_future(coro)
        waiting_since = None
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=5)
                if done:
                    return task.result()
                offline = not self.hub.online(ws.target)
                if offline and waiting_since is None:
                    waiting_since = time.monotonic()
                    self.scheduler.release(sid)
                    self.set_status(sid, "waiting_target")
                    self.bus.emit(sid, "target_waiting", {"target": ws.target, "during": name})
                elif not offline and waiting_since is not None:
                    self.bus.emit(sid, "target_online", {"target": ws.target,
                                                         "seconds": round(time.monotonic() - waiting_since)})
                    waiting_since = None
                    await self._acquire(sid)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if waiting_since is not None:
                self.bus.emit(sid, "target_online", {"target": ws.target,
                                                     "seconds": round(time.monotonic() - waiting_since)})
                if not task.cancelled():
                    await self._acquire(sid)

    # main entry
    async def run(self, sid: str, recovered: bool = False) -> None:
        try:
            s = self.db.get_session(sid)
            if s.get("backend", "local") != "local":
                await self._run_cli(sid, recovered=recovered)
                return
            if not self.cfg.modules.local_model:
                raise CliBackendError("the local model is disabled in this service profile")
            if recovered:
                self.bus.emit(sid, "resumed", {"status": s["status"]})
                if (s["run"].get("executing") or {}).get("name") in ("run_shell", "git_clone"):
                    await self.sandbox(s).restart()  # kill the orphaned command
            await self._wait_for_target(sid)
            s = self.db.get_session(sid)
            await self._prepare_repo(s)
            if s["status"] == "waiting_approval":
                pass  # _resolve_calls waits without holding the GPU
            else:
                await self._acquire(sid)
            await self._loop(sid)
        except asyncio.CancelledError:
            if sid in self.user_cancelled:
                self._record_cancel(sid)
                await self._end_run(sid)
            raise
        except CliBackendError as e:
            s = self.db.get_session(sid)
            message = str(e)
            code = ("model_unavailable" if s.get("backend", "local") == "local" else
                    "provider_auth_required" if any(marker in message.lower()
                                                    for marker in ("auth", "login", "api key", "api-key",
                                                                   "credential")) else
                    "provider_unavailable")
            failure = {"code": code, "provider": s.get("backend", "local"), "message": message,
                       "retryable": code in ("model_unavailable", "provider_unavailable")}
            self.db.update_session(sid, run={**s["run"], "failure": failure})
            self.bus.emit(sid, "error", failure)
            self.set_status(sid, "failed", stop_reason=f"{code}: {message}")
            await self._end_run(sid)
        except SandboxUnavailable as e:
            s = self.db.get_session(sid)
            failure = {"code": "backend_unavailable", "provider": s.get("backend", "local"),
                       "message": str(e), "retryable": True}
            self.db.update_session(sid, run={**s["run"], "failure": failure})
            self.bus.emit(sid, "error", failure)
            self.set_status(sid, "failed", stop_reason=f"sandbox_unavailable: {e}")
            await self._end_run(sid)
        except (projects.GitError, RunnerError) as e:
            s = self.db.get_session(sid)
            failure = {"code": "workspace_error", "provider": s.get("backend", "local"),
                       "message": str(e), "retryable": False}
            self.db.update_session(sid, run={**s["run"], "failure": failure})
            self.bus.emit(sid, "error", failure)
            self.set_status(sid, "failed", stop_reason=f"workspace_error: {e}")
            await self._end_run(sid)
        except Exception as e:  # noqa: BLE001 - a crash must not leave the session looking active
            log.exception("session %s crashed", sid)
            s = self.db.get_session(sid)
            message = f"{type(e).__name__}: {e}"
            failure = {"code": "internal_error", "provider": s.get("backend", "local"),
                       "message": message, "retryable": True}
            self.db.update_session(sid, run={**s["run"], "failure": failure})
            self.bus.emit(sid, "error", failure)
            self.set_status(sid, "failed", stop_reason=f"internal_error: {type(e).__name__}: {e}")
            await self._end_run(sid)
        finally:
            await asyncio.shield(self._stop_cli(sid))
            self.scheduler.release(sid)
            self.user_cancelled.discard(sid)

    async def _loop(self, sid: str) -> None:
        while True:
            s = self.db.get_session(sid)
            if s["status"] not in ACTIVE:
                return
            pending = unresolved_calls(s["context"])
            if pending:
                if await self._resolve_calls(s, pending):
                    await self._end_run(sid)
                    return
                continue

            if s["inbox"]:
                context = s["context"] + [{"role": "user", "content": m} for m in s["inbox"]]
                self.db.update_session(sid, context=context, inbox=[])
                continue

            run = s["run"]
            if run["turns"] >= self.cfg.max_turns or run["completion_tokens"] >= self.cfg.max_completion_tokens:
                reason = "budget_turns" if run["turns"] >= self.cfg.max_turns else "budget_tokens"
                self.set_status(sid, "done", stop_reason=reason)
                await self._end_run(sid)
                return

            s = await self._maybe_compact(s)
            if await self._generate(s):
                await self._end_run(sid)
                return

    # hosted CLI backends
    async def _run_cli(self, sid: str, recovered: bool = False) -> None:
        s = self.db.get_session(sid)
        backend_name = s["backend"]
        backend = self.cfg.backends[backend_name]
        if backend_name not in ("claude", "codex", "cursor"):
            raise CliBackendError(f"backend {backend_name!r} is not implemented")
        if recovered:
            self.bus.emit(sid, "resumed", {"status": s["status"]})
        await self._prepare_repo(s)
        slot = self._backend_slots[backend_name]
        while True:
            s = self.db.get_session(sid)
            if s["status"] == "waiting_limit":
                delay = max(0, float(s["run"].get("limit_resets_at") or 0) - time.time())
                if delay:
                    await asyncio.sleep(delay)
                self.set_status(sid, "queued")
            backend_session_id = str(s["run"].get("backend_session_id") or "")
            credential = self._backend_credential(s)
            if credential["policy"] == "denied":
                raise CliBackendError(f"{backend_name} credential policy was revoked for this app")
            use_api_key = credential["policy"] == "api_key" or s["run"].get("backend_auth") == "api_key"
            api_key = credential["key"] if use_api_key else ""
            if use_api_key and not api_key:
                self.bus.emit(sid, "backend_auth_required", {"backend": backend_name, "auth": "api_key"})
                raise CliBackendError(f"{backend_name} API-key credential is configured but no key is available")
            try:
                async with slot:
                    s = self.db.get_session(sid)
                    if s["status"] != "waiting_approval":
                        self.set_status(sid, "running")
                    source = credential["source"] if use_api_key else "subscription"
                    run = {**s["run"], "billing_mode": "api_key" if use_api_key else backend.billing,
                           "credential_source": source,
                           "credential_assignment": credential["assignment_id"]}
                    self.db.update_session(sid, run=run)
                    warning = billing_warning(backend, run.get("rate_limits"), using_api_key=use_api_key)
                    if warning and not run.get("billing_warned"):
                        run["billing_warned"] = True
                        self.db.update_session(sid, run=run)
                        self.bus.emit(sid, "billing_warning", {"backend": backend_name, "message": warning})
                    factory = {"claude": self.cli_factory, "codex": self.codex_factory,
                               "cursor": self.cursor_factory}[backend_name]
                    cli = factory(session_id=sid, workspace=Path(s["workspace"]), backend=backend,
                                  sandbox=self.cfg.sandbox, system_prompt=s["context"][0]["content"],
                                  model=s["model"], backend_session_id=backend_session_id, api_key=api_key)
                    self._cli_sessions[sid] = cli
                    await cli.start()
                    prompt = ("The harness restarted; continue the task." if recovered else
                              next((m["content"] for m in reversed(s["context"][1:])
                                    if m.get("role") == "user" and isinstance(m.get("content"), str)), ""))
                    await cli.initialize(prompt)
                    if cli.backend_session_id and cli.backend_session_id != backend_session_id:
                        s = self.db.get_session(sid)
                        self.db.update_session(sid, run={**s["run"],
                                                       "backend_session_id": cli.backend_session_id})
                    tool_names: dict[str, str] = {}
                    while True:
                        if credential["assignment_id"]:
                            current = self.db.app_provider_credential_by_id(credential["assignment_id"])
                            if current is None or current.get("revoked_at") is not None:
                                raise CliBackendError("the app provider credential was revoked")
                            if use_api_key and self._secret_marker(credential["path"]) != credential["marker"]:
                                raise CliBackendError("the app provider credential file changed")
                        await self._send_cli_inbox(sid, cli)
                        event = await cli.receive(timeout=0.05)
                        if event is None:
                            continue
                        if await self._handle_cli_event(sid, cli, event, tool_names, recovered=recovered):
                            await self._end_run(sid)
                            return
            except CliLimitError as limit:
                await self._stop_cli(sid)
                s = self.db.get_session(sid)
                credential = self._backend_credential(s)
                if credential["policy"] == "subscription_then_api_key" and credential["key"] and not use_api_key:
                    run = {**s["run"], "backend_auth": "api_key"}
                    self.db.update_session(sid, run=run)
                    self.bus.emit(sid, "backend_fallback", {"backend": backend_name, "auth": "api_key"})
                    recovered = True
                    continue
                reset = limit.reset_at or time.time() + 300
                run = {**s["run"], "limit_resets_at": reset}
                self.db.update_session(sid, run=run)
                self.set_status(sid, "waiting_limit")
                self.bus.emit(sid, "limit_waiting", {"backend": backend_name, "resets_at": reset})
                recovered = True

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
        assignment = self.db.app_provider_credential(app_id, s["backend"]) if app_id else None
        if assignment is not None:
            path = self.cfg.provider_secret_files.get(assignment["secret_ref"], "")
            return {"policy": assignment["policy"], "key": self._read_secret(path),
                    "source": "app_file" if assignment["secret_ref"] else "subscription",
                    "assignment_id": assignment["id"], "path": path, "marker": self._secret_marker(path)}
        if app_id and self.db.app_provider_managed(app_id):
            return {"policy": "denied", "key": "", "source": "app_file", "assignment_id": "",
                    "path": "", "marker": None}
        path = backend.api_key_file
        return {"policy": backend.auth, "key": self._read_secret(path), "source": "user_file",
                "assignment_id": "", "path": path, "marker": self._secret_marker(path)}

    async def _send_cli_inbox(self, sid: str, cli: ClaudeSession | CodexSession | CursorSession) -> None:
        """Forward messages received during a CLI run without racing a newer inbox append."""
        queued = list(self.db.get_session(sid)["inbox"])
        if not queued:
            return
        for content in queued:
            await cli.send(cli.user_message(content))
        with self.db.tx():
            current = self.db.get_session(sid)["inbox"]
            remaining = current[len(queued):] if current[:len(queued)] == queued else current
            self.db.update_session(sid, inbox=remaining)

    @staticmethod
    def _cli_text(content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return "" if content is None else json.dumps(content, ensure_ascii=False)
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
                elif isinstance(block.get("content"), list):
                    parts.append(Runner._cli_text(block["content"]))
        return "\n".join(x for x in parts if x)

    async def _handle_cli_event(self, sid: str, cli: ClaudeSession | CodexSession | CursorSession, event: dict,
                                 tool_names: dict[str, str], recovered: bool = False) -> bool:
        """Map one hosted-CLI record. Returns true when the run is complete."""
        if isinstance(cli, CodexSession):
            return await self._handle_codex_event(sid, cli, event, tool_names, recovered=recovered)
        if isinstance(cli, CursorSession):
            return await self._handle_cursor_event(sid, cli, event, tool_names)
        type_ = event.get("type")
        if type_ == "system" and event.get("subtype") == "init":
            backend_session_id = str(event.get("session_id") or "")
            if backend_session_id:
                s = self.db.get_session(sid)
                run = {**s["run"], "backend_session_id": backend_session_id}
                self.db.update_session(sid, run=run)
                cli.backend_session_id = backend_session_id
            return False
        if type_ == "stream_event":
            delta = (event.get("event") or {}).get("delta") or {}
            text = delta.get("text")
            if isinstance(text, str) and text:
                kind = "reasoning" if delta.get("type") in ("thinking_delta", "reasoning_delta") else "content"
                self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})
            return False
        if type_ == "assistant":
            message = event.get("message") or {}
            content = message.get("content") or []
            blocks = content if isinstance(content, list) else []
            calls = []
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                call_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                tool_names[call_id] = name
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}})
            self.bus.emit(sid, "assistant", {"content": self._cli_text(content), "reasoning": "",
                                               "tool_calls": calls, "finish_reason": "",
                                               "prompt_tokens": 0, "completion_tokens": 0,
                                               "prompt_tps": 0, "gen_tps": 0})
            return False
        if type_ == "control_request" and (event.get("request") or {}).get("subtype") == "can_use_tool":
            request = event["request"]
            call_id = str(request.get("tool_use_id") or event.get("request_id") or "")
            tool_names[call_id] = str(request.get("tool_name") or "")
            await self._authorize_cli(sid, cli, str(event.get("request_id") or ""), request,
                                      recovered=recovered)
            return False
        if type_ == "user":
            message = event.get("message") or {}
            content = message.get("content") or []
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or "")
                output = self._cli_text(block.get("content"))
                ok = not bool(block.get("is_error"))
                with self.db.tx():
                    run = self.db.get_session(sid)["run"]
                    run["tool_calls"] = run.get("tool_calls", 0) + 1
                    if not ok:
                        run["tool_errors"] = run.get("tool_errors", 0) + 1
                    self.db.update_session(sid, run=run)
                    self.bus.emit(sid, "tool_result", {"id": call_id, "name": tool_names.get(call_id, ""),
                                                        "ok": ok, "seconds": 0,
                                                        "output": truncate_middle(output, 20000)})
            return False
        if type_ == "rate_limit_event":
            rate_limits = event.get("rate_limit_info") or {}
            s = self.db.get_session(sid)
            self.db.update_session(sid, run={**s["run"], "rate_limits": rate_limits})
            self.db.set_backend_usage(s["backend"], rate_limits)
            self.bus.emit(sid, "rate_limit", rate_limits)
            warning = billing_warning(self.cfg.backends[s["backend"]], rate_limits,
                                      using_api_key=s["run"].get("billing_mode") == "api_key")
            if warning:
                self.bus.emit(sid, "billing_warning", {"backend": s["backend"], "message": warning})
            utilization = float(rate_limits.get("utilization") or 0)
            stop = float(self.cfg.backends[s["backend"]].stop_at_utilization or 0)
            if rate_limits.get("status") == "rejected" or (stop and utilization >= stop):
                raise CliLimitError(float(rate_limits.get("resetsAt") or 0))
            return False
        if type_ == "result":
            text = str(event.get("result") or "").lower()
            if (event.get("is_error") or event.get("subtype") in ("error", "failed")) and "rate limit" in text:
                latest = self.db.get_session(sid)["run"].get("rate_limits") or {}
                raise CliLimitError(float(latest.get("resetsAt") or 0))
            self._finish_cli_result(sid, event)
            return True
        return False

    @staticmethod
    def _cursor_tool(event: dict) -> tuple[str, dict, dict]:
        tool_call = event.get("tool_call") if isinstance(event.get("tool_call"), dict) else {}
        for name, value in tool_call.items():
            if isinstance(value, dict):
                args = value.get("args") if isinstance(value.get("args"), dict) else {}
                result = value.get("result") if isinstance(value.get("result"), dict) else {}
                return name, args, result
        return "cursorTool", {}, {}

    async def _handle_cursor_event(self, sid: str, cli: CursorSession, event: dict,
                                   tool_names: dict[str, str]) -> bool:
        """Map Cursor Agent's documented print-mode stream-json records."""
        type_ = str(event.get("type") or "")
        if type_ == "system" and event.get("subtype") == "init":
            backend_session_id = str(event.get("session_id") or "")
            if backend_session_id:
                cli.backend_session_id = backend_session_id
                s = self.db.get_session(sid)
                self.db.update_session(sid, run={**s["run"], "backend_session_id": backend_session_id})
            return False
        if type_ in ("assistant", "thinking"):
            text = self._cli_text((event.get("message") or {}).get("content"))
            if not text and isinstance(event.get("text"), str):
                text = event["text"]
            if text:
                kind = "reasoning" if type_ == "thinking" else "content"
                # With --stream-partial-output Cursor emits timestamped deltas,
                # then one untimestamped aggregate assistant record. Stream the
                # deltas and retain the aggregate without displaying it twice.
                if type_ == "thinking" or event.get("timestamp_ms") is not None:
                    self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})
                if type_ == "assistant" and event.get("timestamp_ms") is None:
                    cli.last_answer = text
            return False
        if type_ == "tool_call":
            call_id = str(event.get("call_id") or "")
            name, args, result = self._cursor_tool(event)
            tool_names[call_id] = name
            if event.get("subtype") == "started":
                self.bus.emit(sid, "assistant", {"content": "", "reasoning": "", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}], "finish_reason": "",
                    "prompt_tokens": 0, "completion_tokens": 0, "prompt_tps": 0, "gen_tps": 0})
            elif event.get("subtype") == "completed":
                ok = "success" in result and "error" not in result
                output = result.get("success") if ok else result.get("error", result)
                with self.db.tx():
                    run = self.db.get_session(sid)["run"]
                    run["tool_calls"] = run.get("tool_calls", 0) + 1
                    if not ok:
                        run["tool_errors"] = run.get("tool_errors", 0) + 1
                    self.db.update_session(sid, run=run)
                    self.bus.emit(sid, "tool_result", {"id": call_id, "name": tool_names.get(call_id, name),
                                                        "ok": ok, "seconds": 0,
                                                        "output": truncate_middle(self._cli_text(output), 20000)})
            return False
        if type_ == "result":
            combined = cli.combined_result(event)
            text = str(combined.get("result") or "").lower()
            if (combined.get("is_error") or combined.get("subtype") in ("error", "failed")) and any(
                    marker in text for marker in ("rate limit", "quota", "usage limit")):
                raise CliLimitError()
            if cli.has_followups:
                await cli.continue_followups()
                return False
            if cli.last_answer:
                self.bus.emit(sid, "assistant", {"content": cli.last_answer, "reasoning": "", "tool_calls": [],
                                                   "finish_reason": "", "prompt_tokens": 0,
                                                   "completion_tokens": 0, "prompt_tps": 0, "gen_tps": 0})
            self._finish_cli_result(sid, combined)
            return True
        return False

    @staticmethod
    def _codex_rate_limits(snapshot: dict) -> dict:
        """Translate app-server's percent windows into the shared 0..1 shape."""
        windows: dict[str, dict] = {}
        ranked: list[tuple[int, str, float, float]] = []
        for slot in ("primary", "secondary"):
            value = snapshot.get(slot)
            if not isinstance(value, dict) or value.get("usedPercent") is None:
                continue
            minutes = int(value.get("windowDurationMins") or 0)
            if minutes == 300:
                name = "five_hour"
            elif minutes == 10080:
                name = "seven_day"
            else:
                name = f"{minutes}_minute" if minutes else slot
            utilization = float(value["usedPercent"]) / 100
            window = {"utilization": utilization}
            if value.get("resetsAt") is not None:
                window["resetsAt"] = value["resetsAt"]
            windows[name] = window
            ranked.append((minutes, name, utilization, float(value.get("resetsAt") or 0)))
        longest = max(ranked, default=(0, "", 0.0, 0.0))
        reached = snapshot.get("rateLimitReachedType") or ("spend_control" if snapshot.get("spendControlReached") else "")
        result = {**snapshot, "status": "rejected" if reached else "allowed",
                  "unifiedWindows": windows}
        if longest[1]:
            result.update({"rateLimitType": longest[1], "utilization": longest[2],
                           "resetsAt": longest[3]})
        return result

    @staticmethod
    def _codex_file_args(item: dict) -> dict:
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        paths, patches = [], []
        for change in changes:
            if not isinstance(change, dict):
                continue
            raw = str(change.get("path") or "").replace("\\", "/")
            path = raw if raw.startswith("/") else "/workspace/" + raw.lstrip("/")
            if raw:
                paths.append(path)
            if change.get("diff"):
                patches.append(str(change["diff"]))
        return {"file_paths": paths, "patch": "\n".join(patches)}

    async def _handle_codex_event(self, sid: str, cli: CodexSession, event: dict,
                                  tool_names: dict[str, str], recovered: bool = False) -> bool:
        """Map Codex app-server v2 JSON-RPC messages into harness events."""
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        if not method:  # response to turn/start (or another fire-and-continue request)
            return False
        if method == "thread/started":
            thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
            thread_id = str(thread.get("id") or "")
            if thread_id:
                cli.backend_session_id = thread_id
                s = self.db.get_session(sid)
                self.db.update_session(sid, run={**s["run"], "backend_session_id": thread_id})
            return False
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            cli.active_turn_id = str(turn.get("id") or cli.active_turn_id)
            return False
        if method in ("item/agentMessage/delta", "item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            text = params.get("delta")
            if isinstance(text, str) and text:
                kind = "content" if method == "item/agentMessage/delta" else "reasoning"
                self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})
            return False
        if method == "item/started":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            item_id, kind = str(item.get("id") or ""), str(item.get("type") or "")
            if item_id:
                cli.items[item_id] = item
            if kind == "commandExecution":
                name = "exec_command"
                args = {"command": str(item.get("command") or ""), "cwd": str(item.get("cwd") or "")}
            elif kind == "fileChange":
                name, args = "apply_patch", self._codex_file_args(item)
            else:
                return False
            tool_names[item_id] = name
            self.bus.emit(sid, "assistant", {"content": "", "reasoning": "", "tool_calls": [{
                "id": item_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}], "finish_reason": "",
                "prompt_tokens": 0, "completion_tokens": 0, "prompt_tps": 0, "gen_tps": 0})
            return False
        if method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            item_id, kind = str(item.get("id") or ""), str(item.get("type") or "")
            if item_id:
                cli.items[item_id] = item
            if kind == "agentMessage":
                cli.last_answer = str(item.get("text") or "")
                self.bus.emit(sid, "assistant", {"content": cli.last_answer, "reasoning": "", "tool_calls": [],
                                                   "finish_reason": "", "prompt_tokens": 0,
                                                   "completion_tokens": 0, "prompt_tps": 0, "gen_tps": 0})
                return False
            if kind not in ("commandExecution", "fileChange"):
                return False
            name = "exec_command" if kind == "commandExecution" else "apply_patch"
            tool_names[item_id] = name
            status = str(item.get("status") or "")
            output = (str(item.get("aggregatedOutput") or "") if kind == "commandExecution"
                      else "\n".join(str(c.get("diff") or "") for c in item.get("changes", [])
                                     if isinstance(c, dict)))
            ok = status == "completed"
            with self.db.tx():
                run = self.db.get_session(sid)["run"]
                run["tool_calls"] = run.get("tool_calls", 0) + 1
                if not ok:
                    run["tool_errors"] = run.get("tool_errors", 0) + 1
                self.db.update_session(sid, run=run)
                self.bus.emit(sid, "tool_result", {"id": item_id, "name": name, "ok": ok,
                                                    "seconds": float(item.get("durationMs") or 0) / 1000,
                                                    "output": truncate_middle(output, 20000)})
            return False
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            item_id = str(params.get("itemId") or event.get("id") or "")
            item = cli.items.get(item_id, {})
            if method == "item/commandExecution/requestApproval":
                name = "exec_command"
                command = params.get("command") if params.get("command") is not None else item.get("command")
                args = {"command": str(command or ""), "cwd": str(params.get("cwd") or item.get("cwd") or "")}
                if params.get("networkApprovalContext"):
                    args["network"] = True
            else:
                name, args = "apply_patch", self._codex_file_args(item)
                if params.get("grantRoot") and not args["file_paths"]:
                    args["file_paths"] = [str(params["grantRoot"])]
            tool_names[item_id] = name
            request = {"tool_name": name, "input": args, "tool_use_id": item_id,
                       "description": str(params.get("reason") or params.get("command") or "")}
            await self._authorize_cli(sid, cli, event.get("id"), request, recovered=recovered)
            return False
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            if isinstance(usage, dict):
                cli.token_usage = usage
            return False
        if method == "account/rateLimits/updated":
            snapshot = params.get("rateLimits") if isinstance(params.get("rateLimits"), dict) else {}
            limits = self._codex_rate_limits(snapshot)
            s = self.db.get_session(sid)
            self.db.update_session(sid, run={**s["run"], "rate_limits": limits})
            self.db.set_backend_usage(s["backend"], limits)
            self.bus.emit(sid, "rate_limit", limits)
            stop = float(self.cfg.backends[s["backend"]].stop_at_utilization or 0)
            if limits.get("status") == "rejected" or (stop and float(limits.get("utilization") or 0) >= stop):
                raise CliLimitError(float(limits.get("resetsAt") or 0))
            return False
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            status = str(turn.get("status") or "")
            cli.active_turn_id = ""
            error = turn.get("error")
            error_text = json.dumps(error, ensure_ascii=False) if error else ""
            if status == "failed" and any(x in error_text.lower() for x in ("ratelimit", "rate_limit", "usage limit")):
                latest = self.db.get_session(sid)["run"].get("rate_limits") or {}
                raise CliLimitError(float(latest.get("resetsAt") or 0))
            usage = cli.token_usage.get("last") if isinstance(cli.token_usage.get("last"), dict) else {}
            result = {"subtype": "success" if status == "completed" else status or "failed",
                      "is_error": status != "completed", "result": cli.last_answer or error_text,
                      "total_cost_usd": 0, "num_turns": 1, "usage": {
                          "input_tokens": int(usage.get("inputTokens") or 0),
                          "output_tokens": int(usage.get("outputTokens") or 0),
                          "cached_input_tokens": int(usage.get("cachedInputTokens") or 0),
                          "reasoning_output_tokens": int(usage.get("reasoningOutputTokens") or 0)}}
            self._finish_cli_result(sid, result)
            return True
        if method == "error":
            error = params.get("error") or params
            raise CliBackendError(f"Codex app-server error: {error}")
        return False

    async def _authorize_cli(self, sid: str, cli: ClaudeSession | CodexSession, request_id, request: dict,
                              recovered: bool = False) -> None:
        name = str(request.get("tool_name") or "")
        args = request.get("input") if isinstance(request.get("input"), dict) else {}
        call_id = str(request.get("tool_use_id") or request_id)
        s = self.db.get_session(sid)
        existing = self.db.approval_for_call(sid, call_id)
        if existing is None and recovered and s["status"] == "waiting_approval":
            # Claude regenerates the interrupted tool request with a new
            # tool_use_id after --resume. Bind it to the one durable approval
            # with identical semantics so the user sees and decides it once.
            matches = [approval for approval in self.db.approvals(sid)
                       if approval["tool"] == name and approval["args"] == args]
            if len(matches) == 1:
                existing = matches[0]
        if existing is None:
            decision = self.policy(s).decide(name, args)
            self.bus.emit(sid, "tool_call", {"id": call_id, "name": name, "args": args,
                                             "decision": decision.action, "reason": decision.reason})
            if decision.action == ALLOW:
                await cli.respond_permission(request_id, "allow", args)
                return
            if decision.action != ASK:
                reason = decision.reason or "not allowed"
                await cli.respond_permission(request_id, "deny", args,
                                             f"Blocked by harness policy: {reason}. Don't retry this.")
                return
            existing = {"id": "a-" + uuid.uuid4().hex[:8], "session_id": sid, "tool_call_id": call_id,
                        "tool": name, "args": args, "reason": decision.reason,
                        "detail": str(request.get("description") or "")}
            with self.db.tx():
                self.db.insert_approval(existing)
                self.bus.emit(sid, "approval_requested", {k: existing[k] for k in
                                                          ("id", "tool_call_id", "tool", "args", "reason", "detail")})
            existing["status"] = "pending"
        if existing["status"] == "pending":
            self.set_status(sid, "waiting_approval")
            existing = await self._wait_approval(existing["id"])
        if existing["status"] == "approved":
            self.set_status(sid, "running")
            await cli.respond_permission(request_id, "allow", args)
            return
        note = f" User note: {existing['note']}" if existing.get("note") else ""
        self.set_status(sid, "running")
        await cli.respond_permission(request_id, "deny", args, f"The user denied this {name} call.{note}")

    def _finish_cli_result(self, sid: str, result: dict) -> None:
        s = self.db.get_session(sid)
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        prompt_tokens = sum(int(usage.get(key) or 0) for key in
                            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        completion_tokens = int(usage.get("output_tokens") or 0)
        turns = int(result.get("num_turns") or 0)
        cost = float(result.get("total_cost_usd") or 0)
        run = {**s["run"], "turns": turns, "prompt_tokens": prompt_tokens,
               "completion_tokens": completion_tokens, "usage": usage, "total_cost_usd": cost,
               "executing": None}
        totals = dict(s["totals"])
        totals["turns"] = totals.get("turns", 0) + turns
        totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + prompt_tokens
        totals["completion_tokens"] = totals.get("completion_tokens", 0) + completion_tokens
        totals["total_cost_usd"] = round(float(totals.get("total_cost_usd", 0)) + cost, 10)
        answer = str(result.get("result") or "")
        failed = bool(result.get("is_error")) or result.get("subtype") in ("error", "failed")
        status, reason = ("failed", "provider_error") if failed else ("done", "final_message")
        if failed:
            failure = {"code": "provider_error", "provider": s["backend"],
                       "message": answer or str(result.get("subtype") or "provider failed"), "retryable": True}
            run["failure"] = failure
        with self.db.tx():
            self.db.update_session(sid, run=run, totals=totals, status=status, stop_reason=reason, answer=answer)
            self.db.record_usage(s["backend"], sid, s.get("app_id", ""), prompt_tokens, completion_tokens, cost,
                                 str(run.get("billing_mode") or self.cfg.backends[s["backend"]].billing),
                                 str(run.get("credential_source") or "subscription"))
            if failed:
                self.bus.emit(sid, "error", failure)
            self.bus.emit(sid, "status", {"status": status, "stop_reason": reason, "answer": answer})

    async def _stop_cli(self, sid: str) -> None:
        cli = self._cli_sessions.pop(sid, None)
        if cli is not None:
            await cli.stop()

    # model turn
    async def _generate(self, s: dict) -> bool:
        """One model call. Returns True when the run ended with a final answer."""
        sid = s["id"]
        model = self.cfg.models[s["model"]]
        ws = self.workspace(s)
        buffer = {"content": "", "reasoning": ""}
        last_flush = [time.monotonic()]

        def flush() -> None:
            for kind, text in buffer.items():
                if text:
                    self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})
                    buffer[kind] = ""
            last_flush[0] = time.monotonic()

        # A sleeping model takes about a minute to reload; tell the user instead of looking stuck.
        waking_since = None
        if await self.warmer.state(model) in (SLEEPING, WAKING):
            waking_since = time.monotonic()
            self.bus.emit(sid, "model_waking", {"model": model.name, "expected_seconds": EXPECTED_WAKE_SECONDS})

        async def on_delta(kind: str, text: str) -> None:
            nonlocal waking_since
            if waking_since is not None:
                self.bus.emit(sid, "model_ready", {"model": model.name,
                                                   "seconds": round(time.monotonic() - waking_since)})
                waking_since = None
            buffer[kind] += text
            if time.monotonic() - last_flush[0] > 0.25:
                flush()

        reading = self._progress_reporter(sid, "prompt_progress", {})

        run = s["run"]
        tools = self.tool_schemas(s, ws)
        completion = None
        for attempt in range(4):
            try:
                completion = await self._model_call(sid, model, s["context"], tools, on_delta, on_progress=reading)
                break
            except llm.LLMError as e:
                flush()
                if not e.retryable or attempt == 3:
                    raise
                if "HTTP 500" in str(e):
                    run["invalid_tool_calls"] += 1
                self.bus.emit(sid, "llm_retry", {"attempt": attempt + 1, "error": str(e)[:500]})
                await asyncio.sleep(2 * attempt)
        flush()

        run["turns"] += 1
        run["prompt_tokens"] += completion.prompt_tokens
        run["completion_tokens"] += completion.completion_tokens
        if completion.prompt_tokens > 2000:
            # prompt_tokens covers the whole prompt (cached or not), including the tool schemas.
            chars = sum(compaction.message_chars(m) for m in s["context"]) + len(json.dumps(tools))
            run["chars_per_token"] = min(6.0, max(1.5, chars / completion.prompt_tokens))
            run["context_tokens"] = completion.prompt_tokens + completion.completion_tokens

        msg: dict = {"role": "assistant", "content": completion.content}
        if completion.reasoning:
            msg["reasoning_content"] = completion.reasoning
        if completion.tool_calls:
            msg["tool_calls"] = completion.tool_calls
        event = {"content": completion.content, "reasoning": completion.reasoning,
                 "tool_calls": completion.tool_calls, "finish_reason": completion.finish_reason,
                 "prompt_tokens": completion.prompt_tokens, "completion_tokens": completion.completion_tokens,
                 "prompt_tps": round(completion.prompt_tps, 1), "gen_tps": round(completion.gen_tps, 1)}
        context = s["context"] + [msg]
        totals = self._add_totals(s["totals"], completion)
        event["totals"] = totals
        self.last_completion = {"model": model.name, "prompt_tps": completion.prompt_tps, "gen_tps": completion.gen_tps,
                                "at": time.time()}

        final = not completion.tool_calls and completion.content.strip() and completion.finish_reason != "length"
        quotes = self._quote_check(s, run, completion.content) if final else []
        if quotes:
            final = False
        with self.db.tx():
            if final:
                run["idle"] = 0
                self.db.update_session(sid, context=context, run=run, totals=totals, status="done",
                                       stop_reason="final_message", answer=completion.content)
                self.bus.emit(sid, "assistant", event)
                self.bus.emit(sid, "status", {"status": "done", "stop_reason": "final_message",
                                              "answer": completion.content})
                return True
            if quotes:
                context.append({"role": "user", "content": grounding.nudge(quotes)})
                self.bus.emit(sid, "quote_check", {"quotes": quotes})
            elif not completion.tool_calls:
                run["idle"] += 1
                if completion.finish_reason == "length":
                    nudge = "Your reply was cut off by the output limit. Continue, using the tools if needed."
                else:
                    nudge = "Continue the task using the tools. When you are done, give your final answer."
                context.append({"role": "user", "content": nudge})
            else:
                run["idle"] = 0
            self.db.update_session(sid, context=context, run=run, totals=totals)
            self.bus.emit(sid, "assistant", event)
        if run["idle"] >= 3:
            self.set_status(sid, "done", stop_reason="empty_replies")
            return True
        return False

    @staticmethod
    def _add_totals(totals: dict, c: llm.Completion, turn: bool = True) -> dict:
        """Cumulative tokens for the session, compaction summaries included (they aren't turns)."""
        totals = dict(totals)
        totals["turns"] = totals.get("turns", 0) + (1 if turn else 0)
        totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + c.prompt_tokens
        totals["completion_tokens"] = totals.get("completion_tokens", 0) + c.completion_tokens
        return totals

    # tool calls
    async def _resolve_calls(self, s: dict, pending: list[dict]) -> bool:
        """Run the unresolved tool calls of the last assistant message. Returns True if `finish` was called."""
        sid = s["id"]
        executing = s["run"].get("executing") or {}
        model = self.cfg.models[s["model"]]
        # Parallel reads can overflow the window in one turn, so all results of a turn share one budget.
        budget = int(0.35 * model.context_tokens * s["run"].get("chars_per_token", 3.0))
        for i, call in enumerate(pending):
            fn = call.get("function") or {}
            name = fn.get("name", "")
            if executing.get("id") == call["id"]:
                self._record_result(sid, call, name, INTERRUPTED, ok=False)
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as e:
                self._bump(sid, "invalid_tool_calls")
                self._record_result(sid, call, name, f"Error: tool arguments were not a valid JSON object ({e}).",
                                    ok=False)
                continue

            if name == "finish":
                answer = str(args.get("answer", ""))
                run = self.db.get_session(sid)["run"]
                quotes = self._quote_check(s, run, answer)
                if quotes:
                    with self.db.tx():
                        self.db.update_session(sid, run=run)
                        self.bus.emit(sid, "quote_check", {"quotes": quotes})
                    self._record_result(sid, call, name, "Not finished yet. " + grounding.nudge(quotes), ok=False)
                    for rest in pending[i + 1:]:
                        self._record_result(sid, rest, rest["function"].get("name", ""),
                                            "Not run: fix the quotes first.", ok=False)
                    return False
                self._record_result(sid, call, name, "Task finished.", ok=True)
                for rest in pending[i + 1:]:
                    self._record_result(sid, rest, rest["function"].get("name", ""),
                                        "Not run: the task was already finished.", ok=False)
                self.set_status(sid, "done", stop_reason="finished", answer=answer)
                return True

            if name == "update_notes" and isinstance(args.get("notes"), str):
                with self.db.tx():
                    self.db.update_session(sid, run={**self.db.get_session(sid)["run"], "notes": args["notes"]})
                    self.bus.emit(sid, "notes", {"notes": args["notes"]})
                self._record_result(sid, call, name, f"Notes saved ({len(args['notes'])} characters).", ok=True)
                continue

            ws = self.workspace(s)
            schemas = {t["function"]["name"]: t for t in self.tool_schemas(s, ws)}
            if name not in schemas:
                self._bump(sid, "invalid_tool_calls")
                self._record_result(sid, call, name, f"Error: unknown tool '{name}'. Available: "
                                                     f"{', '.join(schemas)}.", ok=False)
                continue
            try:
                args = validate_args(schemas[name], args)
            except ToolError as e:
                self._bump(sid, "invalid_tool_calls")
                self._record_result(sid, call, name, f"Error: bad arguments for {name}: {e}", ok=False)
                continue

            output = await self._authorize(s, call, name, args, ws)
            if output is None:
                output = await self._execute(sid, call, name, args, ws, max_chars=max(2000, budget))
                if name in ("run_shell", "git_clone", "write_file", "generate_image") and await self._over_quota(sid):
                    for rest in pending[i + 1:]:
                        self._record_result(sid, rest, rest["function"].get("name", ""),
                                            "Not run: the workspace is over its disk quota.", ok=False)
                    return True
            budget -= len(output)
            s = self.db.get_session(sid)
        return False

    async def _over_quota(self, sid: str) -> bool:
        """Stop the run when the workspace outgrows its quota. Shrinking is always allowed, so a follow-up run
        can clean up."""
        s = self.db.get_session(sid)
        quota = self.quota_mb(s)
        run = s["run"]
        last = run.get("workspace_mb", 0)
        now = time.monotonic()
        if last < 0.8 * quota and now - self._quota_checked.get(sid, 0) < QUOTA_CHECK_SECONDS:
            return False
        self._quota_checked[sid] = now
        try:
            mb = round(await self.workspace(s).size_bytes() / 2**20)
        except (ToolError, RunnerError):
            return False
        run["workspace_mb"] = mb
        self.db.update_session(sid, run=run)
        user_id = session_user_id(s)
        if user_id != OWNER_USER_ID:
            from .storage import account_usage_bytes, quota_message
            account = self.db.account_by_id(user_id)
            if account is not None:
                used = account_usage_bytes(self.cfg, user_id)
                limit = int(account["disk_quota_bytes"])
                if used >= limit:
                    message = (f"{quota_message(used, limit)}, so the run was stopped. Delete unused files "
                               "or ask the owner to raise the account quota.")
                    self.bus.emit(sid, "error", {"message": message})
                    self.set_status(sid, "failed", stop_reason=f"account_quota_exceeded: {used} > {limit}")
                    return True
        if mb <= quota or mb <= last:
            return False
        message = (f"The workspace is {mb} MB, over its {quota} MB quota, so the run was stopped. Send a message "
                   "asking the agent to delete build artifacts or other large files, or raise quota_mb for the project.")
        self.bus.emit(sid, "error", {"message": message})
        self.set_status(sid, "failed", stop_reason=f"quota_exceeded: {mb} MB > {quota} MB")
        return True

    async def _prepare_repo(self, s: dict) -> None:
        """Clone the project repo on the session's first run; refresh origin on later runs."""
        project = self.project_for(s)
        if not project or not project.repo or s["workspace_removed"]:
            return
        ws = Path(s["workspace"])
        remote = s["target"] != "tower"
        member = session_user_id(s) != OWNER_USER_ID
        if member and remote:
            return
        if not s["base_commit"]:
            if remote:
                info = await self.hub.call(s["target"], "prepare", {"session": s["id"], "repo": project.repo,
                                                                    "base_branch": project.base_branch}, timeout=900)
            elif member:
                from . import clone, storage
                root = storage.workspaces_dir(self.cfg, session_user_id(s))
                info = await asyncio.to_thread(clone.isolated_prepare, ws, project.repo, s["id"], root)
            else:
                info = await asyncio.to_thread(projects.prepare, project, ws, s["id"])
            s = self.db.get_session(s["id"])
            context = s["context"]
            context[0] = {**context[0], "content": context[0]["content"].replace("{base_branch}", info["base_branch"])}
            with self.db.tx():
                self.db.update_session(s["id"], context=context, **info)
                self.bus.emit(s["id"], "workspace_ready", {"repo": project.repo, **info})
        elif not s["run"].get("origin_refreshed"):
            error = (await self.hub.call(s["target"], "refresh_origin", {"session": s["id"]}, timeout=400) if remote
                     else await asyncio.to_thread(projects.refresh_origin, ws))
            if error:
                self.bus.emit(s["id"], "error", {"message": f"could not refresh origin: {error}"})
            run = self.db.get_session(s["id"])["run"]
            self.db.update_session(s["id"], run={**run, "origin_refreshed": True})

    async def _authorize(self, s: dict, call: dict, name: str, args: dict, ws: Workspace) -> str | None:
        """Apply the policy. Returns None to proceed, or the tool result to record instead of running it."""
        sid = s["id"]
        existing = self.db.approval_for_call(sid, call["id"])
        if existing is None:
            decision = self.policy(s).decide(name, args)
            self.bus.emit(sid, "tool_call", {"id": call["id"], "name": name, "args": args,
                                             "decision": decision.action, "reason": decision.reason})
            if decision.action == ALLOW:
                return None
            if decision.action != ASK:
                output = f"Error: blocked by policy ({decision.reason or 'not allowed'}). Don't retry this."
                self._record_result(sid, call, name, output, ok=False)
                return output
            reason, detail = decision.reason, ""
            if name in ("write_file", "edit_file"):
                detail = await ws.preview(name, args)
            else:
                kit = next((k for k in self.daemon_toolkits(s) if name in k.tool_names and hasattr(k, "preview")), None)
                if kit is not None:
                    try:
                        detail, warning = await kit.preview(name, args)
                    except ToolError as e:  # can't be applied as proposed: tell the agent, don't bother the user
                        output = f"Error: {e}"
                        self._record_result(sid, call, name, output, ok=False)
                        return output
                    reason = f"{reason} {warning}".strip()
            existing = {"id": "a-" + uuid.uuid4().hex[:8], "session_id": sid, "tool_call_id": call["id"],
                        "tool": name, "args": args, "reason": reason, "detail": detail}
            with self.db.tx():
                self.db.insert_approval(existing)
                self.bus.emit(sid, "approval_requested", {k: existing[k] for k in
                                                          ("id", "tool_call_id", "tool", "args", "reason", "detail")})
            existing["status"] = "pending"

        if existing["status"] == "pending":
            self.scheduler.release(sid)
            self.set_status(sid, "waiting_approval")
            existing = await self._wait_approval(existing["id"])
            await self._acquire(sid)
        if existing["status"] == "approved":
            return None
        note = f" Their note: {existing['note']}" if existing.get("note") else ""
        output = f"Error: the user denied this {name} call.{note} Don't retry it; choose another approach or explain."
        self._record_result(sid, call, name, output, ok=False)
        return output

    async def _wait_approval(self, aid: str) -> dict:
        event = self.approval_events.setdefault(aid, asyncio.Event())
        try:
            while True:
                approval = self.db.get_approval(aid)
                if approval["status"] != "pending":
                    return approval
                try:
                    await asyncio.wait_for(event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                event.clear()
        finally:
            self.approval_events.pop(aid, None)

    async def _execute(self, sid: str, call: dict, name: str, args: dict, ws: Workspace,
                       max_chars: int = 10**9) -> str:
        await self._acquire(sid)  # e.g. resumed after a restart with the approval already granted
        s = self.db.get_session(sid)
        run = s["run"]
        run["executing"] = {"id": call["id"], "name": name}
        self.db.update_session(sid, run=run)
        started = time.monotonic()
        ok = True
        try:
            kit = next((k for k in self.daemon_toolkits(s) if name in k.tool_names), None)
            if self.app_tools is not None and name in self.app_tools.names(s):
                def waiting() -> None:  # the app works on it: give the GPU to other sessions meanwhile
                    self.scheduler.release(sid)
                    self.set_status(sid, "waiting_app")
                output = await self.app_tools.call(s, call["id"], name, args, on_wait=waiting,
                                                   on_resume=lambda: self._acquire(sid))
            elif kit is not None and kit is self.images:
                put_bytes = None
                root = Path(s["workspace"]) if s["target"] == "tower" else None
                if root is None and isinstance(ws, RemoteWorkspace):
                    async def put_bytes(rel: str, data: bytes) -> str:
                        await self._wait_for_target(sid)
                        return await self._remote_await(sid, ws, "put_file", ws.put_file(rel, data))
                output = await kit.call(name, {**args, "_session": sid}, workspace_root=root, put_bytes=put_bytes)
            elif kit is not None and getattr(kit, "wants_session", False):
                output = await kit.call(name, args, session=s, call_id=call["id"])
            elif kit is not None:
                output = await kit.call(name, args)  # daemon-side for every target
            elif isinstance(ws, RemoteWorkspace):
                await self._wait_for_target(sid)
                output = await self._remote_call(sid, ws, name, args)
            else:
                output = await ws.call(name, args)
        except (ToolError, OSError, UnicodeError) as e:
            ok = False
            output = f"Error: {e}"
        if len(output) > max_chars:
            output = (output[:max_chars] + f"\n... [output cut at {max_chars} characters: this turn's tool results "
                      "would overflow the context window. Request less at once, e.g. a smaller line range.]")
        self._record_result(sid, call, name, output, ok=ok, seconds=time.monotonic() - started)
        return output

    def _bump(self, sid: str, counter: str) -> None:
        s = self.db.get_session(sid)
        run = s["run"]
        run[counter] = run.get(counter, 0) + 1
        self.db.update_session(sid, run=run)

    def _record_result(self, sid: str, call: dict, name: str, output: str, ok: bool, seconds: float = 0.0) -> None:
        with self.db.tx():
            s = self.db.get_session(sid)
            run = s["run"]
            run["tool_calls"] = run.get("tool_calls", 0) + 1
            if not ok:
                run["tool_errors"] = run.get("tool_errors", 0) + 1
            if (run.get("executing") or {}).get("id") == call["id"]:
                run["executing"] = None
            context = s["context"] + [{"role": "tool", "tool_call_id": call["id"], "content": output}]
            self.db.update_session(sid, context=context, run=run)
            self.bus.emit(sid, "tool_result", {"id": call["id"], "name": name, "ok": ok,
                                               "seconds": round(seconds, 2),
                                               "output": truncate_middle(output, 20000)})

    def _progress_reporter(self, sid: str, event: str, base: dict):
        """on_progress callback that sends throttled ephemeral progress events for long prompts."""
        state = {"first": None, "last": 0.0}

        async def report(processed: int, total: int, cache: int = -1) -> None:
            if state["first"] is None:
                state["first"] = cache if cache >= 0 else processed  # the first chunk covers the cached prefix
            if total - state["first"] < PROGRESS_MIN_TOKENS:
                return
            now = time.monotonic()
            if processed < total and now - state["last"] < 0.5:
                return
            state["last"] = now
            self.bus.ephemeral(sid, event, {**base, "processed": processed, "total": total,
                                            "cached": state["first"]})
        return report

    # compaction
    async def _maybe_compact(self, s: dict) -> dict:
        sid = s["id"]
        model = self.cfg.models[s["model"]]
        n = model.context_tokens
        cpt = s["run"].get("chars_per_token", 3.0)
        # Tool schemas are part of every prompt but not of the context list.
        overhead = int(len(json.dumps(self.tool_schemas(s, self.workspace(s)))) / cpt)
        before = compaction.estimate_tokens(s["context"], cpt) + overhead
        if before < self.cfg.elide_at * n:
            return s
        context, saved = compaction.elide(s["context"])
        after = compaction.estimate_tokens(context, cpt) + overhead
        data = {"tier": "elide", "tokens_before": before, "tokens_after": after}
        if after >= self.cfg.summarize_at * n:
            split = compaction.split_for_summary(context, keep_chars=int(self.cfg.keep_recent * n * cpt))
            if split:
                start, end = split
                # Persisted, so a client that opens the session mid-summary still shows it.
                self.bus.emit(sid, "compaction_started", {"messages": end - start, "tokens_before": before,
                                                          "context_tokens": n})
                request = compaction.summary_request(context, start, end, max_chars=int(0.45 * n * cpt))
                written = {"tokens": 0, "last": 0.0}

                async def writing(kind: str, text: str) -> None:
                    written["tokens"] += 1  # one streamed chunk is about one token
                    now = time.monotonic()
                    if now - written["last"] >= 0.5:
                        written["last"] = now
                        self.bus.ephemeral(sid, "compacting", {"phase": "writing", "tokens": written["tokens"],
                                                               "max_tokens": 4096})

                reading = self._progress_reporter(sid, "compacting", {"phase": "reading"})
                try:
                    summary = await self._model_call(sid, model, request, None, writing, max_tokens=4096,
                                              extra={"chat_template_kwargs": {"enable_thinking": False}},
                                              on_progress=reading)
                except llm.LLMError as e:
                    self.bus.emit(sid, "error", {"message": f"compaction summary failed: {e}"})
                else:
                    totals = self._add_totals(self.db.get_session(sid)["totals"], summary, turn=False)
                    self.db.update_session(sid, totals=totals)
                    data.update(prompt_tokens=summary.prompt_tokens, completion_tokens=summary.completion_tokens,
                                totals=totals)
                    if summary.content.strip():
                        context = compaction.apply_summary(context, start, end, summary.content,
                                                           notes=s["run"].get("notes", ""))
                        after = compaction.estimate_tokens(context, cpt) + overhead
                        data.update(tier="summary", tokens_after=after, summarized_messages=end - start,
                                    summary=summary.content)
        data["context_tokens"] = n
        with self.db.tx():
            self.db.update_session(sid, context=context)
            self.bus.emit(sid, "compaction", data)
        return self.db.get_session(sid)

    def _quote_check(self, s: dict, run: dict, answer: str) -> list[str]:
        """Quotes in a final answer that nothing the agent read contains. The agent gets one chance per run to fix
        them (returns them and marks `run`); after that the answer is accepted and _end_run flags what's left."""
        if run.get("quote_check") or not self.cfg.web.quote_check:
            return []
        quotes = grounding.ungrounded_quotes(answer, grounding.session_sources(s["context"], self.db.events(s["id"])))
        if quotes:
            run["quote_check"] = 1
        return quotes

    # run end
    def _record_cancel(self, sid: str) -> None:
        s = self.db.get_session(sid)
        executing = (s["run"].get("executing") or {}).get("id")
        for call in unresolved_calls(s["context"]):
            text = ("Cancelled by the user while running." if call["id"] == executing
                    else "Not run: the user cancelled the task.")
            self._record_result(sid, call, call["function"].get("name", ""), text, ok=False)
        for approval in self.db.pending_approvals(sid):
            self.db.decide_approval(approval["id"], "cancelled")
        self.set_status(sid, "cancelled", stop_reason="cancelled")

    async def _end_run(self, sid: str) -> None:
        s = self.db.get_session(sid)
        extra = {}
        if s.get("job_id"):  # scheduled job: the answer's last STATUS line decides how loudly to notify (jobs.py)
            from .jobs import parse_status
            job_status, reason = parse_status(s["answer"]) if s["status"] == "done" else ("", "")
            self.db.update_session(sid, job_status=job_status)
            extra = {"job_id": s["job_id"], "job_status": job_status, "job_reason": reason}
        if s["status"] == "done" and s["answer"] and self.cfg.web.quote_check:
            quotes = grounding.ungrounded_quotes(s["answer"],
                                                 grounding.session_sources(s["context"], self.db.events(sid)))
            if quotes:
                with self.db.tx():
                    self.db.update_session(sid, run={**s["run"], "ungrounded_quotes": quotes})
                    self.bus.emit(sid, "ungrounded_quotes", {"quotes": quotes})
                s = self.db.get_session(sid)
                extra["ungrounded_quotes"] = quotes
        self.bus.emit(sid, "run_finished", {"status": s["status"], "stop_reason": s["stop_reason"],
                                            "answer": s["answer"], "run": s["run"], **extra})
        if s.get("backend", "local") == "local":
            await asyncio.shield(self.sandbox(s).stop())
        else:
            await asyncio.shield(self._stop_cli(sid))
        await asyncio.shield(self.save_branch(sid))
        self.write_transcript(sid)

    def write_transcript(self, sid: str) -> None:
        try:
            from .transcript import write_transcript
            from . import storage
            s = self.db.get_session(sid)
            dest = storage.transcripts_dir(self.cfg, session_user_id(s) if s else OWNER_USER_ID)
            dest.mkdir(parents=True, exist_ok=True)
            write_transcript(self.db, dest, sid)
        except OSError:
            log.exception("could not write transcript for %s", sid)

    async def save_branch(self, sid: str) -> None:
        """Commit leftovers on the session branch and copy it to a local source repo."""
        s = self.db.get_session(sid)
        project = self.project_for(s)
        if not project or not project.repo or not s["base_commit"] or s["workspace_removed"]:
            return
        ws = Path(s["workspace"])

        def work() -> dict:
            committed = projects.snapshot(ws, f"Uncommitted work at the end of a run (session {sid})")
            published = projects.publish_local(project, ws, s["branch"])
            return {"auto_commit": committed, "published": published, "head": projects.head(ws)[:12],
                    "commits": projects.commits_ahead(ws, s["base_commit"])}

        try:
            if s["target"] != "tower":
                if not self.hub.online(s["target"]):
                    self.bus.emit(sid, "error", {"message": f"the {s['target']} is offline, so the branch wasn't "
                                                            "saved yet; review saves it when it's back"})
                    return
                info = await self.hub.call(s["target"], "save_branch", {
                    "session": sid, "repo": project.repo, "branch": s["branch"], "base_commit": s["base_commit"]},
                    timeout=300)
            else:
                info = await asyncio.to_thread(work)
        except (projects.GitError, RunnerError) as e:
            self.bus.emit(sid, "error", {"message": f"could not save the session branch: {e}"})
            return
        with self.db.tx():
            reviewed = next((e["data"] for e in reversed(self.db.events(sid)) if e["type"] == "review"), None)
            if s["review"] and reviewed and reviewed.get("head") != info["head"]:
                self.db.update_session(sid, review="", review_detail="")  # new work since the last review action
            self.bus.emit(sid, "branch_saved", {"branch": s["branch"], **info})
