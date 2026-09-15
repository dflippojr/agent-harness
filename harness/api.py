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
RUNNER_BODY_LIMIT = 16 * 2**20  # a result carries at most a capped command output or file read


class CreateSession(BaseModel):
    prompt: str
    project: str = "scratch"
    target: str | None = None  # default: the project's target
    backend: str = "local"
    model: str | None = None
    title: str | None = None


class SendMessage(BaseModel):
    content: str


class Decision(BaseModel):
    decision: str  # approve | deny
    note: str = ""


class RunnerPoll(BaseModel):
    instance: str
    inflight: list[str] = []
    info: dict = {}


class RunnerResult(BaseModel):
    id: str
    ok: bool
    value: object = None
    error: str = ""
    kind: str = "internal"


class ImageRequest(BaseModel):
    prompt: str
    model: str = "fast"
    aspect_ratio: str = "1:1"
    seed: int | None = None


class Job(BaseModel):
    name: str
    prompt: str
    cron: str
    project: str = "scratch"
    model: str = ""
    notify: str = "low"          # OK results: attention (no notification) | low | always
    enabled: bool = True
    catch_up_minutes: int = 360


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

    from . import apps, endpoint
    endpoint.register(app, mgr)
    apps.register(app, mgr)

    # API
    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    async def metrics(request: Request):
        from .metrics import render
        m = mgr(request)
        return PlainTextResponse(await asyncio.to_thread(render, m), media_type="text/plain; version=0.0.4")

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
        return [{"name": p.name, "description": p.description, "repo": bool(p.repo), "homelab": p.homelab,
                 "target": p.target} for p in cfg.projects.values()]

    # runners (the MacBook): outbound long-polling, authenticated with a per-runner bearer token
    def runner_auth(request: Request, name: str) -> Manager:
        m = mgr(request)
        if name not in m.hub.state:
            raise HarnessError(404, "unknown runner")
        if not m.hub.authorized(name, request.headers.get("authorization")):
            log.warning("refused runner %s request from %s", name, request.client.host if request.client else "?")
            raise HarnessError(401, "bad runner token")
        return m

    @app.get("/runners")
    async def runners(request: Request):
        return mgr(request).hub.status()

    @app.post("/runners/{name}/poll")
    async def runner_poll(name: str, body: RunnerPoll, request: Request):
        m = runner_auth(request, name)
        return await m.hub.poll(name, body.instance, body.inflight, body.info)

    @app.post("/runners/{name}/results")
    async def runner_result(name: str, body: RunnerResult, request: Request):
        m = runner_auth(request, name)
        if int(request.headers.get("content-length") or 0) > RUNNER_BODY_LIMIT:
            raise HarnessError(413, "result too large")
        return {"accepted": m.hub.result(name, body.id, body.ok, body.value, body.error, body.kind)}

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

    # image generation
    def images_service(request: Request):
        m = mgr(request)
        if m.images is None:
            raise HarnessError(400, "image generation is disabled in config/harness.yaml")
        return m.images

    @app.get("/images")
    async def list_images(request: Request, limit: int = 60):
        svc = images_service(request)
        return {"status": svc.status(), "images": svc.db.list_images(limit=limit)}

    @app.post("/images", status_code=201)
    async def create_image(body: ImageRequest, request: Request):
        from .fileops import ToolError
        svc = images_service(request)
        try:
            return svc.submit(body.prompt, model=body.model, aspect_ratio=body.aspect_ratio, seed=body.seed)
        except ToolError as e:
            raise HarnessError(400, str(e))

    @app.get("/images/{iid}")
    async def get_image(iid: str, request: Request):
        svc = images_service(request)
        job = svc.db.get_image(iid.removesuffix(".png"))
        if job is None:
            raise HarnessError(404, "no such image")
        if iid.endswith(".png"):
            if job["status"] != "done" or not svc.path(job).exists():
                raise HarnessError(404, "image not ready")
            return FileResponse(svc.path(job), media_type="image/png", headers={"Cache-Control": "max-age=86400"})
        return {**job, "service": svc.status()}

    # GPU contention guard
    @app.get("/gpu")
    async def gpu(request: Request):
        m = mgr(request)
        return m.guard.status() if m.guard else {"enabled": False, "state": "clear", "signals": []}

    @app.post("/gpu/{action}")
    async def gpu_action(action: str, request: Request):
        """pause: hold the GPU for other uses until resumed. resume: reload now, ignoring the current triggers."""
        m = mgr(request)
        if m.guard is None:
            raise HarnessError(400, "the GPU guard is disabled in config/harness.yaml")
        if action == "pause":
            m.guard.pause()
        elif action == "resume":
            m.guard.resume()
        else:
            raise HarnessError(404, "unknown action")
        return m.guard.status()

    # Claude Code Remote Control servers (remote_control.py)
    def remote_control(m):
        if m.remote_control is None:
            raise HarnessError(400, "Remote Control launches are disabled (remote_control.enabled in harness.yaml)")
        return m.remote_control

    @app.get("/remote-control")
    async def rc_status(request: Request):
        m = mgr(request)
        if m.remote_control is None:
            return {"enabled": False, "projects": []}
        return {"enabled": True, "projects": m.remote_control.status()}

    @app.post("/remote-control/{project}")
    async def rc_launch(project: str, request: Request):
        from .fileops import ToolError
        rc = remote_control(mgr(request))
        try:
            return await rc.launch(project, started_by=request.headers.get("tailscale-user-login") or "web app")
        except ToolError as e:
            raise HarnessError(400, str(e))

    @app.post("/remote-control/{project}/stop")
    async def rc_stop(project: str, request: Request):
        from .fileops import ToolError
        try:
            return await remote_control(mgr(request)).stop(project)
        except ToolError as e:
            raise HarnessError(404, str(e))

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

    @app.get("/memory")
    async def memory(request: Request):
        """The agent profile new sessions get, and the latest change agents saved to the memory library."""
        m = mgr(request)
        lib, cfg = m.runner.memory, m.cfg.memory_library
        if lib is None:
            return {"enabled": False}
        profile = await asyncio.to_thread(lib.profile_text)
        return {"enabled": True, "writes": cfg.writes, "categories": cfg.categories, "profile_path": cfg.profile_path,
                "profile": profile, "profile_chars": len(profile), "profile_max_chars": cfg.profile_max_chars,
                "last_commit": lib.last_commit, "refresh_error": lib.refresh_error}

    @app.get("/search")
    async def search_sessions(request: Request, q: str = "", project: str = "", limit: int = 20):
        """Full-text search over past sessions. Passages mark matches with \\u0002 ... \\u0003."""
        from . import search
        m = mgr(request)
        if not m.cfg.search.enabled:
            raise HarnessError(400, "session search is disabled in config/harness.yaml")
        return await asyncio.to_thread(search.search, m.db, q, project, max(1, min(limit, 50)))

    @app.post("/sessions", status_code=201)
    async def create_session(body: CreateSession, request: Request):
        m = mgr(request)
        s = m.create(body.prompt, project=body.project, target=body.target, backend=body.backend,
                     model=body.model, title=body.title)
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

    @app.post("/sessions/{ref}/review/{action}")
    async def review(ref: str, action: str, request: Request):
        """merge | push | discard the session's git branch."""
        m = mgr(request)
        return m.summary(await m.review(ref, action))

    @app.get("/maintenance")
    async def maintenance(request: Request):
        return await mgr(request).maintenance.usage()

    @app.post("/maintenance/cleanup")
    async def maintenance_cleanup(request: Request):
        return await mgr(request).maintenance.cleanup()

    @app.post("/maintenance/backup")
    async def maintenance_backup(request: Request):
        return await mgr(request).maintenance.backup()

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

    # scheduled jobs (jobs.py)
    def jobs_on(request: Request) -> Manager:
        m = mgr(request)
        if m.jobs is None:
            raise HarnessError(400, "scheduled jobs are disabled in config/harness.yaml")
        return m

    def job_view(m: Manager, job: dict, runs: int = 1) -> dict:
        recent = m.db.job_sessions(job["id"], limit=runs)
        return {**job, "enabled": bool(job["enabled"]), "recent": recent}

    @app.get("/jobs")
    async def list_jobs(request: Request):
        m = jobs_on(request)
        return [job_view(m, j) for j in m.db.list_jobs()]

    @app.get("/jobs/preview")
    async def preview_cron(cron: str, request: Request, count: int = 3):
        """The next few run times of a schedule, or why it's invalid."""
        import time as _time
        from .jobs import Cron, CronError
        try:
            c = Cron(cron)
        except CronError as e:
            return {"ok": False, "error": str(e)}
        times, t = [], _time.time()
        for _ in range(max(1, min(count, 10))):
            t = c.next_after(t)
            times.append(t)
        return {"ok": True, "cron": c.expr, "next": times}

    @app.post("/jobs", status_code=201)
    async def create_job(body: Job, request: Request):
        import time as _time
        from .jobs import Cron, CronError, new_job_id, validate
        m = jobs_on(request)
        try:
            job = validate(body.model_dump(), m.cfg.projects, m.cfg.models)
        except (ValueError, CronError) as e:
            raise HarnessError(400, str(e))
        job["id"] = new_job_id()
        job["next_run_at"] = Cron(job["cron"]).next_after(_time.time())
        m.db.insert_job(job)
        return job_view(m, m.db.get_job(job["id"]))

    @app.get("/jobs/{jid}")
    async def get_job(jid: str, request: Request):
        m = jobs_on(request)
        job = m.db.get_job(jid)
        if job is None:
            raise HarnessError(404, "no such job")
        return job_view(m, job, runs=15)

    @app.put("/jobs/{jid}")
    async def update_job(jid: str, body: Job, request: Request):
        import time as _time
        from .jobs import Cron, CronError, validate
        m = jobs_on(request)
        old = m.db.get_job(jid)
        if old is None:
            raise HarnessError(404, "no such job")
        try:
            job = validate(body.model_dump(), m.cfg.projects, m.cfg.models)
        except (ValueError, CronError) as e:
            raise HarnessError(400, str(e))
        job["next_run_at"] = Cron(job["cron"]).next_after(_time.time())
        m.db.update_job(jid, **job)
        return job_view(m, m.db.get_job(jid), runs=15)

    @app.delete("/jobs/{jid}", status_code=204)
    async def delete_job(jid: str, request: Request):
        if not jobs_on(request).db.delete_job(jid):
            raise HarnessError(404, "no such job")

    @app.post("/jobs/{jid}/run", status_code=201)
    async def run_job(jid: str, request: Request):
        """Run a job now, outside its schedule (the next scheduled run is unchanged)."""
        m = jobs_on(request)
        job = m.db.get_job(jid)
        if job is None:
            raise HarnessError(404, "no such job")
        if job["last_session_id"] and m._is_active(job["last_session_id"]):
            raise HarnessError(409, "the previous run is still going")
        sid = m.jobs.run(job, manual=True)
        return m.summary(m.db.get_session(sid))

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
