"""HTTP API and web app. Bound to localhost; `tailscale serve` publishes it on the tailnet over HTTPS."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as config_mod
from . import transcript
from .manager import HarnessError, Manager, public_approval

log = logging.getLogger("harness.api")
WEB = Path(__file__).parent / "web"
# Session-list stream: status-level events only, no tool output or token deltas.
GLOBAL_TYPES = {"session_created", "status", "approval_requested", "approval_decided", "run_finished", "queue"}


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


class Template(BaseModel):
    name: str
    project: str = "scratch"
    model: str = ""
    prompt: str


def sse(event: dict) -> str:
    head = f"id: {event['seq']}\n" if event.get("seq") is not None else ""
    return f"{head}event: {event['type']}\ndata: {json.dumps(event)}\n\n"


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

    @app.middleware("http")
    async def guard(request: Request, call_next):
        m: Manager = request.app.state.manager
        cfg = m.cfg
        # `tailscale serve` adds the caller's identity. Requests without it can only come from this machine.
        login = request.headers.get("tailscale-user-login")
        if login is not None and cfg.allowed_logins and login not in cfg.allowed_logins:
            log.warning("refused %s %s from tailnet login %s", request.method, request.url.path, login)
            return JSONResponse({"detail": "this tailnet login is not allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Browsers send Origin on POSTs: refuse cross-site requests (a web page can't drive the agent).
            origin = request.headers.get("origin")
            allowed = {cfg.public_url, f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}"}
            if origin and origin not in allowed or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(HarnessError)
    async def harness_error(request: Request, exc: HarnessError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    # web app
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        # Served from the root so its scope covers the whole app.
        return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    # API
    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/me")
    async def me(request: Request):
        cfg = mgr(request).cfg
        return {
            "login": request.headers.get("tailscale-user-login"),
            "name": request.headers.get("tailscale-user-name"),
            "public_url": cfg.public_url,
            "notify": {"enabled": cfg.notify.enabled, "topic": cfg.notify.topic},
        }

    @app.get("/projects")
    async def projects(request: Request):
        cfg = mgr(request).cfg
        return [{"name": p.name, "description": p.description} for p in cfg.projects.values()]

    @app.get("/models")
    async def models(request: Request):
        cfg = mgr(request).cfg
        return [{"name": m.name, "context_tokens": m.context_tokens, "default": m.name == cfg.default_model}
                for m in cfg.models.values()]

    @app.get("/models/status")
    async def models_status(request: Request):
        m = mgr(request)
        return [{"name": mc.name, "state": await m.warmer.state(mc), "waking_seconds": m.warmer.waking_for(mc)}
                for mc in m.cfg.models.values()]

    @app.post("/models/warm")
    async def models_warm(request: Request):
        """Load the default model if it's asleep. The web app calls this when it opens."""
        m = mgr(request)
        model = m.cfg.models[m.cfg.default_model]
        return {"name": model.name, "state": await m.warmer.warm(model)}

    @app.get("/queue")
    async def queue(request: Request):
        positions = mgr(request).scheduler.positions()
        return [{"session_id": sid, "position": pos} for sid, pos in sorted(positions.items(), key=lambda x: x[1])]

    @app.get("/sessions")
    async def list_sessions(request: Request, limit: int = 50):
        m = mgr(request)
        out = []
        for s in m.db.list_sessions(limit):
            item = m.summary(s)
            if s["status"] in ("done", "failed", "cancelled"):
                item["answer_preview"] = (m.db.get_session(s["id"])["answer"] or "")[:200]
            out.append(item)
        return out

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

    @app.post("/sessions/{ref}/rerun", status_code=201)
    async def rerun(ref: str, request: Request):
        m = mgr(request)
        return m.summary(m.rerun(ref))

    @app.get("/sessions/{ref}/changes")
    async def changes(ref: str, request: Request):
        return await mgr(request).changes(ref)

    @app.get("/sessions/{ref}/approvals")
    async def approvals(ref: str, request: Request, all: bool = False):
        m = mgr(request)
        sid = m.resolve_id(ref)
        rows = m.db.approvals(sid) if all else m.db.pending_approvals(sid)
        return [public_approval(a) for a in rows]

    @app.post("/sessions/{ref}/approvals/{approval_id}")
    async def decide(ref: str, approval_id: str, body: Decision, request: Request):
        if body.decision not in ("approve", "deny"):
            raise HarnessError(400, "decision must be approve or deny")
        return mgr(request).decide(ref, None if approval_id == "pending" else approval_id,
                                   body.decision == "approve", body.note)

    @app.post("/a/{token}/{decision}")
    async def decide_by_token(token: str, decision: str, request: Request):
        """Target of the notification's Approve / Deny buttons. The token is the credential."""
        if decision not in ("approve", "deny"):
            raise HarnessError(404, "not found")
        a = mgr(request).decide_by_token(token, decision == "approve")
        return {"id": a["id"], "status": a["status"]}

    @app.post("/sessions/{ref}/cancel")
    async def cancel(ref: str, request: Request):
        m = mgr(request)
        return m.summary(await m.cancel(ref))

    @app.get("/sessions/{ref}/transcript", response_class=PlainTextResponse)
    async def get_transcript(ref: str, request: Request):
        m = mgr(request)
        return transcript.render(m.db, m.resolve_id(ref))

    # templates
    @app.get("/templates")
    async def list_templates(request: Request):
        return mgr(request).db.list_templates()

    @app.post("/templates", status_code=201)
    async def create_template(body: Template, request: Request):
        return _save_template(mgr(request), "t-" + uuid.uuid4().hex[:8], body)

    @app.put("/templates/{tid}")
    async def update_template(tid: str, body: Template, request: Request):
        m = mgr(request)
        if m.db.get_template(tid) is None:
            raise HarnessError(404, "no such template")
        return _save_template(m, tid, body)

    @app.delete("/templates/{tid}", status_code=204)
    async def delete_template(tid: str, request: Request):
        if not mgr(request).db.delete_template(tid):
            raise HarnessError(404, "no such template")

    def _save_template(m: Manager, tid: str, body: Template) -> dict:
        if not body.name.strip() or not body.prompt.strip():
            raise HarnessError(400, "name and prompt are required")
        if body.project not in m.cfg.projects:
            raise HarnessError(400, f"unknown project {body.project!r}")
        if body.model and body.model not in m.cfg.models:
            raise HarnessError(400, f"unknown model {body.model!r}")
        m.db.upsert_template({"id": tid, **body.model_dump()})
        return m.db.get_template(tid)

    # notifications
    @app.post("/notify/test")
    async def notify_test(request: Request):
        m = mgr(request)
        if not m.cfg.notify.enabled:
            raise HarnessError(400, "notifications are disabled in config/harness.yaml")
        payload = {"topic": m.cfg.notify.topic, "title": "Agent harness", "message": "Test notification 👋",
                   "tags": ["robot"], "click": m.notifier.link("/")}
        async with httpx.AsyncClient(timeout=15) as client:
            await m.notifier.publish(client, payload)
        return {"sent": True}

    # event streams
    @app.get("/events")
    async def all_events(request: Request):
        """Status-level events for every session (the session list). Live only; reload the list to catch up."""
        m = mgr(request)

        async def stream():
            sub = m.bus.subscribe("*")
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        e = await asyncio.wait_for(sub.queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": keepalive\n\n"
                        continue
                    if e["type"] in GLOBAL_TYPES:
                        if e["type"] == "run_finished":
                            e = {**e, "data": {k: v for k, v in e["data"].items() if k != "run"}}
                        yield sse(e)
            finally:
                m.bus.unsubscribe("*", sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/sessions/{ref}/events")
    async def events(ref: str, request: Request, after: int = 0, follow: bool = True):
        """Server-sent events: replays persisted events after `after`, then streams live ones.
        Ephemeral events (token deltas, queue moves) have `seq: null` and are never replayed."""
        m = mgr(request)
        sid = m.resolve_id(ref)
        if request.headers.get("last-event-id", "").isdigit():  # EventSource reconnects resume by itself
            after = max(after, int(request.headers["last-event-id"]))

        async def stream():
            sub = m.bus.subscribe(sid)
            last = after
            try:
                yield ": connected\n\n"
                for e in m.db.events(sid, after):
                    last = e["seq"]
                    yield sse(e)
                if not follow:
                    return
                while True:
                    if sub.overflowed:
                        sub.overflowed = False
                        while not sub.queue.empty():
                            sub.queue.get_nowait()
                        for e in m.db.events(sid, last):
                            last = e["seq"]
                            yield sse(e)
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
                    yield sse(e)
            finally:
                m.bus.unsubscribe(sid, sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
