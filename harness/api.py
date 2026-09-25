"""HTTP API and web app. Bound to localhost; `tailscale serve` publishes it on the tailnet over HTTPS."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import access as access_mod
from . import compat
from . import config as config_mod
from . import transcript
from .manager import HarnessError, Manager, public_approval

NO_SUCH_JOB = "no such job"

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
    skills: list[str] | None = None


class CompareChoice(BaseModel):
    backend: str = "local"
    model: str | None = None
    effort: str | None = None


class CreateCompare(BaseModel):
    prompt: str
    project: str
    choices: list[CompareChoice]


class PickWinner(BaseModel):
    winner: str
    action: str = "merge"
    discard_rest: bool = False


class CreateProject(BaseModel):
    name: str
    description: str = ""
    target: str = "tower"
    repo: str = ""  # empty workspace, or a local folder / git URL cloned for each session


class SendMessage(BaseModel):
    content: str


class ReviewComment(BaseModel):
    repo: str = "."
    path: str
    side: str
    start_line: int
    end_line: int | None = None
    quoted: list[str]
    comment: str
    base: str = ""
    head: str = ""


class SessionUpdate(BaseModel):
    title: str


class RunSnippet(BaseModel):
    # Only these fields: no image, flags, arguments, packages, or files.
    model_config = ConfigDict(extra="forbid")
    language: str
    source: str
    origin: str = "editor"


class CreateChat(BaseModel):
    prompt: str
    backend: str = ""
    model: str = ""
    effort: str = ""


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
    resolution: str = "auto"
    seed: int | None = None
    upscale: str = "none"


class ImageUpscaleRequest(BaseModel):
    upscale: str = "2x"


class GpuHoldRequest(BaseModel):
    duration_seconds: int | None = None


class ProfileUpdate(BaseModel):
    emoji: str


class BackendUpdate(BaseModel):
    model: str | None = None
    effort: str | None = None


class SmartApprovalsUpdate(BaseModel):
    mode: str


class MemoryProfileUpdate(BaseModel):
    content: str
    summary: str = "Update agent profile"


class SkillInstall(BaseModel):
    content_hash: str


class SkillAllowlist(BaseModel):
    projects: list[str] = []


class SkillReject(BaseModel):
    reason: str = ""


class ImageArchiveRetentionApply(BaseModel):
    confirmation: str


class Job(BaseModel):
    name: str
    prompt: str
    cron: str
    project: str = "scratch"
    backend: str = "local"
    model: str = ""
    notify: str = "low"          # OK results: attention (no notification) | low | always
    enabled: bool = True
    catch_up_minutes: int = 360


class Template(BaseModel):
    name: str
    project: str = "scratch"
    backend: str = "local"
    model: str = ""
    prompt: str


def sse(event: dict) -> str:
    head = f"id: {event['seq']}\n" if event.get("seq") is not None else ""
    return f"{head}event: {event['type']}\ndata: {json.dumps(event)}\n\n"


IMAGE_NOT_READY = "image not ready"


class RouteTable:
    """Collects route handlers at import time and installs them, in order, on a FastAPI app.

    Unlike ``APIRouter`` this registers plain ``APIRoute`` entries on the app, which
    ``admin.register`` inspects to build the versioned owner API.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[str, str, object, dict]] = []

    def _route(self, method: str, path: str, **kwargs):
        def register(fn):
            self._routes.append((method, path, fn, kwargs))
            return fn
        return register

    def get(self, path: str, **kwargs):
        return self._route("get", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self._route("post", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self._route("put", path, **kwargs)

    def patch(self, path: str, **kwargs):
        return self._route("patch", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self._route("delete", path, **kwargs)

    def install(self, app: FastAPI) -> None:
        for method, path, fn, kwargs in self._routes:
            getattr(app, method)(path, **kwargs)(fn)


web_router = RouteTable()
api_router = RouteTable()


def mgr(request: Request) -> Manager:
    return request.app.state.manager


def owner_id(request: Request) -> str:
    ident = request.state.access
    if ident.kind == "member":
        return ident.user_id
    return "owner" if ident.role == "owner" else f"guest:{ident.login or 'unknown'}"



def owned_session(request: Request, ref: str, *, kind: str = "agent") -> tuple[Manager, str, dict]:
    """Resolve a human-facing conversation of one kind only inside the caller's durable user scope."""
    m = mgr(request)
    ident = request.state.access
    if ident.role == "guest":
        raise HarnessError(404, "no session matches that id")
    scope = owner_id(request)
    sid = m.resolve_id(ref, user_id=scope, kind=kind)
    session = m.db.get_session(sid)
    if (session is None or session.get("owner_id", "owner") != scope
            or (session.get("kind") or "agent") != kind):
        m.db.insert_audit(scope, scope, "cross_user", "denied")
        raise HarnessError(404, "no chat matches that id" if kind == "chat" else "no session matches that id")
    return m, sid, session


def require_owner(request: Request) -> Manager:
    # Compatibility routes normally rely on Tailscale identity. If a bearer credential is supplied, enforce
    # its kind too so an app/device token can never reveal owner-only paths or operate maintenance.
    from .admin import require_admin
    require_admin(request, mgr)
    return mgr(request)


@dataclass
class _OriginInfo:
    raw: str
    origin: str
    browser_api: bool
    cross_origin_api: bool
    cors_headers: dict


def _origin_info(request: Request, m: Manager, public_path: str) -> _OriginInfo:
    from .apps import cors_origin_allowed, daemon_origins, normalize_origin
    raw_origin = request.headers.get("origin", "")
    try:
        origin = normalize_origin(raw_origin) if raw_origin else ""
    except ValueError:
        origin = ""
    browser_api = (public_path == "/health" or public_path.startswith("/api/v1")
                   or public_path.startswith("/api/admin/v1"))
    cross_origin_api = bool(origin and origin not in daemon_origins(m.cfg)
                            and browser_api and cors_origin_allowed(m, request, origin))
    cors_headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if cross_origin_api else {}
    return _OriginInfo(raw_origin, origin, browser_api, cross_origin_api, cors_headers)


def _access_refusal(request: Request, m: Manager, ident, login: str | None,
                    cors_headers: dict) -> JSONResponse | None:
    """403 when the tailnet login, guest scope, or member scope may not make this request."""
    if not ident.allowed:
        log.warning("refused %s %s from tailnet login %s (%s)",
                    request.method, request.url.path, login, ident.detail)
        m.db.insert_audit(ident.user_id or "unknown", ident.user_id or "", "auth", "denied",
                          ident.detail or "not allowed")
        return JSONResponse({"detail": ident.detail or "this tailnet login is not allowed"},
                            status_code=403, headers=cors_headers)
    guest_block = access_mod.guest_forbidden(ident, request.method, request.url.path)
    if guest_block:
        log.warning("refused guest %s %s from %s (%s)", request.method, request.url.path, login, guest_block)
        return JSONResponse({"detail": guest_block}, status_code=403, headers=cors_headers)
    member_block = access_mod.member_forbidden(ident, request.method, request.url.path)
    if member_block:
        log.warning("refused member %s %s from %s (%s)", request.method, request.url.path, login, member_block)
        m.db.insert_audit(ident.user_id, ident.user_id, "cross_user", "denied", member_block)
        return JSONResponse({"detail": member_block}, status_code=403, headers=cors_headers)
    return None


def _compat_refusal(request: Request, compatibility: dict | None, public_path: str,
                    cors_headers: dict) -> JSONResponse | None:
    discovery = request.method in {"GET", "HEAD"} and public_path in {"/api/v1", "/api/admin/v1"}
    if not compatibility or discovery:
        return None
    if compatibility["state"] == "invalid":
        return JSONResponse({"detail": "invalid first-party client identity", "error": {
            "code": "invalid_client_identity", **compatibility,
        }}, status_code=400, headers=cors_headers)
    if compatibility["state"] in {"client_update_required", "daemon_update_required"}:
        return JSONResponse({"detail": compatibility["state"].replace("_", " "), "error": {
            "code": compatibility["state"], **compatibility,
        }}, status_code=426, headers=cors_headers)
    return None


def _cors_preflight(request: Request, info: _OriginInfo) -> Response:
    requested_method = request.headers.get("access-control-request-method", "").upper()
    requested_headers = {h.strip().lower() for h in
                         request.headers.get("access-control-request-headers", "").split(",") if h.strip()}
    if (not info.cross_origin_api or requested_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or not requested_headers <= {"authorization", "content-type", "last-event-id",
                                          "x-agent-harness-client"}):
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403,
                            headers=info.cors_headers)
    return Response(status_code=204, headers={**info.cors_headers,
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, Last-Event-ID, X-Agent-Harness-Client",
                    "Access-Control-Max-Age": "600"})


def _cross_site_refused(request: Request, m: Manager, info: _OriginInfo) -> bool:
    """Browsers send Origin on POSTs: refuse cross-site requests (a web page can't drive the agent)."""
    from .apps import daemon_origins
    if request.method in ("GET", "HEAD", "OPTIONS") or info.cross_origin_api:
        return False
    return bool((info.raw and info.origin not in daemon_origins(m.cfg))
                or request.headers.get("sec-fetch-site") == "cross-site")


async def guard(request: Request, call_next):
    m: Manager = request.app.state.manager
    public_path = request.scope.get("harness_original_path", request.url.path)
    info = _origin_info(request, m, public_path)
    # `tailscale serve` adds the caller's identity. Requests without it can only come from this machine.
    login = request.headers.get("tailscale-user-login")
    ident = access_mod.resolve_access(m.cfg, login, m.db)
    request.state.access = ident
    if ident.kind in ("owner", "member") and ident.allowed and ident.user_id != "owner":
        m.db.touch_account(ident.user_id)
    refusal = _access_refusal(request, m, ident, login, info.cors_headers)
    if refusal is not None:
        return refusal
    surface = compat.surface_for_path(public_path)
    compatibility = compat.check_client(request.headers.get(compat.CLIENT_HEADER, ""), surface) if surface else None
    refusal = _compat_refusal(request, compatibility, public_path, info.cors_headers)
    if refusal is not None:
        return refusal
    if request.method == "OPTIONS" and info.browser_api and info.raw:
        return _cors_preflight(request, info)
    if _cross_site_refused(request, m, info):
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403,
                            headers=info.cors_headers)
    response = await call_next(request)
    if compatibility and compatibility["state"] == "transition":
        response.headers["X-Agent-Harness-Deprecation"] = "missing_client_version"
        response.headers["Warning"] = '299 agent-harness "client version header will be required after this transition release"'
    for key, value in info.cors_headers.items():
        response.headers[key] = value
    return response


async def harness_error(request: Request, exc: HarnessError):
    body = {"detail": str(exc), "error": {
        "code": exc.code, "message": str(exc), "retryable": exc.status == 429 or exc.status >= 500,
    }}
    if getattr(exc, "keys", None):
        body["error"]["keys"] = exc.keys
    if getattr(exc, "details", None):
        body["error"]["details"] = exc.details
    return JSONResponse(body, status_code=exc.status)


# web app
@web_router.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@web_router.get("/sw.js", include_in_schema=False)
async def service_worker():
    # Served from the root so its scope covers the whole app.
    return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@web_router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@web_router.get("/mac-client/install.sh", include_in_schema=False)
async def mac_client_installer():
    return FileResponse(Path(__file__).parent.parent / "macrunner" / "install.sh",
                        media_type="text/x-shellscript", headers={"Cache-Control": "no-cache"})


@web_router.get("/mac-client/package.tar.gz", include_in_schema=False)
async def mac_client_package():
    from .mac_client import package_bytes
    return Response(package_bytes(), media_type="application/gzip", headers={
        "Content-Disposition": 'attachment; filename="agent-harness-mac.tar.gz"',
        "Cache-Control": "no-cache",
    })


@web_router.get("/mac-client/manifest.json", include_in_schema=False)
async def mac_client_manifest():
    from .mac_client import package_manifest
    return JSONResponse(package_manifest(), headers={"Cache-Control": "no-cache"})


# API
@api_router.get("/health")
async def health(request: Request):
    cfg = mgr(request).cfg
    settings = getattr(mgr(request), "settings", None)
    recovery = settings.store.read_status() if settings else {}
    config_view = settings.admin_view() if settings else None
    return {
        "ok": True, "profile": cfg.profile, **compat.metadata(cfg.capabilities()),
        # Which commit this process is running, when a deployer told it (staging sets HARNESS_BUILD_COMMIT).
        "build": {"commit": os.environ.get("HARNESS_BUILD_COMMIT", "").strip()},
        "config": {
            "revision": config_view["revision"] if config_view else 0,
            "confirmed": config_view["confirmed"] if config_view else True,
            "supervised_restart": bool(config_view and config_view["supervised_restart"]),
            "recovery": recovery or None,
        },
    }


@api_router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics(request: Request):
    from .metrics import render
    m = mgr(request)
    return PlainTextResponse(await asyncio.to_thread(render, m), media_type="text/plain; version=0.0.4")


@api_router.get("/me")
async def me(request: Request):
    m = mgr(request)
    cfg = m.cfg
    ident = getattr(request.state, "access", None) or access_mod.resolve_access(
        cfg, request.headers.get("tailscale-user-login"), m.db)
    guest = ident.role == "guest"
    member = ident.role == "member"
    usage = {}
    if member and ident.allowed:
        from .storage import account_usage_bytes, quota_message
        account = m.db.account_by_id(ident.user_id)
        if account:
            used = account_usage_bytes(cfg, ident.user_id)
            usage = {
                "disk_used_bytes": used,
                "disk_quota_bytes": int(account["disk_quota_bytes"]),
                "disk_note": quota_message(used, int(account["disk_quota_bytes"])),
                "max_running": int(account["max_running"]),
                "max_queued": int(account["max_queued"]),
                "running": m.db.count_sessions(ident.user_id, "running"),
                "queued": m.db.count_sessions(ident.user_id, "queued"),
                "account_hint": ident.user_id[2:10] if ident.user_id.startswith("u-") else ident.user_id[:8],
            }
    return {
        "login": ident.login,
        "name": (ident.display_name or request.headers.get("tailscale-user-name")),
        "public_url": cfg.public_url,
        "role": ident.role,
        "user_id": ident.user_id if ident.role != "guest" else None,
        "guest_until": ident.until_iso(),
        "capabilities": {
            "admin": ident.role == "owner",
            "local_sessions": ident.role in ("owner", "member"),
            "hosted_backends": ident.role == "owner",
            "images": ident.role == "owner",
            "jobs": ident.role == "owner",
            "runners": ident.role == "owner",
            "accounts": ident.role == "owner",
        },
        "usage": usage,
        "notify": {"enabled": False, "topic": ""} if guest or member else {
            "enabled": cfg.notify.enabled, "topic": cfg.notify.topic,
        },
    }


profile_emojis = ("🙂", "😎", "🤓", "🧠", "🤖", "👾", "🧑‍💻", "🦊", "🐙", "🐉", "🦉", "🐝", "🌙", "⭐", "🔥", "⚡", "🎨", "🎯", "🚀", "🛠️", "💻", "🎮", "🎧", "📚")


@api_router.get("/profile")
async def profile(request: Request):
    m = mgr(request)
    return {"emoji": m.db.get_meta("profile_emoji", "🙂"), "choices": profile_emojis}


@api_router.put("/profile")
async def update_profile(body: ProfileUpdate, request: Request):
    if body.emoji not in profile_emojis:
        raise HarnessError(400, "choose one of the available profile icons")
    m = mgr(request)
    m.db.set_meta("profile_emoji", body.emoji)
    return {"emoji": body.emoji, "choices": profile_emojis}


@api_router.get("/projects")
async def projects(request: Request):
    from . import catalog
    m = mgr(request)
    scope = owner_id(request)
    if request.state.access.role == "guest":
        return []
    return [catalog.public_project(p) for p in catalog.list_projects(m.cfg, m.db, scope)]


@api_router.post("/projects", status_code=201)
async def create_project(body: CreateProject, request: Request):
    ident = request.state.access
    if ident.role == "guest":
        raise HarnessError(403, "demo access is read-only")
    if ident.kind not in ("owner", "member") or not ident.bundled:
        raise HarnessError(403, "project creation is only for the signed-in Tailscale owner or member")
    m = mgr(request)
    if ident.role == "member":
        return m.create_member_project(ident.user_id, body.name, body.description, body.repo)
    cfg = m.cfg
    if body.target != "tower" and body.target not in cfg.runners:
        raise HarnessError(400, f"runner {body.target!r} is not configured")
    project = config_mod.Project(name=body.name, description=body.description, target=body.target,
                                 repo=body.repo, owner_id=owner_id(request), managed=True)
    try:
        config_mod.add_project(cfg, project)
    except (OSError, ValueError, TypeError) as e:
        raise HarnessError(400, str(e))
    return {"name": project.name, "description": project.description, "repo": bool(project.repo),
            "homelab": False, "target": project.target, "managed": True}


# runners (the MacBook): outbound long-polling, authenticated with a per-runner bearer token
def runner_auth(request: Request, name: str) -> Manager:
    m = mgr(request)
    if name not in m.hub.state:
        raise HarnessError(404, "unknown runner")
    if not m.hub.authorized(name, request.headers.get("authorization")):
        log.warning("refused runner %s request from %s", name, request.client.host if request.client else "?")
        raise HarnessError(401, "bad runner token")
    return m


@api_router.get("/runners")
async def runners(request: Request):
    return mgr(request).hub.status()


@api_router.post("/runners/{name}/poll")
async def runner_poll(name: str, body: RunnerPoll, request: Request):
    m = runner_auth(request, name)
    return await m.hub.poll(name, body.instance, body.inflight, body.info)


@api_router.post("/runners/{name}/results")
async def runner_result(name: str, body: RunnerResult, request: Request):
    m = runner_auth(request, name)
    if int(request.headers.get("content-length") or 0) > RUNNER_BODY_LIMIT:
        raise HarnessError(413, "result too large")
    return {"accepted": m.hub.result(name, body.id, body.ok, body.value, body.error, body.kind)}


@api_router.post("/runners/{name}/update")
async def runner_update(name: str, request: Request):
    m = mgr(request)
    from .admin import require_admin
    require_admin(request, mgr)
    if name not in m.hub.state:
        raise HarnessError(404, "unknown runner")
    state = m.hub.state[name]
    protocol = state.info.get("protocol")
    public = m.cfg.public_url or str(request.base_url).rstrip("/")
    fallback = (f"Run `harness update` on the Mac. If that command is unavailable, run "
                f"`curl -fsSL {public}/mac-client/install.sh | bash -s -- --server {public}`.")
    if not m.hub.online(name):
        raise HarnessError(409, f"runner is offline. {fallback}")
    try:
        remote_update_supported = int(protocol) == compat.PROTOCOLS["runner"]["max"]
    except (TypeError, ValueError):
        remote_update_supported = False
    if not remote_update_supported:
        raise HarnessError(409, f"runner is too old for remote update. {fallback}")
    try:
        return await m.hub.call(name, "update_client", {}, timeout=300, wait_if_offline=False)
    except Exception as exc:
        raise HarnessError(409, f"Mac client update failed: {exc}. {fallback}") from exc


@api_router.get("/models")
async def models(request: Request):
    cfg = mgr(request).cfg
    return [{"name": m.name, "context_tokens": m.context_tokens, "default": m.name == cfg.default_model}
            for m in cfg.models.values()]


@api_router.get("/backends")
async def backends(request: Request):
    m = mgr(request)
    from .backend_state import local_view, view as backend_view
    if request.state.access.role == "member":
        return [local_view(m)]
    check_auth = request.query_params.get("auth") != "skip"
    names = list(m.cfg.backends)
    if check_auth:
        hosted = await asyncio.gather(*[asyncio.to_thread(backend_view, m, name) for name in names])
    else:
        hosted = [backend_view(m, name, False) for name in names]
    return [local_view(m), *hosted]


@api_router.put("/backends/{name}")
async def update_backend(name: str, body: BackendUpdate, request: Request):
    """Settings → Backends: persist the default model (and effort, for hosted CLIs)."""
    from .backend_state import save_prefs
    m = mgr(request)
    if body.model is None and body.effort is None:
        raise HarnessError(400, "set model or effort")
    try:
        save_prefs(m, name, model=body.model, effort=body.effort)
    except KeyError:
        raise HarnessError(404, f"unknown backend {name!r}")
    except ValueError as e:
        raise HarnessError(400, str(e))
    if name == "local":
        return {"name": "local", "model": m.cfg.default_model, "effort": ""}
    from .backend_state import view as backend_view
    return await asyncio.to_thread(backend_view, m, name, False)


@api_router.get("/smart-approvals")
async def smart_approvals(request: Request):
    return require_owner(request).smart_approvals_status()


@api_router.put("/smart-approvals")
async def update_smart_approvals(body: SmartApprovalsUpdate, request: Request):
    return require_owner(request).set_smart_approvals_mode(body.mode)


@api_router.get("/models/status")
async def models_status(request: Request):
    m = mgr(request)
    return [{"name": mc.name, "state": await m.warmer.state(mc), "waking_seconds": m.warmer.waking_for(mc)}
            for mc in m.cfg.models.values()]


@api_router.post("/models/warm")
async def models_warm(request: Request):
    """Load the default model if it's asleep. The web app calls this when it opens."""
    m = mgr(request)
    if not m.cfg.modules.local_model:
        raise HarnessError(400, "the local model is disabled by this service profile")
    model = m.cfg.models[m.cfg.default_model]
    return {"name": model.name, "state": await m.warmer.warm(model)}


# image generation
def images_service(request: Request):
    m = mgr(request)
    if m.images is None:
        raise HarnessError(400, "image generation is disabled in config/harness.yaml")
    return m.images


def image_payload(job: dict, svc, request: Request, status: dict) -> dict:
    from . import image_edit
    parent = svc.db.get_image(job["parent_id"]) if job.get("parent_id") else None
    children = svc.db.image_children(job["id"])
    if request.state.access.role == "guest":
        children = [child for child in children if not image_edit.is_private(child)]
    eligibility = image_edit.edit_eligibility(
        job.get("width"), job.get("height"), max_pixels=svc.cfg.max_pixels)
    edit = status.get("edit") or {}
    ready = bool(svc.edit_enabled and edit.get("available"))
    editable = ready and eligibility["editable"]
    editable_reason = (eligibility["reason"] if ready
                       else (edit.get("setup") or svc.edit_status().get("setup", "")))
    return {**job, "service": status, "private": image_edit.is_private(job),
            "editable": editable, "editable_reason": editable_reason,
            "parent": ({"id": parent["id"], "width": parent["width"], "height": parent["height"]}
                       if parent else None),
            "children": [{"id": child["id"], "operation": child.get("operation") or "generate",
                          "scale": child.get("scale"), "status": child["status"],
                          "upscale_model": child.get("upscale_model") or "", "width": child["width"],
                          "height": child["height"]} for child in children]}


def visible_job(job, request):
    from . import image_edit
    if job is None:
        raise HarnessError(404, "no such image")
    if request.state.access.role == "guest" and image_edit.is_private(job):
        raise HarnessError(404, "no such image")
    return job


async def read_upload(file: UploadFile | None, limit: int) -> bytes:
    if file is None:
        raise HarnessError(400, "file is required")
    # Ignore the client filename entirely: uploads are stored as a generated id, never as a path.
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HarnessError(400, f"image is too large (max {limit} bytes)")
    return data


@api_router.get("/images")
async def list_images(request: Request, limit: int = 60):
    from . import image_edit
    svc = images_service(request)
    visible_operations = tuple(image_edit.PUBLIC_OPERATIONS) if request.state.access.role == "guest" else ()
    images = svc.db.list_images(limit=limit, operations=visible_operations)
    status = await asyncio.to_thread(svc.status)
    return {"status": status, "images": images}


@api_router.post("/images", status_code=201)
async def create_image(body: ImageRequest, request: Request):
    from .fileops import ToolError
    svc = images_service(request)
    try:
        return svc.submit(body.prompt, model=body.model, aspect_ratio=body.aspect_ratio,
                          resolution=body.resolution, seed=body.seed, upscale=body.upscale)
    except ToolError as e:
        raise HarnessError(400, str(e))


@api_router.post("/images/uploads", status_code=201)
async def upload_image(request: Request, file: UploadFile = File(...)):
    from .fileops import ToolError
    require_owner(request)
    svc = images_service(request)
    try:
        return svc.ingest_upload(await read_upload(file, svc.cfg.max_upload_bytes))
    except ToolError as e:
        raise HarnessError(400, str(e))


@api_router.post("/images/warmup")
async def warmup_images(request: Request):
    """Start ComfyUI without a checkpoint. Called when the owner opens the Images tab."""
    return await images_service(request).warmup()


@api_router.post("/images/cooldown")
async def cooldown_images(request: Request):
    """Drop an unused Images-tab warmup so the language model can come back."""
    return images_service(request).cooldown()


@api_router.post("/images/{iid}/edit", status_code=201)
async def edit_image(iid: str, request: Request, prompt: str = Form(...), mask: UploadFile = File(...),
                     feather: int = Form(0), seed: int | None = Form(None)):
    from .fileops import ToolError
    require_owner(request)
    svc = images_service(request)
    parent = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    try:
        return svc.submit_edit(parent["id"], prompt, await read_upload(mask, svc.cfg.max_upload_bytes),
                               feather=feather, seed=seed)
    except ToolError as e:
        raise HarnessError(400, str(e))


@api_router.post("/images/{iid}/upscale", status_code=201)
async def upscale_image(iid: str, body: ImageUpscaleRequest, request: Request):
    from .fileops import ToolError
    svc = images_service(request)
    parent = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    try:
        return svc.submit_upscale(parent["id"], body.upscale)
    except ToolError as e:
        raise HarnessError(400, str(e))


@api_router.post("/images/{iid}/cancel")
async def cancel_image(iid: str, request: Request):
    from .fileops import ToolError
    require_owner(request)
    svc = images_service(request)
    job = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    try:
        return await svc.cancel(job["id"])
    except ToolError as e:
        raise HarnessError(409, str(e))


@api_router.delete("/images/{iid}")
async def delete_image(iid: str, request: Request):
    from .fileops import ToolError
    require_owner(request)
    svc = images_service(request)
    job = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    backup = Path(mgr(request).cfg.backup.dir) if mgr(request).cfg.backup.dir else None
    try:
        return await svc.delete(job["id"], backup_dir=backup)
    except ToolError as e:
        raise HarnessError(404, str(e))


@api_router.get("/images/{iid}")
async def get_image(iid: str, request: Request):
    svc = images_service(request)
    variant = "json"
    raw = iid
    if raw.endswith(".source.png"):
        variant, raw = "source", raw[: -len(".source.png")]
    elif raw.endswith(".mask.png"):
        variant, raw = "mask", raw[: -len(".mask.png")]
    elif raw.endswith(".png"):
        variant, raw = "png", raw[: -len(".png")]
    job = visible_job(svc.db.get_image(raw), request)
    owner = request.state.access.role == "owner"
    if variant == "json":
        status = await asyncio.to_thread(svc.status)
        return image_payload(job, svc, request, status)
    from . import image_edit
    if variant in ("source", "mask") and not owner:
        raise HarnessError(404, IMAGE_NOT_READY)
    path = {"png": svc.path, "source": svc.source_path, "mask": svc.mask_path}[variant](job)
    if variant == "png" and job["status"] != "done":
        raise HarnessError(404, IMAGE_NOT_READY)
    if not path.exists():
        raise HarnessError(404, IMAGE_NOT_READY)
    headers = {"Cache-Control": "private, no-store"} if image_edit.is_private(job) or variant != "png" else {
        "Cache-Control": "max-age=86400"}
    return FileResponse(path, media_type="image/png", headers=headers)


# GPU contention guard
@api_router.get("/gpu")
async def gpu(request: Request):
    m = mgr(request)
    return m.guard.status() if m.guard else {"enabled": False, "state": "clear", "signals": []}


@api_router.post("/gpu/{action}")
async def gpu_action(action: str, request: Request, body: GpuHoldRequest | None = None):
    """pause: hold the GPU for other uses until resumed. resume: reload now, ignoring the current triggers."""
    m = mgr(request)
    if m.guard is None:
        raise HarnessError(400, "the GPU guard is disabled in config/harness.yaml")
    if action == "pause":
        duration = body.duration_seconds if body else None
        if duration is not None and not 1 <= duration <= 24 * 60 * 60:
            raise HarnessError(400, "duration_seconds must be between 1 and 86400")
        m.guard.pause(duration)
    elif action == "resume":
        # Turning off the manual hold must not suppress a live game/Plex trigger. A direct resume while only an
        # automatic trigger is active retains the legacy "resume anyway" operator action.
        m.guard.resume(override_signals=not m.guard.manual)
    else:
        raise HarnessError(404, "unknown action")
    return m.guard.status()


# Claude Code Remote Control servers (remote_control.py)
def remote_control(m):
    if m.remote_control is None:
        raise HarnessError(400, "Remote Control launches are disabled (remote_control.enabled in harness.yaml)")
    return m.remote_control


@api_router.get("/remote-control")
async def rc_status(request: Request):
    m = mgr(request)
    if m.remote_control is None:
        return {"enabled": False, "projects": []}
    if request.state.access.role == "guest":
        return {"enabled": True, "projects": []}
    return {"enabled": True, "projects": m.remote_control.status()}


@api_router.post("/remote-control/{project}")
async def rc_launch(project: str, request: Request):
    from .fileops import ToolError
    rc = remote_control(mgr(request))
    try:
        return await rc.launch(project, started_by=request.headers.get("tailscale-user-login") or "web app")
    except ToolError as e:
        raise HarnessError(400, str(e))


@api_router.post("/remote-control/{project}/trust")
async def rc_trust(project: str, request: Request):
    from .fileops import ToolError
    try:
        return remote_control(mgr(request)).open_trust_prompt(project)
    except ToolError as e:
        raise HarnessError(400, str(e))


@api_router.post("/remote-control/{project}/stop")
async def rc_stop(project: str, request: Request):
    from .fileops import ToolError
    try:
        return await remote_control(mgr(request)).stop(project)
    except ToolError as e:
        raise HarnessError(404, str(e))


@api_router.get("/queue")
async def queue(request: Request):
    m = mgr(request)
    scope = owner_id(request)
    positions = m.scheduler.positions()
    out = []
    for sid, pos in sorted(positions.items(), key=lambda x: x[1]):
        session = m.db.get_session(sid) or {}
        if session.get("owner_id", "owner") != scope or (session.get("kind") or "agent") != "agent":
            continue
        out.append({"session_id": sid, "position": pos})
    return out


@api_router.get("/sessions")
async def list_sessions(request: Request, limit: int = 50):
    m = mgr(request)
    return [m.list_summary(s) for s in m.db.list_sessions(limit, owner_id=owner_id(request))]


@api_router.get("/memory")
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


@api_router.put("/memory/profile")
async def update_memory_profile(body: MemoryProfileUpdate, request: Request):
    """Owner edit of the agent profile from Settings. Commits and pushes like an approved memory write."""
    from .fileops import ToolError
    m = mgr(request)
    lib, cfg = m.runner.memory, m.cfg.memory_library
    if lib is None:
        raise HarnessError(400, "the memory library is disabled in config/harness.yaml")
    if not cfg.profile_path:
        raise HarnessError(400, "memory_library.profile_path is not set")
    try:
        saved = await lib.owner_write(cfg.profile_path, body.content, body.summary)
    except ToolError as e:
        raise HarnessError(400, str(e))
    profile = await asyncio.to_thread(lib.profile_text)
    return {"profile": profile, "profile_chars": len(profile), "profile_max_chars": cfg.profile_max_chars,
            "last_commit": saved}


def skills_or_400(m: Manager):
    if m.skills is None:
        raise HarnessError(400, "instruction skills are disabled")
    return m.skills


def skill_op(fn):
    from .skills import SkillError
    try:
        return fn()
    except SkillError as e:
        raise HarnessError(e.status, str(e)) from e
    except sqlite3.IntegrityError as e:
        raise HarnessError(409, "skill store constraint failed") from e


def skills_owner(request: Request):
    require_owner(request)
    return skills_or_400(mgr(request))


@api_router.get("/skills")
async def skills_overview(request: Request):
    m = require_owner(request)
    if m.skills is None:
        return {"enabled": False, "proposals": [], "installed": []}
    return m.skills.list_overview()


@api_router.get("/skills/enabled")
async def skills_enabled(request: Request):
    m = require_owner(request)
    if m.skills is None:
        return []
    return m.skills.list_enabled()


@api_router.get("/skills/proposals/{pid}")
async def skill_proposal(pid: str, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.get_proposal(pid, include_body=True))


@api_router.post("/skills/proposals/{pid}/install")
async def skill_install(pid: str, body: SkillInstall, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.install(pid, body.content_hash))


@api_router.post("/skills/proposals/{pid}/reject")
async def skill_reject(pid: str, body: SkillReject, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.reject(pid, body.reason))


@api_router.post("/skills/proposals/{pid}/reopen")
async def skill_reopen(pid: str, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.reopen(pid))


@api_router.post("/skills/proposals/{pid}/review")
async def skill_hosted_review(pid: str, request: Request):
    store = skills_owner(request)
    if store.reviewer is None:
        raise HarnessError(400, "skill review is not available")
    return skill_op(lambda: store.reviewer.request_hosted(pid))


@api_router.delete("/skills/proposals/{pid}", status_code=204)
async def skill_delete_draft(pid: str, request: Request):
    store = skills_owner(request)
    skill_op(lambda: store.delete_draft(pid))


@api_router.post("/skills/{slug}/enable")
async def skill_enable(slug: str, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.set_enabled(slug, True))


@api_router.post("/skills/{slug}/disable")
async def skill_disable(slug: str, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.set_enabled(slug, False))


@api_router.post("/skills/{slug}/rollback")
async def skill_rollback(slug: str, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.rollback(slug))


@api_router.post("/skills/{slug}/uninstall")
async def skill_uninstall(slug: str, request: Request):
    store = skills_owner(request)
    skill_op(lambda: store.uninstall(slug))
    return {"ok": True}


@api_router.put("/skills/{slug}/projects")
async def skill_projects(slug: str, body: SkillAllowlist, request: Request):
    require_owner(request)
    m = mgr(request)
    return skill_op(lambda: skills_or_400(m).set_allowlist(slug, body.projects, list(m.cfg.projects)))


@api_router.get("/skills/{slug}/export")
async def skill_export(slug: str, request: Request):
    store = skills_owner(request)
    return skill_op(lambda: store.export_bundle(slug))


@api_router.get("/search")
async def search_sessions(request: Request, q: str = "", project: str = "", limit: int = 20):
    """Full-text search over past sessions. Passages mark matches with \\u0002 ... \\u0003."""
    from . import search
    m = mgr(request)
    if request.state.access.role == "guest":
        return {"query": q, "mode": "all", "results": []}
    if not m.cfg.search.enabled:
        raise HarnessError(400, "session search is disabled in config/harness.yaml")
    return await asyncio.to_thread(search.search, m.db, q, project, max(1, min(limit, 50)),
                                   "", owner_id(request))


@api_router.post("/sessions", status_code=201)
async def create_session(body: CreateSession, request: Request):
    m = mgr(request)
    s = m.create(body.prompt, project=body.project, target=body.target, backend=body.backend,
                 model=body.model, title=body.title, owner_id=owner_id(request), skills=body.skills)
    return m.summary(s)


@api_router.post("/compare", status_code=201)
async def create_compare(body: CreateCompare, request: Request):
    m = require_owner(request)
    return await m.create_compare(body.prompt, [c.model_dump() for c in body.choices], body.project, owner_id(request))


@api_router.get("/compare/{group}")
async def get_compare(group: str, request: Request):
    return require_owner(request).compare_view(group, owner_id(request))


@api_router.post("/compare/{group}/pick")
async def pick_compare(group: str, body: PickWinner, request: Request):
    m = require_owner(request)
    return await m.compare_pick(group, body.winner, body.action, body.discard_rest, owner_id(request))


@api_router.post("/compare/{group}/discard")
async def discard_compare(group: str, request: Request):
    return await require_owner(request).compare_discard(group, owner_id(request))


@api_router.get("/sessions/{ref}")
async def get_session(ref: str, request: Request):
    m, _, session = owned_session(request, ref)
    return m.summary(session)


@api_router.patch("/sessions/{ref}")
@api_router.put("/sessions/{ref}")
async def patch_session(ref: str, body: SessionUpdate, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(m.rename(sid, body.title))


@api_router.post("/sessions/{ref}/messages")
async def send_message(ref: str, body: SendMessage, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.send(sid, body.content))


@api_router.post("/sessions/{ref}/rerun", status_code=201)
async def rerun(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(m.rerun(sid))


@api_router.get("/sessions/{ref}/changes")
async def changes(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return await m.changes(sid)


@api_router.get("/sessions/{ref}/review-comments")
async def review_comments(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.review_comments(sid)


@api_router.post("/sessions/{ref}/review-comments", status_code=201)
async def add_review_comment(ref: str, body: ReviewComment, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.add_review_comment(sid, body.model_dump())


@api_router.delete("/sessions/{ref}/review-comments/{comment_id}", status_code=204)
async def delete_review_comment(ref: str, comment_id: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    m.delete_review_comment(sid, comment_id)


@api_router.post("/sessions/{ref}/review-comments/send")
async def send_review_comments(ref: str, request: Request):
    """Send the drafted line comments to the agent as one follow-up message."""
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.send_review_comments(sid))


@api_router.post("/sessions/{ref}/review/{action}")
async def review(ref: str, action: str, request: Request):
    """merge | push | discard the session's git branch."""
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.review(sid, action))


@api_router.get("/maintenance")
async def maintenance(request: Request):
    return await require_owner(request).maintenance.usage()


@api_router.post("/maintenance/cleanup")
async def maintenance_cleanup(request: Request):
    return await require_owner(request).maintenance.cleanup()


@api_router.post("/maintenance/backup")
async def maintenance_backup(request: Request):
    return await require_owner(request).maintenance.backup()


@api_router.post("/maintenance/image-archive/retention/preview")
async def image_archive_retention_preview(request: Request):
    return await asyncio.to_thread(require_owner(request).image_archive.retention_preview)


@api_router.post("/maintenance/image-archive/retention/apply")
async def image_archive_retention_apply(body: ImageArchiveRetentionApply, request: Request):
    from .image_archive import ImageArchiveError
    try:
        return await asyncio.to_thread(require_owner(request).image_archive.apply_retention, body.confirmation)
    except ImageArchiveError as e:
        raise HarnessError(409, str(e))


@api_router.get("/sessions/{ref}/approvals")
async def approvals(ref: str, request: Request, all: bool = False):
    m, sid, _ = owned_session(request, ref)
    rows = m.db.approvals(sid) if all else m.db.pending_approvals(sid)
    return [public_approval(a) for a in rows]


@api_router.post("/sessions/{ref}/approvals/{approval_id}")
async def decide(ref: str, approval_id: str, body: Decision, request: Request):
    if body.decision not in ("approve", "deny"):
        raise HarnessError(400, "decision must be approve or deny")
    m, sid, _ = owned_session(request, ref)
    return m.decide(sid, None if approval_id == "pending" else approval_id,
                    body.decision == "approve", body.note)


@api_router.post("/a/{token}/{decision}")
async def decide_by_token(token: str, decision: str, request: Request):
    """Target of the notification's Approve / Deny buttons. The token is the credential."""
    if decision not in ("approve", "deny"):
        raise HarnessError(404, "not found")
    a = mgr(request).decide_by_token(token, decision == "approve")
    return {"id": a["id"], "status": a["status"]}


def owned_chat(request: Request, ref: str) -> tuple[Manager, str, dict]:
    return owned_session(request, ref, kind="chat")


@api_router.get("/chats/options")
async def chat_options(request: Request):
    m = require_owner(request)
    return await asyncio.to_thread(m.chat_options)


def owner_chat(request: Request, ref: str) -> tuple[Manager, str, dict]:
    """A chat the caller owns, for owner-only actions (snippets); household members and guests never pass."""
    require_owner(request)
    if not request.state.access.is_owner:
        raise HarnessError(403, "only the owner can run snippets")
    return owned_chat(request, ref)


@api_router.get("/chats/snippet-languages")
async def snippet_languages(request: Request):
    require_owner(request)
    from .snippets import LIMITS, SnippetService
    return {"languages": SnippetService.languages(), "limits": LIMITS}


@api_router.get("/chats")
async def list_chats(request: Request, limit: int = 50):
    m = require_owner(request)
    rows = m.db.list_sessions(max(1, min(limit, 200)), owner_id=owner_id(request), kind="chat")
    return [m.summary(r) for r in rows]


@api_router.post("/chats", status_code=201)
async def create_chat(body: CreateChat, request: Request):
    m = require_owner(request)
    s = m.create(body.prompt, backend=body.backend or None, model=body.model or None,
                 effort=body.effort or None, owner_id=owner_id(request), kind="chat")
    return m.summary(s)


@api_router.get("/chats/{ref}")
async def get_chat(ref: str, request: Request):
    m, _, session = owned_chat(request, ref)
    return m.summary(session)


@api_router.patch("/chats/{ref}")
@api_router.put("/chats/{ref}")
async def rename_chat(ref: str, body: SessionUpdate, request: Request):
    m, sid, _ = owned_chat(request, ref)
    return m.summary(m.rename(sid, body.title))


@api_router.delete("/chats/{ref}")
async def delete_chat(ref: str, request: Request):
    m, sid, _ = owned_chat(request, ref)
    m.delete_chat(sid)
    return {"deleted": sid}


@api_router.post("/chats/{ref}/messages")
async def send_chat_message(ref: str, body: SendMessage, request: Request):
    m, sid, _ = owned_chat(request, ref)
    return m.summary(await m.send(sid, body.content))


@api_router.post("/chats/{ref}/snippets", status_code=202)
async def run_snippet(ref: str, body: RunSnippet, request: Request):
    """Run one snippet the owner chose, in a fresh sandbox. The result arrives as a snippet_result event."""
    m, sid, _ = owner_chat(request, ref)
    return m.snippets.start(sid, body.language, body.source, body.origin)


@api_router.post("/chats/{ref}/snippets/{run_id}/cancel")
async def cancel_snippet(ref: str, run_id: str, request: Request):
    m, sid, _ = owner_chat(request, ref)
    return m.snippets.cancel(sid, run_id)


@api_router.post("/chats/{ref}/cancel")
async def cancel_chat(ref: str, request: Request):
    m, sid, _ = owned_chat(request, ref)
    return m.summary(await m.cancel(sid))


@api_router.post("/sessions/{ref}/cancel")
async def cancel(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.cancel(sid))


@api_router.get("/sessions/{ref}/transcript", response_class=PlainTextResponse)
async def get_transcript(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return transcript.render(m.db, sid)


# scheduled jobs (jobs.py)
def jobs_on(request: Request) -> Manager:
    m = mgr(request)
    if m.jobs is None:
        raise HarnessError(400, "scheduled jobs are disabled in config/harness.yaml")
    return m


def job_view(m: Manager, job: dict, runs: int = 1) -> dict:
    recent = m.db.job_sessions(job["id"], limit=runs)
    return {**job, "enabled": bool(job["enabled"]), "recent": recent}


@api_router.get("/jobs")
async def list_jobs(request: Request):
    m = jobs_on(request)
    if request.state.access.role == "guest":
        return []
    return [job_view(m, j) for j in m.db.list_jobs()]


@api_router.get("/jobs/preview")
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


@api_router.post("/jobs", status_code=201)
async def create_job(body: Job, request: Request):
    import time as _time
    from .jobs import Cron, new_job_id, validate
    m = jobs_on(request)
    try:
        job = validate(body.model_dump(), m.cfg.projects, m.cfg.models, m.cfg.backends)
    except ValueError as e:
        raise HarnessError(400, str(e))
    job["id"] = new_job_id()
    job["next_run_at"] = Cron(job["cron"]).next_after(_time.time())
    m.db.insert_job(job)
    return job_view(m, m.db.get_job(job["id"]))


@api_router.get("/jobs/{jid}")
async def get_job(jid: str, request: Request):
    m = jobs_on(request)
    if request.state.access.role == "guest":
        raise HarnessError(404, NO_SUCH_JOB)
    job = m.db.get_job(jid)
    if job is None:
        raise HarnessError(404, NO_SUCH_JOB)
    return job_view(m, job, runs=15)


@api_router.put("/jobs/{jid}")
async def update_job(jid: str, body: Job, request: Request):
    import time as _time
    from .jobs import Cron, validate
    m = jobs_on(request)
    old = m.db.get_job(jid)
    if old is None:
        raise HarnessError(404, NO_SUCH_JOB)
    try:
        job = validate(body.model_dump(), m.cfg.projects, m.cfg.models, m.cfg.backends)
    except ValueError as e:
        raise HarnessError(400, str(e))
    job["next_run_at"] = Cron(job["cron"]).next_after(_time.time())
    m.db.update_job(jid, **job)
    return job_view(m, m.db.get_job(jid), runs=15)


@api_router.delete("/jobs/{jid}", status_code=204)
async def delete_job(jid: str, request: Request):
    if not jobs_on(request).db.delete_job(jid):
        raise HarnessError(404, NO_SUCH_JOB)


@api_router.post("/jobs/{jid}/run", status_code=201)
async def run_job(jid: str, request: Request):
    """Run a job now, outside its schedule (the next scheduled run is unchanged)."""
    m = jobs_on(request)
    job = m.db.get_job(jid)
    if job is None:
        raise HarnessError(404, NO_SUCH_JOB)
    if job["last_session_id"] and m._is_active(job["last_session_id"]):
        raise HarnessError(409, "the previous run is still going")
    sid = m.jobs.run(job, manual=True)
    return m.summary(m.db.get_session(sid))


# templates
@api_router.get("/templates")
async def list_templates(request: Request):
    if request.state.access.role == "guest":
        return []
    return mgr(request).db.list_templates()


@api_router.post("/templates", status_code=201)
async def create_template(body: Template, request: Request):
    return _save_template(mgr(request), "t-" + uuid.uuid4().hex[:8], body)


@api_router.put("/templates/{tid}")
async def update_template(tid: str, body: Template, request: Request):
    m = mgr(request)
    if m.db.get_template(tid) is None:
        raise HarnessError(404, "no such template")
    return _save_template(m, tid, body)


@api_router.delete("/templates/{tid}", status_code=204)
async def delete_template(tid: str, request: Request):
    if not mgr(request).db.delete_template(tid):
        raise HarnessError(404, "no such template")


def _save_template(m: Manager, tid: str, body: Template) -> dict:
    if not body.name.strip() or not body.prompt.strip():
        raise HarnessError(400, "name and prompt are required")
    if body.project not in m.cfg.projects:
        raise HarnessError(400, f"unknown project {body.project!r}")
    if body.backend != "local" and (body.backend not in m.cfg.backends or not m.cfg.backends[body.backend].enabled):
        raise HarnessError(400, f"unknown or disabled backend {body.backend!r}")
    if body.backend == "local" and body.model and body.model not in m.cfg.models:
        raise HarnessError(400, f"unknown model {body.model!r}")
    m.db.upsert_template({"id": tid, **body.model_dump()})
    return m.db.get_template(tid)


# notifications
@api_router.post("/notify/test")
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
KEEPALIVE = ": keepalive\n\n"


def _event_stream_response(stream) -> StreamingResponse:
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _next_bus_event(sub, request: Request) -> tuple[dict | None, bool]:
    """Wait up to 15s for the next bus event. Returns (event, client_gone); event is None on a quiet interval."""
    try:
        return await asyncio.wait_for(sub.queue.get(), timeout=15), False
    except asyncio.TimeoutError:
        return None, await request.is_disconnected()


def _session_list_payload(m: Manager, scope: str, e: dict) -> str | None:
    """SSE frame for a status-level event of one of this scope's agent sessions, else None."""
    session = m.db.get_session(e["session_id"])
    if not (e["type"] in GLOBAL_TYPES and session and session.get("owner_id", "owner") == scope
            and (session.get("kind") or "agent") == "agent"):
        return None
    if e["type"] == "run_finished":
        e = {**e, "data": {k: v for k, v in e["data"].items() if k != "run"}}
    # Live-only list stream: drop the global seq so gaps cannot reveal other accounts.
    return sse({**e, "seq": None})


@api_router.get("/events")
async def all_events(request: Request):
    """Status-level events for every session (the session list). Live only; reload the list to catch up."""
    m = mgr(request)
    scope = owner_id(request)

    async def stream():
        sub = m.bus.subscribe("*")
        epoch = m.stream_epoch.get(scope, 0)
        try:
            yield ": connected\n\n"
            while m.stream_epoch.get(scope, 0) == epoch:
                e, gone = await _next_bus_event(sub, request)
                if gone:
                    return
                if e is None:
                    yield KEEPALIVE
                    continue
                frame = _session_list_payload(m, scope, e)
                if frame is not None:
                    yield frame
        finally:
            m.bus.unsubscribe("*", sub)

    return _event_stream_response(stream())


def _resync_after_overflow(m: Manager, sub, sid: str, last: int) -> list[dict]:
    """Drop the overflowed live queue and re-read persisted events after `last`."""
    sub.overflowed = False
    while not sub.queue.empty():
        sub.queue.get_nowait()
    return m.db.events(sid, last)


def _advance_seq(last: int, e: dict) -> int | None:
    """New high-water seq after `e`, or None when a persisted event was already delivered."""
    seq = e["seq"]
    if seq is None:
        return last
    return seq if seq > last else None


async def _conversation_events(request: Request, m: Manager, sid: str, after: int, follow: bool):
    sub = m.bus.subscribe(sid)
    last = after
    epoch = m.stream_epoch.get(owner_id(request), 0)
    try:
        yield ": connected\n\n"
        for e in m.db.events(sid, after):
            last = e["seq"]
            yield sse(e)
        while follow and m.stream_epoch.get(owner_id(request), 0) == epoch:
            if sub.overflowed:
                for e in _resync_after_overflow(m, sub, sid, last):
                    last = e["seq"]
                    yield sse(e)
            e, gone = await _next_bus_event(sub, request)
            if gone:
                return
            if e is None:
                yield KEEPALIVE
                continue
            advanced = _advance_seq(last, e)
            if advanced is None:
                continue
            last = advanced
            yield sse(e)
    finally:
        m.bus.unsubscribe(sid, sub)


def conversation_event_stream(request: Request, sid: str, after: int, follow: bool):
    if request.headers.get("last-event-id", "").isdigit():  # EventSource reconnects resume by itself
        after = max(after, int(request.headers["last-event-id"]))
    return _event_stream_response(_conversation_events(request, mgr(request), sid, after, follow))


@api_router.get("/sessions/{ref}/events")
async def events(ref: str, request: Request, after: int = 0, follow: bool = True):
    """Server-sent events: replays persisted events after `after`, then streams live ones.
    Ephemeral events (token deltas, queue moves) have `seq: null` and are never replayed."""
    _, sid, _ = owned_session(request, ref)
    return conversation_event_stream(request, sid, after, follow)


@api_router.get("/chats/{ref}/events")
async def chat_events(ref: str, request: Request, after: int = 0, follow: bool = True):
    _, sid, _ = owned_chat(request, ref)
    return conversation_event_stream(request, sid, after, follow)


def create_app(manager: Manager | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = manager or Manager(config_mod.load())
        await app.state.manager.start()
        yield
        await app.state.manager.stop()

    app = FastAPI(title="agent-harness", lifespan=lifespan)
    app.middleware("http")(guard)
    app.add_exception_handler(HarnessError, harness_error)
    web_router.install(app)
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    from . import apps, endpoint
    endpoint.register(app, mgr)
    apps.register(app)
    api_router.install(app)
    from . import admin
    admin.register(app, mgr)
    # Keep /static for installed bundled clients, while making harness/web directly deployable at a static-site root.
    # This catch-all mount is last so daemon/API routes always win.
    app.mount("/", StaticFiles(directory=WEB), name="web-root")
    return app
