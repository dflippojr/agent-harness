"""Session operations used by the API: create, message, approve, cancel, and recovery at startup."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from pathlib import Path

from .bus import EventBus
from .changes import workspace_changes
from .maintenance import Maintenance
from .notify import Notifier
from .warmup import ModelWarmer
from .config import Config
from .db import Database
from .runner import ACTIVE, HOMELAB_PROMPT, REPO_PROMPT, SYSTEM_PROMPT, Runner, new_run
from .scheduler import GpuScheduler
from . import llm, projects

log = logging.getLogger("harness.manager")

TARGETS = ("tower", "macbook")


def public_approval(a: dict | None) -> dict | None:
    return a and {k: v for k, v in a.items() if k != "token"}


class HarnessError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Manager:
    def __init__(self, cfg: Config, db: Database | None = None, chat=llm.chat):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.bus = EventBus(self.db)
        self.scheduler = GpuScheduler(self._queue_changed)
        self.warmer = ModelWarmer()
        self.runner = Runner(cfg, self.db, self.bus, self.scheduler, chat=chat, warmer=self.warmer)
        self.tasks: dict[str, asyncio.Task] = {}
        self.notifier = Notifier(cfg, self.db)
        self.bus.add_listener(self.notifier.listener)
        self.maintenance = Maintenance(cfg, self.db, self.runner)

    def _queue_changed(self, positions: dict[str, int]) -> None:
        for sid, position in positions.items():
            self.bus.ephemeral(sid, "queue", {"position": position})

    # lifecycle
    async def start(self, maintenance: bool = True) -> None:
        self.notifier.start()
        if maintenance:
            self.maintenance.start()
        for s in self.db.sessions_with_status(*ACTIVE):
            log.info("resuming session %s (%s)", s["id"], s["status"])
            self._spawn(s["id"], recovered=True)

    async def stop(self) -> None:
        """Daemon shutdown: stop tasks but leave session state as-is so the next start resumes them."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.notifier.stop()
        await self.maintenance.stop()

    def _spawn(self, sid: str, recovered: bool = False) -> None:
        task = asyncio.create_task(self.runner.run(sid, recovered=recovered), name=f"session-{sid}")
        self.tasks[sid] = task
        task.add_done_callback(lambda t, sid=sid: self.tasks.pop(sid, None) if self.tasks.get(sid) is t else None)

    def resolve_id(self, ref: str) -> str:
        ids = self.db.find_session_ids(ref)
        if ref in ids:
            return ref
        if len(ids) != 1:
            raise HarnessError(404 if not ids else 400,
                               f"no session matches {ref!r}" if not ids else f"{ref!r} is ambiguous")
        return ids[0]

    def get(self, ref: str) -> dict:
        return self.db.get_session(self.resolve_id(ref))

    # operations
    def create(self, prompt: str, project: str = "scratch", target: str = "tower", model: str | None = None,
               title: str | None = None) -> dict:
        if not prompt.strip():
            raise HarnessError(400, "prompt is empty")
        if project not in self.cfg.projects:
            raise HarnessError(400, f"unknown project {project!r}; known: {', '.join(self.cfg.projects)}")
        if target not in TARGETS:
            raise HarnessError(400, f"target must be one of {TARGETS}")
        if target != "tower":
            raise HarnessError(501, "the macbook target arrives in Phase 4")
        model = model or self.cfg.default_model
        if model not in self.cfg.models:
            raise HarnessError(400, f"unknown model {model!r}; known: {', '.join(self.cfg.models)}")
        self.cfg.workspaces_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(self.cfg.workspaces_dir).free / 2**30
        if free_gb < self.cfg.cleanup.min_free_gb:
            raise HarnessError(507, f"only {free_gb:.1f} GB free on the data drive "
                                    f"(minimum {self.cfg.cleanup.min_free_gb} GB); run cleanup first")

        sid = uuid.uuid4().hex[:10]
        workspace = self.cfg.workspaces_dir / sid
        workspace.mkdir(parents=True, exist_ok=False)
        spec = self.cfg.projects[project]
        system = SYSTEM_PROMPT
        branch = ""
        if spec.repo:
            branch = projects.branch_name(sid)
            repo_name = spec.repo.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".git")
            # The base branch is filled in once the repo is cloned (runner._prepare_repo).
            system += "\n\n" + REPO_PROMPT.format(repo_name=repo_name, branch=branch, base_branch="{base_branch}")
        if spec.homelab:
            system += "\n\n" + HOMELAB_PROMPT
        instructions = spec.instructions.strip()
        if instructions:
            system += f"\n\nProject instructions ({project}):\n{instructions}"
        now = time.time()
        first_line = prompt.strip().splitlines()[0]
        session = {
            "id": sid, "project": project, "target": target, "model": model,
            "title": title or (first_line[:80] + ("…" if len(first_line) > 80 else "")),
            "status": "queued", "workspace": str(workspace), "created_at": now, "updated_at": now,
            "context": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "run": new_run(), "totals": {}, "inbox": [], "branch": branch,
        }
        with self.db.tx():
            self.db.insert_session(session)
            self.bus.emit(sid, "session_created", {k: session[k] for k in ("project", "target", "model", "title")})
            self.bus.emit(sid, "user_message", {"content": prompt})
        self._spawn(sid)
        return self.db.get_session(sid)

    async def send(self, ref: str, content: str) -> dict:
        sid = self.resolve_id(ref)
        if not content.strip():
            raise HarnessError(400, "message is empty")
        s = self.db.get_session(sid)
        if s["workspace_removed"]:
            raise HarnessError(409, "this session's workspace was cleaned up or discarded; run it again as a new session")
        task = self.tasks.get(sid)
        if task and s["status"] not in ACTIVE:
            await asyncio.gather(task, return_exceptions=True)  # a finished run still wrapping up
            s = self.db.get_session(sid)
        with self.db.tx():
            self.bus.emit(sid, "user_message", {"content": content})
            if s["status"] in ACTIVE:
                # Delivered before the agent's next model call.
                self.db.update_session(sid, inbox=s["inbox"] + [content])
            else:
                self.db.update_session(sid, context=s["context"] + [{"role": "user", "content": content}],
                                       run=new_run(carry=s["run"]), status="queued", stop_reason="", answer="")
                self.bus.emit(sid, "status", {"status": "queued"})
        if sid not in self.tasks:
            self._spawn(sid)
        return self.db.get_session(sid)

    def original_prompt(self, sid: str) -> str:
        for e in self.db.events(sid):
            if e["type"] == "user_message":
                return e["data"]["content"]
        return self.db.get_session(sid)["context"][1]["content"]

    def rerun(self, ref: str) -> dict:
        """Start a fresh session with the same task, project, and model."""
        s = self.get(ref)
        return self.create(self.original_prompt(s["id"]), project=s["project"], target=s["target"],
                           model=s["model"] if s["model"] in self.cfg.models else None, title=s["title"])

    async def changes(self, ref: str) -> dict:
        s = self.get(ref)
        if s["workspace_removed"]:
            return {"repos": [], "removed": True}
        return await asyncio.to_thread(workspace_changes, Path(s["workspace"]), s["base_commit"] or None)

    # review of a git project's session branch
    async def review(self, ref: str, action: str) -> dict:
        """merge (local projects), push (URL projects), or discard. Runs host-side with the user's git setup."""
        sid = self.resolve_id(ref)
        s = self.db.get_session(sid)
        project = self.cfg.projects.get(s["project"])
        if not project or not project.repo or not s["branch"]:
            raise HarnessError(400, "this session isn't on a git project branch")
        if s["status"] in ACTIVE:
            raise HarnessError(409, "the agent is still working; wait for the run to end or cancel it")
        if sid in self.tasks:  # the run ended and is still saving its branch
            await asyncio.gather(self.tasks[sid], return_exceptions=True)
            s = self.db.get_session(sid)
        if s["review"] == "discarded" or (s["workspace_removed"] and action != "discard"):
            raise HarnessError(409, "the session's workspace is gone")
        if not s["base_commit"]:
            raise HarnessError(409, "the repository was never checked out")
        ws = Path(s["workspace"])
        try:
            if action == "merge":
                result = await asyncio.to_thread(projects.merge, project, ws, sid, s["branch"], s["base_branch"],
                                                 s["title"])
                state, detail = ("merged" if result["merged"] else ""), result["message"]
            elif action == "push":
                await asyncio.to_thread(projects.snapshot, ws, f"Work in progress from session {sid}")
                detail, state = await asyncio.to_thread(projects.push, project, ws, s["branch"]), "pushed"
            elif action == "discard":
                await asyncio.to_thread(projects.discard, project, s["branch"])
                await self.runner.sandbox(s).remove()
                if not s["workspace_removed"]:
                    await asyncio.to_thread(self.maintenance.remove_workspace, sid)
                state, detail = "discarded", "branch deleted and workspace removed"
            else:
                raise HarnessError(404, f"unknown review action {action!r}")
        except projects.GitError as e:
            self.bus.emit(sid, "error", {"message": f"{action} failed: {e}"})
            raise HarnessError(e.status, str(e))
        head = "" if action == "discard" else await asyncio.to_thread(projects.head, ws)
        with self.db.tx():
            self.db.update_session(sid, review=state, review_detail=detail)
            self.bus.emit(sid, "review", {"action": action, "state": state, "detail": detail, "head": head[:12]})
        self.runner.write_transcript(sid)
        return self.db.get_session(sid)

    def decide_by_token(self, token: str, approve: bool) -> dict:
        approval = self.db.approval_by_token(token)
        if approval is None:
            raise HarnessError(404, "unknown approval link")
        if approval["status"] != "pending":
            return public_approval(approval)  # a repeated button press is harmless
        return self.decide(approval["session_id"], approval["id"], approve, note="")

    def decide(self, ref: str, approval_id: str | None, approve: bool, note: str = "") -> dict:
        sid = self.resolve_id(ref)
        pending = self.db.pending_approvals(sid)
        if approval_id is None:
            if len(pending) != 1:
                raise HarnessError(400 if pending else 404,
                                   f"{len(pending)} pending approvals; pass an approval id")
            approval_id = pending[0]["id"]
        approval = self.db.get_approval(approval_id)
        if approval is None or approval["session_id"] != sid:
            raise HarnessError(404, f"no approval {approval_id} in session {sid}")
        status = "approved" if approve else "denied"
        with self.db.tx():
            if not self.db.decide_approval(approval_id, status, note):
                raise HarnessError(409, f"approval is already {self.db.get_approval(approval_id)['status']}")
            self.bus.emit(sid, "approval_decided", {"id": approval_id, "status": status, "note": note})
        event = self.runner.approval_events.get(approval_id)
        if event:
            event.set()
        return public_approval(self.db.get_approval(approval_id))

    async def cancel(self, ref: str) -> dict:
        sid = self.resolve_id(ref)
        task = self.tasks.get(sid)
        s = self.db.get_session(sid)
        if s["status"] not in ACTIVE:
            raise HarnessError(409, f"session is {s['status']}, nothing to cancel")
        if task is None:  # no live task (shouldn't happen); fix the record anyway
            self.runner.set_status(sid, "cancelled", stop_reason="cancelled")
            return self.db.get_session(sid)
        self.runner.user_cancelled.add(sid)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return self.db.get_session(sid)

    def summary(self, s: dict) -> dict:
        out = {k: v for k, v in s.items() if k not in ("context", "inbox")}
        out["queue_position"] = self.scheduler.positions().get(s["id"])
        out["last_event_seq"] = self.db.last_event_seq(s["id"])
        project = self.cfg.projects.get(s["project"])
        out["repo_kind"] = ("" if not project or not project.repo else
                            "url" if projects.is_url(project.repo) else "local")
        if s["status"] == "waiting_approval":
            out["pending_approvals"] = [public_approval(a) for a in self.db.pending_approvals(s["id"])]
        return out
