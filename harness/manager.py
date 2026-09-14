"""Session operations used by the API: create, message, approve, cancel, and recovery at startup."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .bus import EventBus
from .config import Config
from .db import Database
from .runner import ACTIVE, SYSTEM_PROMPT, Runner, new_run
from .scheduler import GpuScheduler
from . import llm

log = logging.getLogger("harness.manager")

TARGETS = ("tower", "macbook")


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
        self.runner = Runner(cfg, self.db, self.bus, self.scheduler, chat=chat)
        self.tasks: dict[str, asyncio.Task] = {}

    def _queue_changed(self, positions: dict[str, int]) -> None:
        for sid, position in positions.items():
            self.bus.ephemeral(sid, "queue", {"position": position})

    # lifecycle
    async def start(self) -> None:
        for s in self.db.sessions_with_status(*ACTIVE):
            log.info("resuming session %s (%s)", s["id"], s["status"])
            self._spawn(s["id"], recovered=True)

    async def stop(self) -> None:
        """Daemon shutdown: stop tasks but leave session state as-is so the next start resumes them."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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

        sid = uuid.uuid4().hex[:10]
        workspace = self.cfg.workspaces_dir / sid
        workspace.mkdir(parents=True, exist_ok=False)
        system = SYSTEM_PROMPT
        instructions = self.cfg.projects[project].instructions.strip()
        if instructions:
            system += f"\n\nProject instructions ({project}):\n{instructions}"
        now = time.time()
        first_line = prompt.strip().splitlines()[0]
        session = {
            "id": sid, "project": project, "target": target, "model": model,
            "title": title or (first_line[:80] + ("…" if len(first_line) > 80 else "")),
            "status": "queued", "workspace": str(workspace), "created_at": now, "updated_at": now,
            "context": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "run": new_run(), "totals": {}, "inbox": [],
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
        return self.db.get_approval(approval_id)

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
        if s["status"] == "waiting_approval":
            out["pending_approvals"] = self.db.pending_approvals(s["id"])
        return out
