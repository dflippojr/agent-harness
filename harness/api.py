"""HTTP API. Bound to localhost; Phase 2 publishes it on the tailnet with `tailscale serve`."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import config as config_mod
from . import transcript
from .manager import HarnessError, Manager

TERMINAL = ("done", "cancelled", "failed")


class CreateSession(BaseModel):
    prompt: str
    project: str = "scratch"
    target: str = "tower"
    model: str | None = None
    title: str | None = None


class SendMessage(BaseModel):
    content: str


class Decision(BaseModel):
    decision: str  # approve | deny
    note: str = ""


def create_app(manager: Manager | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = manager or Manager(config_mod.load())
        await app.state.manager.start()
        yield
        await app.state.manager.stop()

    app = FastAPI(title="agent-harness", lifespan=lifespan)

    def mgr(request: Request) -> Manager:
        return request.app.state.manager

    @app.exception_handler(HarnessError)
    async def harness_error(request: Request, exc: HarnessError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/projects")
    async def projects(request: Request):
        cfg = mgr(request).cfg
        return [{"name": p.name, "description": p.description} for p in cfg.projects.values()]

    @app.get("/models")
    async def models(request: Request):
        cfg = mgr(request).cfg
        return [{"name": m.name, "context_tokens": m.context_tokens, "default": m.name == cfg.default_model}
                for m in cfg.models.values()]

    @app.get("/queue")
    async def queue(request: Request):
        positions = mgr(request).scheduler.positions()
        return [{"session_id": sid, "position": pos} for sid, pos in sorted(positions.items(), key=lambda x: x[1])]

    @app.get("/sessions")
    async def list_sessions(request: Request, limit: int = 50):
        m = mgr(request)
        return [m.summary(s) for s in m.db.list_sessions(limit)]

    @app.post("/sessions", status_code=201)
    async def create_session(body: CreateSession, request: Request):
        m = mgr(request)
        s = m.create(body.prompt, project=body.project, target=body.target, model=body.model, title=body.title)
        return m.summary(s)

    @app.get("/sessions/{ref}")
    async def get_session(ref: str, request: Request):
        m = mgr(request)
        return m.summary(m.get(ref))

    @app.post("/sessions/{ref}/messages")
    async def send_message(ref: str, body: SendMessage, request: Request):
        m = mgr(request)
        return m.summary(await m.send(ref, body.content))

    @app.get("/sessions/{ref}/approvals")
    async def approvals(ref: str, request: Request):
        m = mgr(request)
        return m.db.pending_approvals(m.resolve_id(ref))

    @app.post("/sessions/{ref}/approvals/{approval_id}")
    async def decide(ref: str, approval_id: str, body: Decision, request: Request):
        if body.decision not in ("approve", "deny"):
            raise HarnessError(400, "decision must be approve or deny")
        return mgr(request).decide(ref, None if approval_id == "pending" else approval_id,
                                   body.decision == "approve", body.note)

    @app.post("/sessions/{ref}/cancel")
    async def cancel(ref: str, request: Request):
        m = mgr(request)
        return m.summary(await m.cancel(ref))

    @app.get("/sessions/{ref}/transcript", response_class=PlainTextResponse)
    async def get_transcript(ref: str, request: Request):
        m = mgr(request)
        return transcript.render(m.db, m.resolve_id(ref))

    @app.get("/sessions/{ref}/events")
    async def events(ref: str, request: Request, after: int = 0, follow: bool = True):
        """Server-sent events: replays persisted events after `after`, then streams live ones.
        Ephemeral events (token deltas, queue moves) have `seq: null` and are never replayed."""
        m = mgr(request)
        sid = m.resolve_id(ref)

        async def stream():
            sub = m.bus.subscribe(sid)
            last = after
            try:
                for e in m.db.events(sid, after):
                    last = e["seq"]
                    yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n"
                if not follow:
                    return
                while True:
                    if sub.overflowed:
                        sub.overflowed = False
                        while not sub.queue.empty():
                            sub.queue.get_nowait()
                        for e in m.db.events(sid, last):
                            last = e["seq"]
                            yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n"
                    try:
                        e = await asyncio.wait_for(sub.queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": keepalive\n\n"
                        continue
                    if e["seq"] is not None:
                        if e["seq"] <= last:
                            continue
                        last = e["seq"]
                        yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n"
                    else:
                        yield f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
            finally:
                m.bus.unsubscribe(sid, sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
