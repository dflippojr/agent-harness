"""The agent loop.

The loop is re-entrant: every step reads the session from SQLite and commits its result before the next step,
so after a daemon restart `run()` picks up wherever the session stopped (a model call, a tool call waiting for
approval, or a tool call that was interrupted mid-run).
"""

from __future__ import annotations

import asyncio
import contextlib
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
from .policy import ALLOW, ASK, ChatPolicy, Policy
from .smart_approvals import SmartReviewer, persist_review, sanitized_record
from .remote import RemoteSandbox, RemoteWorkspace, RunnerError, RunnerHub
from .sandbox import Sandbox, SandboxUnavailable
from .scheduler import GpuScheduler, InferenceGate
from .settings import app_allows
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


async def ride_out(task: asyncio.Future) -> None:
    """Wait for `task` to finish even if the waiter is cancelled meanwhile (repeatedly, too). A cancel is
    re-raised once the task is done; the task's own outcome is then dropped so the cancel wins."""
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # mark retrieved: the cancel wins over a setup failure
        raise


class _DeltaStream:
    """Batches streamed model tokens into a few ephemeral delta events and announces the end of a model wake-up."""

    def __init__(self, bus, sid: str, model_name: str) -> None:
        self.bus = bus
        self.sid = sid
        self.model_name = model_name
        self.buffer = {"content": "", "reasoning": ""}
        self.last_flush = time.monotonic()
        self.waking_since: float | None = None

    def flush(self) -> None:
        for kind, text in self.buffer.items():
            if text:
                self.bus.ephemeral(self.sid, "delta", {"kind": kind, "text": text})
                self.buffer[kind] = ""
        self.last_flush = time.monotonic()

    async def on_delta(self, kind: str, text: str) -> None:
        if self.waking_since is not None:
            self.bus.emit(self.sid, "model_ready", {"model": self.model_name,
                                                    "seconds": round(time.monotonic() - self.waking_since)})
            self.waking_since = None
        self.buffer[kind] += text
        if time.monotonic() - self.last_flush > 0.25:
            self.flush()


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
        self.skills = None                      # skills.SkillStore, set by the manager when enabled
        self.app_tools = None                   # apps.AppToolBroker, set by the manager
        self.smart = SmartReviewer(cfg)
        self.settings = None                    # settings_service.SettingsService, set by the manager
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
        defaults = self._app_defaults_for_session(s)
        from . import storage
        user_id = session_user_id(s)
        member = user_id != OWNER_USER_ID
        homelab = (Homelab(self.cfg.homelab)
                   if project and project.homelab and not member and app_allows(defaults, "homelab") else None)
        repos = storage.repos_dir(self.cfg, user_id)
        budget = self._member_clone_budget(user_id) if member else None
        return Workspace(Path(s["workspace"]), self.sandbox(s), repos, model.context_tokens, homelab,
                         public_clone_only=member, clone_max_bytes=budget)

    def daemon_toolkits(self, s: dict) -> list:
        """Tools that run in the daemon for every target (memory library, web, session search), as enabled for the
        project and narrowed by app.capabilities."""
        if s.get("kind") == "chat":  # Chat gets search/fetch only, never the agent toolkits
            return [self.web] if self.web is not None else []
        project = self.project_for(s)
        defaults = self._app_defaults_for_session(s)
        member = session_user_id(s) != OWNER_USER_ID
        kits = []
        if not member and self.memory is not None and self._kit_allowed(project, "memory_library", defaults, "memory_library"):
            kits.append(self.memory)
        if self.web is not None and self._kit_allowed(project, "web", defaults, "web"):
            kits.append(self.web)
        if not member and self.images is not None and self._kit_allowed(project, "images", defaults, "images"):
            kits.append(self.images)
        if self.sessions is not None and self._kit_allowed(project, "session_search", defaults, "search"):
            kits.append(self.sessions)
        if (not member and self.remote_control is not None and s["target"] == "tower"
                and s.get("app_id", "") == "" and app_allows(defaults, "remote_control")):
            kits.append(self.remote_control)  # not for app sessions: apps launch through /api/v1/remote-control
        if self.skills is not None and self.skills.can_propose(s):
            kits.append(self.skills)
        return kits

    @staticmethod
    def _kit_allowed(project, flag: str, defaults: dict, capability: str) -> bool:
        """The project (when there is one) enables the toolkit and app.capabilities doesn't narrow it away."""
        return (project is None or getattr(project, flag)) and app_allows(defaults, capability)

    def _app_defaults_for_session(self, s: dict) -> dict:
        if self.settings is None:
            return {}
        return self.settings.app_defaults_for_session(s)

    def tool_schemas(self, s: dict, ws) -> list[dict]:
        if s.get("kind") == "chat":
            return self.web.schemas() if self.web is not None else []
        schemas = ws.schemas()
        for kit in self.daemon_toolkits(s):
            schemas = schemas + kit.schemas()
        if self.app_tools is not None and s.get("app_tools") and session_user_id(s) == OWNER_USER_ID:
            schemas = schemas + self.app_tools.schemas(s)
        return schemas

    def policy(self, s: dict) -> Policy | ChatPolicy:
        if s.get("kind") == "chat":
            return ChatPolicy()
        project = self.project_for(s)
        return Policy(project.rules if project else [], repo=bool(project and project.repo))

    def quota_mb(self, s: dict) -> int:
        project = self.project_for(s)
        default = (self.cfg.runners[s["target"]].workspace_quota_mb if s["target"] in self.cfg.runners
                   else self.cfg.cleanup.workspace_quota_mb)
        return (project.quota_mb if project and project.quota_mb else 0) or default

    async def _review_ask(self, s: dict, name: str, args: dict, decision) -> dict:
        """Consult the smart reviewer for a tagged ASK. Never auto-denies; failures stay pending."""
        extra = {"status": "pending", "smart": {}}
        if decision.action != ASK:
            return extra
        project = self.cfg.projects.get(s["project"])
        repo = bool(project and project.repo)
        _eligibility, review = await self.smart.consider(self.db, self.policy(s), name, args, decision, repo=repo)
        if review is None:
            return extra
        auto = self.smart.should_auto_approve(review)
        record = sanitized_record(review, policy_fingerprint=self.policy(s).fingerprint(),
                                  mode=review.mode, outcome=self.smart.classify(review, auto), tool=name)
        extra["smart"] = {k: record[k] for k in ("recommendation", "confidence", "reason", "risk_flags",
                                                 "mode", "provider", "model", "outcome", "escalate_reason")}
        extra["_record"] = record
        extra["status"] = "approved" if auto else "pending"
        extra["note"] = "smart reviewer: deterministic gate and hosted model both allowed this call" if auto else ""
        return extra

    def _persist_ask(self, sid: str, existing: dict, extra: dict) -> dict:
        record = extra.pop("_record", None)
        existing["status"] = extra.get("status", "pending")
        existing["smart"] = extra.get("smart") or {}
        if extra.get("note"):
            existing["note"] = extra["note"]
        public = {k: existing[k] for k in ("id", "tool_call_id", "tool", "args", "reason", "detail") if k in existing}
        if existing.get("smart"):
            public["smart"] = existing["smart"]
        with self.db.tx():
            self.db.insert_approval(existing)
            if record is not None:
                persist_review(self.db, sid, existing["id"], record)
                self.bus.emit(sid, "smart_review", record)
            if existing["status"] == "approved":
                self.bus.emit(sid, "approval_auto_approved", {"id": existing["id"], **(record or {})})
            else:
                self.bus.emit(sid, "approval_requested", public)
        return existing

    def _member_clone_budget(self, user_id: str) -> int | None:
        """Bytes a member clone may still write. None means uncapped (owner)."""
        if user_id == OWNER_USER_ID:
            return None
        account = self.db.account_by_id(user_id)
        if account is None:
            return 0
        from .storage import account_usage_bytes
        return max(0, int(account["disk_quota_bytes"]) - account_usage_bytes(self.cfg, user_id))

    def set_status(self, sid: str, status: str, **fields) -> None:
        with self.db.tx():
            self.db.update_session(sid, status=status, **fields)
            self.bus.emit(sid, "status", {"status": status, **{k: v for k, v in fields.items()
                                                                 if k in ("stop_reason", "answer")}})
        # Caps key off `running`. After a session leaves that state, ineligible waiters may now be grantable.
        self.scheduler.recheck()

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
            await self._run_local(sid, s, recovered)
        except asyncio.CancelledError:
            await self._take_pending_cancel(sid)
            raise
        except CliBackendError as e:
            if await self._take_pending_cancel(sid):
                return
            message = str(e)
            code = self._cli_failure_code(self.db.get_session(sid), message)
            self._record_failure(sid, code, message, code in ("model_unavailable", "provider_unavailable"),
                                 f"{code}: {message}")
            await self._end_run(sid)
        except SandboxUnavailable as e:
            self._record_failure(sid, "backend_unavailable", str(e), True, f"sandbox_unavailable: {e}")
            await self._end_run(sid)
        except (projects.GitError, RunnerError) as e:
            self._record_failure(sid, "workspace_error", str(e), False, f"workspace_error: {e}")
            await self._end_run(sid)
        except Exception as e:  # noqa: BLE001 - a crash must not leave the session looking active
            if await self._take_pending_cancel(sid):
                return
            log.exception("session %s crashed", sid)
            self._record_failure(sid, "internal_error", f"{type(e).__name__}: {e}", True,
                                 f"internal_error: {type(e).__name__}: {e}")
            await self._end_run(sid)
        finally:
            await asyncio.shield(self._stop_cli(sid))
            self.scheduler.release(sid)
            self.user_cancelled.discard(sid)

    async def _run_local(self, sid: str, s: dict, recovered: bool) -> None:
        if recovered:
            self.bus.emit(sid, "resumed", {"status": s["status"]})
            if (s["run"].get("executing") or {}).get("name") in ("run_shell", "git_clone"):
                await self.sandbox(s).restart()  # kill the orphaned command
        await self._wait_for_target(sid)
        s = self.db.get_session(sid)
        await self._prepare_repo(s)
        if s["status"] != "waiting_approval":  # _resolve_calls waits without holding the GPU
            await self._acquire(sid)
        await self._loop(sid)

    @staticmethod
    def _cli_failure_code(s: dict, message: str) -> str:
        if s.get("backend", "local") == "local":
            return "model_unavailable"
        if any(marker in message.lower() for marker in ("auth", "login", "api key", "api-key", "credential")):
            return "provider_auth_required"
        return "provider_unavailable"

    def _record_failure(self, sid: str, code: str, message: str, retryable: bool, stop_reason: str) -> None:
        s = self.db.get_session(sid)
        failure = {"code": code, "provider": s.get("backend", "local"), "message": message, "retryable": retryable}
        self.db.update_session(sid, run={**s["run"], "failure": failure})
        self.bus.emit(sid, "error", failure)
        self.set_status(sid, "failed", stop_reason=stop_reason)

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

            reason = self._budget_reason(s["run"])
            if reason:
                self.set_status(sid, "done", stop_reason=reason)
                await self._end_run(sid)
                return

            s = await self._maybe_compact(s)
            if await self._generate(s):
                await self._end_run(sid)
                return

    def _budget_reason(self, run: dict) -> str:
        """Why the run is out of budget ("" while it has some left)."""
        max_turns = int(run.get("max_turns") or self.cfg.max_turns)
        max_tokens = int(run.get("max_completion_tokens") or self.cfg.max_completion_tokens)
        if run["turns"] >= max_turns:
            return "budget_turns"
        if run["completion_tokens"] >= max_tokens:
            return "budget_tokens"
        return ""

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
            credential, use_api_key, api_key = self._cli_credentials(sid, s, backend_name)
            try:
                async with slot:
                    cli = await self._start_cli(sid, backend_name, backend, credential, use_api_key, api_key,
                                                backend_session_id, recovered)
                    await self._pump_cli(sid, cli, credential, use_api_key, recovered)
                    return
            except CliLimitError as limit:
                await self._cli_limit_reached(sid, backend_name, limit, use_api_key)
                recovered = True

    def _cli_credentials(self, sid: str, s: dict, backend_name: str) -> tuple[dict, bool, str]:
        """The credential for this attempt, whether it is an API key, and the key itself."""
        credential = self._backend_credential(s)
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
        from dataclasses import replace
        frozen = replace(backend, model=s["model"], effort=s.get("effort") or backend.effort)
        cli = factory(session_id=sid, workspace=Path(s["workspace"]), backend=frozen,
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
            self.db.update_session(sid, run={**s["run"], "backend_session_id": cli.backend_session_id})
        return cli

    def _check_cli_credential(self, credential: dict, use_api_key: bool) -> None:
        if not credential["assignment_id"]:
            return
        current = self.db.app_provider_credential_by_id(credential["assignment_id"])
        if current is None or current.get("revoked_at") is not None:
            raise CliBackendError("the app provider credential was revoked")
        if use_api_key and self._secret_marker(credential["path"]) != credential["marker"]:
            raise CliBackendError("the app provider credential file changed")

    async def _pump_cli(self, sid: str, cli, credential: dict, use_api_key: bool, recovered: bool) -> None:
        """Feed the CLI's events to the handlers until the run is complete."""
        tool_names: dict[str, str] = {}
        while True:
            self._check_cli_credential(credential, use_api_key)
            await self._send_cli_inbox(sid, cli)
            event = await cli.receive(timeout=0.05)
            if event is None:
                continue
            if await self._handle_cli_event(sid, cli, event, tool_names, recovered=recovered):
                await self._end_run(sid)
                return

    async def _cli_limit_reached(self, sid: str, backend_name: str, limit: CliLimitError,
                                 use_api_key: bool) -> None:
        await self._stop_cli(sid)
        s = self.db.get_session(sid)
        credential = self._backend_credential(s)
        if credential["policy"] == "subscription_then_api_key" and credential["key"] and not use_api_key:
            run = {**s["run"], "backend_auth": "api_key"}
            self.db.update_session(sid, run=run)
            self.bus.emit(sid, "backend_fallback", {"backend": backend_name, "auth": "api_key"})
            return
        reset = limit.reset_at or time.time() + 300
        run = {**s["run"], "limit_resets_at": reset}
        self.db.update_session(sid, run=run)
        self.set_status(sid, "waiting_limit")
        self.bus.emit(sid, "limit_waiting", {"backend": backend_name, "resets_at": reset})

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
            self._bind_session_if_any(sid, cli, event)
        elif type_ == "control_request":
            await self._claude_control(sid, cli, event, tool_names, recovered)
        elif type_ == "result":
            self._claude_result(sid, event)
            return True
        elif type_ in self._CLAUDE_SIMPLE:
            getattr(self, self._CLAUDE_SIMPLE[type_])(sid, event, tool_names)
        return False

    _CLAUDE_SIMPLE = {"stream_event": "_claude_delta", "assistant": "_claude_assistant",
                      "user": "_claude_tool_results", "rate_limit_event": "_claude_rate_limits"}

    def _bind_session_if_any(self, sid: str, cli, event: dict) -> None:
        backend_session_id = str(event.get("session_id") or "")
        if backend_session_id:
            self._bind_backend_session(sid, cli, backend_session_id)

    async def _claude_control(self, sid: str, cli, event: dict, tool_names: dict[str, str],
                              recovered: bool) -> None:
        request = event.get("request") or {}
        if request.get("subtype") != "can_use_tool":
            return
        call_id = str(request.get("tool_use_id") or event.get("request_id") or "")
        tool_names[call_id] = str(request.get("tool_name") or "")
        await self._authorize_cli(sid, cli, str(event.get("request_id") or ""), request, recovered=recovered)

    def _claude_result(self, sid: str, event: dict) -> None:
        text = str(event.get("result") or "").lower()
        if (event.get("is_error") or event.get("subtype") in ("error", "failed")) and "rate limit" in text:
            latest = self.db.get_session(sid)["run"].get("rate_limits") or {}
            raise CliLimitError(float(latest.get("resetsAt") or 0))
        self._finish_cli_result(sid, event)

    def _bind_backend_session(self, sid: str, cli, backend_session_id: str) -> None:
        s = self.db.get_session(sid)
        self.db.update_session(sid, run={**s["run"], "backend_session_id": backend_session_id})
        cli.backend_session_id = backend_session_id

    def _emit_assistant(self, sid: str, content: str = "", tool_calls: list | None = None) -> None:
        self.bus.emit(sid, "assistant", {"content": content, "reasoning": "", "tool_calls": tool_calls or [],
                                           "finish_reason": "", "prompt_tokens": 0, "completion_tokens": 0,
                                           "prompt_tps": 0, "gen_tps": 0})

    @staticmethod
    def _tool_call_entry(call_id: str, name: str, args: dict) -> dict:
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    def _record_cli_tool_result(self, sid: str, call_id: str, name: str, ok: bool, output: str,
                                seconds: float = 0) -> None:
        with self.db.tx():
            run = self.db.get_session(sid)["run"]
            run["tool_calls"] = run.get("tool_calls", 0) + 1
            if not ok:
                run["tool_errors"] = run.get("tool_errors", 0) + 1
            self.db.update_session(sid, run=run)
            self.bus.emit(sid, "tool_result", {"id": call_id, "name": name, "ok": ok, "seconds": seconds,
                                               "output": truncate_middle(output, 20000)})

    def _claude_delta(self, sid: str, event: dict, tool_names: dict[str, str] | None = None) -> None:
        delta = (event.get("event") or {}).get("delta") or {}
        text = delta.get("text")
        if isinstance(text, str) and text:
            kind = "reasoning" if delta.get("type") in ("thinking_delta", "reasoning_delta") else "content"
            self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})

    def _claude_assistant(self, sid: str, event: dict, tool_names: dict[str, str]) -> None:
        content = (event.get("message") or {}).get("content") or []
        blocks = content if isinstance(content, list) else []
        calls = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            tool_names[call_id] = name
            calls.append(self._tool_call_entry(call_id, name, args))
        self._emit_assistant(sid, self._cli_text(content), calls)

    def _claude_tool_results(self, sid: str, event: dict, tool_names: dict[str, str]) -> None:
        content = (event.get("message") or {}).get("content") or []
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            self._record_cli_tool_result(sid, call_id, tool_names.get(call_id, ""), not bool(block.get("is_error")),
                                         self._cli_text(block.get("content")))

    def _claude_rate_limits(self, sid: str, event: dict, tool_names: dict[str, str] | None = None) -> None:
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
            self._bind_session_if_any(sid, cli, event)
        elif type_ in ("assistant", "thinking"):
            self._cursor_text(sid, cli, event, type_)
        elif type_ == "tool_call":
            self._cursor_tool_event(sid, event, tool_names)
        elif type_ == "result":
            return await self._cursor_result(sid, cli, event)
        return False

    def _cursor_text(self, sid: str, cli: CursorSession, event: dict, type_: str) -> None:
        text = self._cli_text((event.get("message") or {}).get("content"))
        if not text and isinstance(event.get("text"), str):
            text = event["text"]
        if not text:
            return
        kind = "reasoning" if type_ == "thinking" else "content"
        # With --stream-partial-output Cursor emits timestamped deltas,
        # then one untimestamped aggregate assistant record. Stream the
        # deltas and retain the aggregate without displaying it twice.
        if type_ == "thinking" or event.get("timestamp_ms") is not None:
            self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})
        if type_ == "assistant" and event.get("timestamp_ms") is None:
            cli.last_answer = text

    def _cursor_tool_event(self, sid: str, event: dict, tool_names: dict[str, str]) -> None:
        call_id = str(event.get("call_id") or "")
        name, args, result = self._cursor_tool(event)
        tool_names[call_id] = name
        if event.get("subtype") == "started":
            self._emit_assistant(sid, "", [self._tool_call_entry(call_id, name, args)])
        elif event.get("subtype") == "completed":
            ok = "success" in result and "error" not in result
            output = result.get("success") if ok else result.get("error", result)
            self._record_cli_tool_result(sid, call_id, tool_names.get(call_id, name), ok, self._cli_text(output))

    async def _cursor_result(self, sid: str, cli: CursorSession, event: dict) -> bool:
        combined = cli.combined_result(event)
        text = str(combined.get("result") or "").lower()
        if (combined.get("is_error") or combined.get("subtype") in ("error", "failed")) and any(
                marker in text for marker in ("rate limit", "quota", "usage limit")):
            raise CliLimitError()
        if cli.has_followups:
            await cli.continue_followups()
            return False
        if cli.last_answer:
            self._emit_assistant(sid, cli.last_answer)
        self._finish_cli_result(sid, combined)
        return True

    @staticmethod
    def _codex_window(slot: str, value: dict) -> tuple[int, str, dict]:
        minutes = int(value.get("windowDurationMins") or 0)
        if minutes == 300:
            name = "five_hour"
        elif minutes == 10080:
            name = "seven_day"
        else:
            name = f"{minutes}_minute" if minutes else slot
        window = {"utilization": float(value["usedPercent"]) / 100}
        if value.get("resetsAt") is not None:
            window["resetsAt"] = value["resetsAt"]
        return minutes, name, window

    @staticmethod
    def _codex_rate_limits(snapshot: dict) -> dict:
        """Translate app-server's percent windows into the shared 0..1 shape."""
        windows: dict[str, dict] = {}
        ranked: list[tuple[int, str, float, float]] = []
        for slot in ("primary", "secondary"):
            value = snapshot.get(slot)
            if not isinstance(value, dict) or value.get("usedPercent") is None:
                continue
            minutes, name, window = Runner._codex_window(slot, value)
            windows[name] = window
            ranked.append((minutes, name, window["utilization"], float(value.get("resetsAt") or 0)))
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
        if method in self._CODEX_SIMPLE:
            getattr(self, self._CODEX_SIMPLE[method])(sid, cli, params, tool_names, method)
        elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            await self._codex_request_approval(sid, cli, event, params, tool_names, recovered)
        elif method == "turn/completed":
            self._codex_turn_completed(sid, cli, params)
            return True
        elif method == "error":
            error = params.get("error") or params
            raise CliBackendError(f"Codex app-server error: {error}")
        return False

    _CODEX_SIMPLE = {
        "thread/started": "_codex_thread_started", "turn/started": "_codex_turn_started",
        "item/agentMessage/delta": "_codex_delta", "item/reasoning/summaryTextDelta": "_codex_delta",
        "item/reasoning/textDelta": "_codex_delta", "item/started": "_codex_item_started",
        "item/completed": "_codex_item_completed", "thread/tokenUsage/updated": "_codex_token_usage",
        "account/rateLimits/updated": "_codex_rate_limits_event"}

    def _codex_turn_started(self, sid: str, cli: CodexSession, params: dict, tool_names, method) -> None:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        cli.active_turn_id = str(turn.get("id") or cli.active_turn_id)

    def _codex_delta(self, sid: str, cli: CodexSession, params: dict, tool_names, method: str) -> None:
        text = params.get("delta")
        if isinstance(text, str) and text:
            kind = "content" if method == "item/agentMessage/delta" else "reasoning"
            self.bus.ephemeral(sid, "delta", {"kind": kind, "text": text})

    def _codex_token_usage(self, sid: str, cli: CodexSession, params: dict, tool_names, method) -> None:
        usage = params.get("tokenUsage")
        if isinstance(usage, dict):
            cli.token_usage = usage

    def _codex_thread_started(self, sid: str, cli: CodexSession, params: dict, tool_names=None, method="") -> None:
        thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "")
        if thread_id:
            self._bind_backend_session(sid, cli, thread_id)

    def _codex_item_started(self, sid: str, cli: CodexSession, params: dict, tool_names: dict[str, str],
                            method: str = "") -> None:
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
            return
        tool_names[item_id] = name
        self._emit_assistant(sid, "", [self._tool_call_entry(item_id, name, args)])

    def _codex_item_completed(self, sid: str, cli: CodexSession, params: dict, tool_names: dict[str, str],
                              method: str = "") -> None:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        item_id, kind = str(item.get("id") or ""), str(item.get("type") or "")
        if item_id:
            cli.items[item_id] = item
        if kind == "agentMessage":
            cli.last_answer = str(item.get("text") or "")
            self._emit_assistant(sid, cli.last_answer)
            return
        if kind not in ("commandExecution", "fileChange"):
            return
        name = "exec_command" if kind == "commandExecution" else "apply_patch"
        tool_names[item_id] = name
        output = (str(item.get("aggregatedOutput") or "") if kind == "commandExecution"
                  else "\n".join(str(c.get("diff") or "") for c in item.get("changes", [])
                                 if isinstance(c, dict)))
        self._record_cli_tool_result(sid, item_id, name, str(item.get("status") or "") == "completed", output,
                                     seconds=float(item.get("durationMs") or 0) / 1000)

    async def _codex_request_approval(self, sid: str, cli: CodexSession, event: dict, params: dict,
                                      tool_names: dict[str, str], recovered: bool) -> None:
        item_id = str(params.get("itemId") or event.get("id") or "")
        item = cli.items.get(item_id, {})
        if event.get("method") == "item/commandExecution/requestApproval":
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

    def _codex_rate_limits_event(self, sid: str, cli, params: dict, tool_names=None, method="") -> None:
        snapshot = params.get("rateLimits") if isinstance(params.get("rateLimits"), dict) else {}
        limits = self._codex_rate_limits(snapshot)
        s = self.db.get_session(sid)
        self.db.update_session(sid, run={**s["run"], "rate_limits": limits})
        self.db.set_backend_usage(s["backend"], limits)
        self.bus.emit(sid, "rate_limit", limits)
        stop = float(self.cfg.backends[s["backend"]].stop_at_utilization or 0)
        if limits.get("status") == "rejected" or (stop and float(limits.get("utilization") or 0) >= stop):
            raise CliLimitError(float(limits.get("resetsAt") or 0))

    def _codex_turn_completed(self, sid: str, cli: CodexSession, params: dict) -> None:
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
            existing = await self._ask_cli_policy(s, cli, request_id, request, name, args, call_id)
            if existing is None:
                return
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

    async def _ask_cli_policy(self, s: dict, cli, request_id, request: dict, name: str, args: dict,
                              call_id: str) -> dict | None:
        """Apply the policy to a new CLI tool request: answer it now (None) or persist an approval to wait on."""
        sid = s["id"]
        decision = self.policy(s).decide(name, args)
        self.bus.emit(sid, "tool_call", {"id": call_id, "name": name, "args": args,
                                         "decision": decision.action, "reason": decision.reason})
        if decision.action == ALLOW:
            await cli.respond_permission(request_id, "allow", args)
            return None
        if decision.action != ASK:
            reason = decision.reason or "not allowed"
            await cli.respond_permission(request_id, "deny", args,
                                         f"Blocked by harness policy: {reason}. Don't retry this.")
            return None
        existing = {"id": "a-" + uuid.uuid4().hex[:8], "session_id": sid, "tool_call_id": call_id,
                    "tool": name, "args": args, "reason": decision.reason,
                    "detail": str(request.get("description") or "")}
        extra = await self._review_ask(s, name, args, decision)
        return self._persist_ask(sid, existing, extra)

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
        stream = _DeltaStream(self.bus, sid, model.name)
        # A sleeping model takes about a minute to reload; tell the user instead of looking stuck.
        if await self.warmer.state(model) in (SLEEPING, WAKING):
            stream.waking_since = time.monotonic()
            self.bus.emit(sid, "model_waking", {"model": model.name, "expected_seconds": EXPECTED_WAKE_SECONDS})

        reading = self._progress_reporter(sid, "prompt_progress", {})
        run = s["run"]
        tools = self.tool_schemas(s, ws)
        completion = await self._call_with_retries(sid, model, s["context"], tools, stream, reading, run)
        stream.flush()

        run["turns"] += 1
        run["prompt_tokens"] += completion.prompt_tokens
        run["completion_tokens"] += completion.completion_tokens
        if completion.prompt_tokens > 2000:
            # prompt_tokens covers the whole prompt (cached or not), including the tool schemas.
            chars = sum(compaction.message_chars(m) for m in s["context"]) + len(json.dumps(tools))
            run["chars_per_token"] = min(6.0, max(1.5, chars / completion.prompt_tokens))
            run["context_tokens"] = completion.prompt_tokens + completion.completion_tokens
        return self._commit_completion(s, run, completion, model)

    async def _call_with_retries(self, sid: str, model, context: list, tools: list, stream: "_DeltaStream",
                                 reading, run: dict):
        for attempt in range(4):
            try:
                return await self._model_call(sid, model, context, tools, stream.on_delta, on_progress=reading)
            except llm.LLMError as e:
                stream.flush()
                if not e.retryable or attempt == 3:
                    raise
                if "HTTP 500" in str(e):
                    run["invalid_tool_calls"] += 1
                self.bus.emit(sid, "llm_retry", {"attempt": attempt + 1, "error": str(e)[:500]})
                await asyncio.sleep(2 * attempt)

    def _commit_completion(self, s: dict, run: dict, completion, model) -> bool:
        """Record one model reply: the context, totals and events, and whether the run is over."""
        sid = s["id"]
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
            self._nudge(sid, context, run, completion, quotes)
            self.db.update_session(sid, context=context, run=run, totals=totals)
            self.bus.emit(sid, "assistant", event)
        if run["idle"] >= 3:
            self.set_status(sid, "done", stop_reason="empty_replies")
            return True
        return False

    def _nudge(self, sid: str, context: list, run: dict, completion, quotes: list[str]) -> None:
        """Steer a reply that isn't a final answer: fix ungrounded quotes, or keep going with the tools."""
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
            done, used = await self._resolve_call(s, call, pending[i + 1:], executing, budget)
            if done is not None:
                return done
            if used is not None:
                budget -= used
                s = self.db.get_session(sid)
        return False

    async def _resolve_call(self, s: dict, call: dict, rest: list[dict], executing: dict,
                            budget: int) -> tuple[bool | None, int | None]:
        """Run one tool call. Returns (done, used): done is True/False when the turn ends there (None to go on),
        used is the output length that counts against the turn's budget (None when nothing ran)."""
        sid = s["id"]
        fn = call.get("function") or {}
        name = fn.get("name", "")
        if executing.get("id") == call["id"]:
            self._record_result(sid, call, name, INTERRUPTED, ok=False)
            return None, None
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            self._bump(sid, "invalid_tool_calls")
            self._record_result(sid, call, name, f"Error: tool arguments were not a valid JSON object ({e}).",
                                ok=False)
            return None, None

        if name == "finish":
            return self._finish_call(s, call, args, rest), None
        if name == "update_notes" and isinstance(args.get("notes"), str):
            with self.db.tx():
                self.db.update_session(sid, run={**self.db.get_session(sid)["run"], "notes": args["notes"]})
                self.bus.emit(sid, "notes", {"notes": args["notes"]})
            self._record_result(sid, call, name, f"Notes saved ({len(args['notes'])} characters).", ok=True)
            return None, None

        ws = self.workspace(s)
        schemas = {t["function"]["name"]: t for t in self.tool_schemas(s, ws)}
        if name not in schemas:
            self._bump(sid, "invalid_tool_calls")
            self._record_result(sid, call, name, f"Error: unknown tool '{name}'. Available: "
                                                 f"{', '.join(schemas)}.", ok=False)
            return None, None
        try:
            args = validate_args(schemas[name], args)
        except ToolError as e:
            self._bump(sid, "invalid_tool_calls")
            self._record_result(sid, call, name, f"Error: bad arguments for {name}: {e}", ok=False)
            return None, None

        output = await self._authorize(s, call, name, args, ws)
        if output is None:
            output = await self._execute(sid, call, name, args, ws, max_chars=max(2000, budget))
            if name in ("run_shell", "git_clone", "write_file", "generate_image") and await self._over_quota(sid):
                self._skip_rest(sid, rest, "Not run: the workspace is over its disk quota.")
                return True, None
        return None, len(output)

    def _skip_rest(self, sid: str, rest: list[dict], reason: str) -> None:
        for call in rest:
            self._record_result(sid, call, call["function"].get("name", ""), reason, ok=False)

    def _finish_call(self, s: dict, call: dict, args: dict, rest: list[dict]) -> bool:
        """The agent called `finish`: True when the task is done, False when its quotes need fixing first."""
        sid = s["id"]
        answer = str(args.get("answer", ""))
        run = self.db.get_session(sid)["run"]
        quotes = self._quote_check(s, run, answer)
        if quotes:
            with self.db.tx():
                self.db.update_session(sid, run=run)
                self.bus.emit(sid, "quote_check", {"quotes": quotes})
            self._record_result(sid, call, "finish", "Not finished yet. " + grounding.nudge(quotes), ok=False)
            self._skip_rest(sid, rest, "Not run: fix the quotes first.")
            return False
        self._record_result(sid, call, "finish", "Task finished.", ok=True)
        self._skip_rest(sid, rest, "Not run: the task was already finished.")
        self.set_status(sid, "done", stop_reason="finished", answer=answer)
        return True

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
        """Clone the project repo on the session's first run; refresh origin on later runs. A cancel that lands
        mid-clone waits for the clone and its record: the git work runs in a thread (or on a runner) the cancel
        can't stop, so returning early would leave a workspace and branch nobody records or removes (#203)."""
        setup = asyncio.ensure_future(self._setup_repo(s))
        await ride_out(setup)
        setup.result()

    async def _setup_repo(self, s: dict) -> None:
        project = self.project_for(s)
        if not project or not project.repo or s["workspace_removed"]:
            return
        ws = Path(s["workspace"])
        remote = s["target"] != "tower"
        member = session_user_id(s) != OWNER_USER_ID
        if member and remote:
            return
        if not s["base_commit"]:
            info = await self._first_prepare(s, project, ws, remote, member)
            s = self.db.get_session(s["id"])
            context = s["context"]
            context[0] = {**context[0], "content": context[0]["content"].replace("{base_branch}", info["base_branch"])}
            with self.db.tx():
                self.db.update_session(s["id"], context=context, **info)
                self.bus.emit(s["id"], "workspace_ready", {"repo": project.repo, **info})
        elif not s["run"].get("origin_refreshed"):
            error = await self._refresh_origin(s, ws, remote, member)
            if error:
                self.bus.emit(s["id"], "error", {"message": f"could not refresh origin: {error}"})
            run = self.db.get_session(s["id"])["run"]
            self.db.update_session(s["id"], run={**run, "origin_refreshed": True})

    async def _first_prepare(self, s: dict, project, ws: Path, remote: bool, member: bool) -> dict:
        if remote:
            return await self.hub.call(s["target"], "prepare", {"session": s["id"], "repo": project.repo,
                                                                "base_branch": project.base_branch}, timeout=900)
        if member:
            from . import clone, storage
            uid = session_user_id(s)
            root = storage.workspaces_dir(self.cfg, uid)
            try:
                return await asyncio.to_thread(
                    clone.isolated_prepare, ws, project.repo, s["id"], root,
                    self._member_clone_budget(uid))
            except clone.QuotaExceeded as e:
                raise projects.GitError(str(e)) from e
        return await asyncio.to_thread(projects.prepare, project, ws, s["id"])

    async def _refresh_origin(self, s: dict, ws: Path, remote: bool, member: bool):
        if remote:
            return await self.hub.call(s["target"], "refresh_origin", {"session": s["id"]}, timeout=400)
        if member:
            from . import clone, storage
            from .fileops import dir_size
            uid = session_user_id(s)
            remaining = self._member_clone_budget(uid)
            cap = None if remaining is None else remaining + dir_size(ws)
            return await asyncio.to_thread(
                clone.isolated_refresh_origin, ws, storage.user_root(self.cfg, uid), cap)
        return await asyncio.to_thread(projects.refresh_origin, ws)

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
            detail, reason, error = await self._ask_details(s, name, args, ws, decision.reason)
            if error is not None:
                self._record_result(sid, call, name, error, ok=False)
                return error
            existing = {"id": "a-" + uuid.uuid4().hex[:8], "session_id": sid, "tool_call_id": call["id"],
                        "tool": name, "args": args, "reason": reason, "detail": detail}
            extra = await self._review_ask(s, name, args, decision)
            existing = self._persist_ask(sid, existing, extra)

        if existing["status"] == "pending":
            self.scheduler.release(sid)
            self.set_status(sid, "waiting_approval")
            existing = await self._wait_approval(existing["id"])
            await self._acquire(sid)
        if existing["status"] == "approved":
            return self._member_tool_block(s, call, name, ws)
        note = f" Their note: {existing['note']}" if existing.get("note") else ""
        output = f"Error: the user denied this {name} call.{note} Don't retry it; choose another approach or explain."
        self._record_result(sid, call, name, output, ok=False)
        return output

    def _member_tool_block(self, s: dict, call: dict, name: str, ws: Workspace) -> str | None:
        """An approved call still can't run for a household member without the tool; the recorded error, else None."""
        if session_user_id(s) == OWNER_USER_ID:
            return None
        if name in {t["function"]["name"] for t in self.tool_schemas(s, ws)}:
            return None
        output = "Error: this account cannot use that tool."
        self._record_result(s["id"], call, name, output, ok=False)
        return output

    async def _ask_details(self, s: dict, name: str, args: dict, ws: Workspace,
                           reason: str) -> tuple[str, str, str | None]:
        """What the user sees when asked to approve a call: (detail, reason, error). An error means the call can't
        be applied as proposed, so the agent is told instead of bothering the user."""
        if name in ("write_file", "edit_file"):
            return await ws.preview(name, args), reason, None
        kit = next((k for k in self.daemon_toolkits(s) if name in k.tool_names and hasattr(k, "preview")), None)
        if kit is None:
            return "", reason, None
        try:
            detail, warning = await kit.preview(name, args)
        except ToolError as e:
            return "", reason, f"Error: {e}"
        return detail, f"{reason} {warning}".strip(), None

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
        context, _ = compaction.elide(s["context"])
        after = compaction.estimate_tokens(context, cpt) + overhead
        data = {"tier": "elide", "tokens_before": before, "tokens_after": after}
        if after >= self.cfg.summarize_at * n:
            split = compaction.split_for_summary(context, keep_chars=int(self.cfg.keep_recent * n * cpt))
            if split:
                context = await self._summarize_context(s, context, split, data, before, cpt, overhead)
        data["context_tokens"] = n
        with self.db.tx():
            self.db.update_session(sid, context=context)
            self.bus.emit(sid, "compaction", data)
        return self.db.get_session(sid)

    async def _summarize_context(self, s: dict, context: list, split: tuple[int, int], data: dict, before: int,
                                 cpt: float, overhead: int) -> list:
        """Replace the oldest messages with a model-written summary; `data` gets the compaction details."""
        sid = s["id"]
        model = self.cfg.models[s["model"]]
        n = model.context_tokens
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
            return context
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
        return context

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
    async def _take_pending_cancel(self, sid: str) -> bool:
        """If the user cancelled, finalize as cancelled and skip any failure path. False if not pending."""
        if sid not in self.user_cancelled:
            return False
        s = self.db.get_session(sid)
        if s["status"] in ("cancelled", "done"):
            return False
        self._record_cancel(sid)
        await self._end_run(sid)
        return True

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
