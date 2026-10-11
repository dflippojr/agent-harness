"""HTTP API and web app. Bound to localhost; `tailscale serve` publishes it on the tailnet over HTTPS."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import MutableHeaders

from . import operation_audit, namespace_audit, access as access_mod
from . import catalog_ids
from . import compat
from . import credential_audit
from . import config as config_mod
from . import efficiency
from . import google_signin
from . import idempotency
from . import local_owner
from . import tailscale_peer
from . import taint
from . import telemetry
from . import transcript
from .manager import HarnessError, Manager, public_approval
from .api_models import WebSessionResponse
from .modules import principal_capabilities
from .webgzip import WebGzipMiddleware


# Static files get their type from `mimetypes`, which on Windows also reads the registry, where .js/.mjs can
# be missing or mapped to text/plain. Browsers refuse a module script without a JavaScript type, so pin both.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

log = logging.getLogger("harness.api")
WEB = Path(__file__).parent / "web"
# Session-list stream: status-level events only, no tool output or token deltas.
GLOBAL_TYPES = {"session_created", "status", "approval_requested", "approval_decided", "run_finished", "queue"}


class CreateSession(BaseModel):
    prompt: str
    project: str = "scratch"
    target: str | None = None  # default: the project's target
    backend: str = "local"
    model: str | None = None
    title: str | None = None
    skills: list[str] | None = None


class CreateGitHubSession(CreateSession):
    number: int


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
    github: bool = False  # issue #63: members only; clone with the member's own GitHub connection


class SendMessage(BaseModel):
    content: str


class ForkRequest(BaseModel):
    prompt: str


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


class SecretDismissal(BaseModel):
    reason: str


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


class ProfileUpdate(BaseModel):
    emoji: str


class BackendUpdate(BaseModel):
    model: str | None = None
    effort: str | None = None


class SmartApprovalsUpdate(BaseModel):
    mode: str


def sse(event: dict) -> str:
    head = f"id: {event['seq']}\n" if event.get("seq") is not None else ""
    return f"{head}event: {event['type']}\ndata: {json.dumps(event)}\n\n"


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
    sid = m.resolve_id(ref, user_id=scope, kind=kind, app="")  # never an App's session (#330 decision 3)
    session = m.db.get_session(sid)
    if (session is None or session.get("owner_id", "owner") != scope
            or (session.get("kind") or "agent") != kind or session.get("app_id")):
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
    member_block = access_mod.member_forbidden(ident, request.method, request.url.path, m.cfg)
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
                                          "x-agent-harness-client", idempotency.HEADER.lower()}):
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403,
                            headers=info.cors_headers)
    return Response(status_code=204, headers={**info.cors_headers,
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, Last-Event-ID, X-Agent-Harness-Client, "
                                                    + idempotency.HEADER,
                    "Access-Control-Max-Age": "600"})


def _cross_site_refused(request: Request, m: Manager, info: _OriginInfo) -> bool:
    """Browsers send Origin on POSTs: refuse cross-site requests (a web page can't drive the agent)."""
    from .apps import daemon_origins
    if request.method in ("GET", "HEAD", "OPTIONS") or info.cross_origin_api:
        return False
    return bool((info.raw and info.origin not in daemon_origins(m.cfg))
                or request.headers.get("sec-fetch-site") == "cross-site")


async def _identity_from_tailscaled(request: Request, m: Manager) -> None:
    """Drop the Tailscale identity headers unless tailscaled sent them (harness/tailscale_peer.py). This runs before
    anything reads a header, so a forged login can't select a member, a guest, or open owner mode."""
    scope = request.scope
    if not tailscale_peer.has_identity(scope):
        return
    check = m.tailscale_peer
    if check.platform_ok():
        client, server = scope.get("client"), scope.get("server")
        if client and server and check.cached((str(client[0]), int(client[1]))) is None:
            trusted = await asyncio.to_thread(check.verify, client, server)
        else:
            trusted = check.verify(client, server)
    else:
        trusted = m.cfg.trust_unverified_identity_headers
    if not trusted:
        tailscale_peer.strip_identity(scope)
        request.__dict__.pop("_headers", None)


async def guard(request: Request, call_next):
    m: Manager = request.app.state.manager
    if not _host_allowed(request, m.cfg):
        return JSONResponse({"detail": "request Host is not a daemon address"}, status_code=421)
    return await call_next(request)


async def _access_guard(request: Request, call_next):
    m: Manager = request.app.state.manager
    await _identity_from_tailscaled(request, m)
    public_path = request.scope.get("harness_original_path", request.url.path)
    info = _origin_info(request, m, public_path)
    # `tailscale serve` adds the caller's identity. A request without it reached the loopback listener directly, so
    # it must carry the local owner token or a credential its route checks (harness/local_owner.py).
    login = request.headers.get("tailscale-user-login") or None
    if login is None and not local_owner.admitted(request, m, public_path):
        return JSONResponse({"detail": local_owner.REFUSED}, status_code=401, headers=info.cors_headers)
    # Issue #64: a linked Google session can select a member only on an admitted Serve login (precedence table in
    # docs/google-signin.md). With Google sign-in off this is exactly `resolve_access`.
    ident = m.google_signin.resolve(request, login)
    web = request.state.web_auth
    request.state.access = ident
    if ident.kind in ("owner", "member") and ident.allowed and ident.user_id != "owner":
        m.db.touch_account(ident.user_id)
    refusal = _web_session_refusal(request, m, ident, web, public_path, info.cors_headers)
    if refusal is None and not (web.admitted and not ident.allowed):
        refusal = _access_refusal(request, m, ident, login, info.cors_headers)
    if refusal is not None:
        if web.clear_cookie:
            google_signin.clear_session_cookie(refusal)
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
    warnings = []
    warning_token = namespace_audit.gap.set(warnings)
    try:
        response = await call_next(request)
    finally:
        namespace_audit.gap.reset(warning_token)
    authenticated_key_id = getattr(request.state, "authenticated_key_id", None)
    if authenticated_key_id is not None and not getattr(request.state, "key_activity_in_endpoint_log", False):
        m.record_key_activity(authenticated_key_id)
    if warnings:
        response.headers["X-Agent-Harness-Audit-Warning"] = "audit_gap"
        response.headers["Access-Control-Expose-Headers"] = "X-Agent-Harness-Audit-Warning"
    if info.cross_origin_api and idempotency.REPLAYED_HEADER in response.headers:
        exposed = response.headers.get("Access-Control-Expose-Headers", "")
        response.headers["Access-Control-Expose-Headers"] = ", ".join(
            header for header in (exposed, idempotency.REPLAYED_HEADER) if header)
    if compatibility and compatibility["state"] == "transition":
        response.headers["X-Agent-Harness-Deprecation"] = "missing_client_version"
        response.headers["Warning"] = '299 agent-harness "client version header will be required after this transition release"'
    for key, value in info.cors_headers.items():
        response.headers[key] = value
    if web.clear_cookie:
        google_signin.clear_session_cookie(response)
    return response


def _host_allowed(request: Request, cfg: config_mod.Config) -> bool:
    """Match one exact authority, never a forwarded host or a caller-supplied URL."""
    from .apps import normalize_origin

    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        return False
    allowed = {f"{host}:{cfg.port}" for host in ("127.0.0.1", "localhost", "[::1]")}
    if cfg.port == 80:
        allowed.update({"127.0.0.1", "localhost", "[::1]"})
    try:
        public = urlsplit(normalize_origin(cfg.public_url))
    except ValueError:
        pass  # An absent/invalid public URL never widens the loopback allowlist.
    else:
        allowed.add(public.netloc)
        if public.port is None:
            allowed.add(f"{public.netloc}:{443 if public.scheme == 'https' else 80}")
    return hosts[0].lower() in allowed


def _web_session_refusal(request: Request, m: Manager, ident, web, public_path: str,
                         cors_headers: dict) -> JSONResponse | None:
    """Pre-sign-in admitted devices reach only the Web shell and sign-in routes; cookie mutations need CSRF."""
    if web.admitted and not ident.allowed:
        if m.google_signin.pre_auth_allowed(request.method, public_path):
            return None
        return JSONResponse({"detail": google_signin.SIGN_IN_REQUIRED, "error": {
            "code": "sign_in_required", "message": google_signin.SIGN_IN_REQUIRED, "retryable": False,
        }}, status_code=401, headers=cors_headers)
    if web.via_session and request.method not in access_mod.SAFE_METHODS:
        problem = m.google_signin.csrf_problem(request, web)
        if problem:
            log.warning("refused %s %s for a Google Web session (%s)", request.method, request.url.path, problem)
            return JSONResponse({"detail": problem}, status_code=403, headers=cors_headers)
    return None


async def harness_error(request: Request, exc: HarnessError):
    body = {"detail": str(exc), "error": {
        "code": exc.code, "message": str(exc), "retryable": exc.status == 429 or exc.status >= 500,
    }}
    if getattr(exc, "operation_id", None):
        body["error"].update(operation_id=exc.operation_id, may_have_completed=True, retryable=False)
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
            **principal_capabilities(cfg, ident.role == "owner"),
            "accounts": ident.role == "owner",
        },
        "usage": usage,
        "notify": {"enabled": False, "topic": ""} if guest or member else {
            "enabled": cfg.module_effective("notifications"), "topic": cfg.notify.topic,
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
        if body.github:
            _require_same_origin(request, m)
        return m.create_member_project(ident.user_id, body.name, body.description, body.repo,
                                       github=body.github)
    if body.github:
        raise HarnessError(400, "GitHub sign-in is for household member projects; owner projects keep their "
                                "existing credential path")
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


def _require_same_origin(request: Request, m: Manager) -> None:
    """Refuse cross-site browser requests for member credential actions."""
    from .admin import _check_browser_origin
    _check_browser_origin(request, m)


def _log_safe(value: object) -> str:
    """A client-supplied value as one log line: newlines and other control characters are escaped."""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in str(value))


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


@api_router.get("/queue")
async def queue(request: Request):
    m = mgr(request)
    scope = owner_id(request)
    positions = m.scheduler.positions()
    out = []
    for sid, pos in sorted(positions.items(), key=lambda x: x[1]):
        if m.db.app_of(sid):  # an App's session (#330 decision 3)
            continue
        session = m.db.get_session(sid) or {}
        if session.get("owner_id", "owner") != scope or (session.get("kind") or "agent") != "agent":
            continue
        out.append({"session_id": sid, "position": pos})
    return out


@api_router.get("/sessions", response_model=list[WebSessionResponse], response_model_exclude_unset=True)
async def list_sessions(request: Request, limit: int = 50):
    m = mgr(request)
    return [m.list_summary(s) for s in m.db.list_sessions(limit, owner_id=owner_id(request))]


@api_router.post("/sessions", status_code=201, response_model=WebSessionResponse, response_model_exclude_unset=True)
async def create_session(body: CreateSession, request: Request):
    m = mgr(request)
    s = m.create(body.prompt, project=body.project, target=body.target, backend=body.backend,
                 model=body.model, title=body.title, owner_id=owner_id(request), skills=body.skills,
                 context=operation_audit.request_context(request, m))
    return m.summary(s)


def _github_project(request: Request, project: str):
    from . import catalog, github_tasks
    m = require_owner(request)
    spec = catalog.get_project(m.cfg, m.db, owner_id(request), project)
    repo = github_tasks.repository(spec.repo) if spec else None
    if not repo:
        raise HarnessError(400, "This project has no supported GitHub repository URL")
    return m, spec, repo


@api_router.get("/github/projects/{project}/items")
async def github_items(request: Request, project: str, page: int = 1, q: str = ""):
    from . import github_tasks
    m, _, repo = _github_project(request, project)
    return await asyncio.to_thread(github_tasks.list_items, m.cfg, repo, page, q)


@api_router.get("/github/projects/{project}/items/{number}")
async def github_item(request: Request, project: str, number: int):
    from . import github_tasks
    m, _, repo = _github_project(request, project)
    return await asyncio.to_thread(github_tasks.item, m.cfg, repo, number)


@api_router.post("/github/sessions", status_code=201)
async def create_github_session(body: CreateGitHubSession, request: Request):
    from . import github_tasks
    m, spec, repo = _github_project(request, body.project)
    source = await asyncio.to_thread(github_tasks.item, m.cfg, repo, body.number)
    if source["kind"] == "pr" and (spec.target != "tower" or body.target not in (None, "tower")):
        # Remote runners use their own configured repository and cannot prove it matches this URL.
        raise HarnessError(400, "PR branch tasks require a tower project")
    prompt = github_tasks.prompt(source)
    s = m.create(prompt, project=body.project, target=body.target, backend=body.backend,
                 model=body.model, title=source["title"], owner_id=owner_id(request),
                 skills=body.skills, app_metadata={"github_base_branch": source["base_branch"]},
                 taint=taint.add([], "github", f"GitHub {repo} #{body.number}"))
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
    return await m.compare_pick(group, body.winner, body.action, body.discard_rest, owner_id(request),
                                context=operation_audit.request_context(request, m))


@api_router.post("/compare/{group}/discard")
async def discard_compare(group: str, request: Request):
    m = require_owner(request)
    return await m.compare_discard(group, owner_id(request), context=operation_audit.request_context(request, m))


@api_router.get("/sessions/{ref}", response_model=WebSessionResponse, response_model_exclude_unset=True)
async def get_session(ref: str, request: Request):
    m, _, session = owned_session(request, ref)
    return m.summary(session)


@api_router.patch("/sessions/{ref}")
@api_router.put("/sessions/{ref}")
async def patch_session(ref: str, body: SessionUpdate, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(m.rename(sid, body.title, context=operation_audit.request_context(request, m)))


@api_router.post("/sessions/{ref}/messages")
async def send_message(ref: str, body: SendMessage, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.send(sid, body.content, context=operation_audit.request_context(request, m)))


@api_router.post("/sessions/{ref}/rerun", status_code=201)
async def rerun(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(m.rerun(sid, context=operation_audit.request_context(request, m)))


@api_router.get("/sessions/{ref}/checkpoints")
async def session_checkpoints(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.checkpoints(sid)


@api_router.post("/sessions/{ref}/checkpoints/{turn}/rewind")
async def rewind_checkpoint(ref: str, turn: int, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.rewind(sid, turn, context=operation_audit.request_context(request, m)))


@api_router.post("/sessions/{ref}/checkpoints/{turn}/fork", status_code=201)
async def fork_checkpoint(ref: str, turn: int, body: ForkRequest, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.fork(sid, turn, body.prompt, context=operation_audit.request_context(request, m)))


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


@api_router.post("/sessions/{ref}/secret-findings/fix", status_code=201)
async def secret_findings_fix(ref: str, request: Request):
    """Ask agent to fix: one draft review comment per open secret-scan finding (send them like any draft)."""
    m, sid, _ = owned_session(request, ref)
    return await m.secret_findings_fix(sid)


@api_router.post("/sessions/{ref}/secret-findings/{fingerprint}/dismiss")
async def dismiss_secret_finding(ref: str, fingerprint: str, body: SecretDismissal, request: Request):
    """Owner-only: dismiss one secret-scan finding with a reason (audited)."""
    require_owner(request)
    m, sid, _ = owned_session(request, ref)
    return await m.dismiss_secret_finding(sid, fingerprint, body.reason, owner_id(request))


@api_router.post("/sessions/{ref}/review/{action}")
async def review(ref: str, action: str, request: Request):
    """merge | push | discard the session's git branch."""
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.review(sid, action, context=operation_audit.request_context(request, m)))


@api_router.get("/maintenance")
async def maintenance(request: Request):
    return await require_owner(request).maintenance.usage()


@api_router.post("/maintenance/cleanup")
async def maintenance_cleanup(request: Request):
    m = require_owner(request)
    return await m.maintenance.cleanup(context=operation_audit.request_context(request, m))


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
                    body.decision == "approve", body.note, context=operation_audit.request_context(request, m))


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


@api_router.post("/sessions/{ref}/taint/clear")
async def clear_taint(ref: str, request: Request):
    m = require_owner(request)
    return m.summary(m.clear_taint(ref, context=operation_audit.request_context(request, m)))


@api_router.post("/sessions/{ref}/cancel")
async def cancel(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return m.summary(await m.cancel(sid, context=operation_audit.request_context(request, m)))


@api_router.get("/sessions/{ref}/transcript", response_class=PlainTextResponse)
async def get_transcript(ref: str, request: Request):
    m, sid, _ = owned_session(request, ref)
    return transcript.render(m.db, sid)


@api_router.get("/sessions/{ref}/metrics")
async def session_metrics(ref: str, request: Request):
    """Owner-only per-turn context-efficiency metrics for one agent session (#159)."""
    m = require_owner(request)
    sid = m.resolve_id(ref, kind="agent", app="")
    session = m.db.get_session(sid)
    if session is None or session.get("app_id"):
        raise HarnessError(404, "no session matches that id")
    return efficiency.session_payload(sid, m.db.events(sid))


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
    """SSE frame for a status-level event of one of this scope's agent sessions, else None. An App's session is never
    one (#330 decision 3): its store is not even read."""
    if m.db.app_of(e["session_id"]):
        return None
    session = m.db.get_session(e["session_id"])
    if not (e["type"] in GLOBAL_TYPES and session and session.get("owner_id", "owner") == scope
            and (session.get("kind") or "agent") == "agent" and not session.get("app_id")):
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


def _key_origins(body: dict, kind: str) -> list:
    if not body.get("origins"):
        return []
    if kind != "owner":
        raise HarnessError(400, "browser origins on manually minted keys are owner-only; pair app tokens")
    raw_origins = body["origins"]
    if not isinstance(raw_origins, list) or not all(isinstance(value, str) for value in raw_origins):
        raise HarnessError(400, "origins must be a list of browser origins")
    from .apps import normalize_origin
    try:
        return list(dict.fromkeys(normalize_origin(value) for value in raw_origins))
    except ValueError as e:
        raise HarnessError(400, str(e))


def _key_catalog_app_id(body: dict) -> str:
    try:
        return catalog_ids.normalize(body.get("catalog_app_id"))
    except ValueError as e:
        raise HarnessError(400, str(e))


@api_router.get("/keys")
async def list_keys(request: Request):
    return mgr(request).db.list_api_keys()


@api_router.post("/keys", status_code=201)
async def create_key(request: Request):
    body = await request.json()
    from .admin import parse_key_spec
    m = mgr(request)
    ctx = credential_audit.request_context(request, m)
    try:
        name, scopes, kind = parse_key_spec(body)
        origins = _key_origins(body, kind)
        catalog_app_id = _key_catalog_app_id(body)
    except HarnessError:
        await m.db.main.awrite(credential_audit.record, m.db, ctx, "key.create", "", "denied", "api_key",
                               {"reason": "invalid_request"})
        raise
    if kind == "owner":
        from .hub_claim import refuse_owner_key
        await refuse_owner_key(m, ctx, "key.create", "api_key")

    def commit():
        row, key = m.db.main.create_api_key(name, scopes, kind, origins, catalog_app_id)
        credential_audit.record(m.db, ctx, "key.create", row["id"], "ok", "api_key",
                                {"key_id": row["id"], "kind": kind, "scopes": scopes.split(),
                                 "catalog_app_id": catalog_app_id})
        return row, key
    row, key = await m.db.main.awrite(commit)
    return {**row, "key": key}


@api_router.delete("/keys/{kid}", status_code=204)
async def revoke_key(kid: str, request: Request):
    m = mgr(request)
    ctx = credential_audit.request_context(request, m)

    def commit() -> bool | None:
        before = m.db.main.get_api_key(kid)
        if before is not None and before.get("role") == "hub" and before.get("revoked_at") is None:
            credential_audit.record(m.db, ctx, "key.revoke", kid, "denied", "api_key",
                                    {"key_id": kid, "kind": before["kind"], "reason": "hub_key"})
            return None
        if m.db.main.revoke_api_key(kid):
            credential_audit.record(m.db, ctx, "key.revoke", kid, "ok", "api_key",
                                    {"key_id": kid, "kind": before["kind"] if before else None})
            return True
        # Unknown (or never-revocable) id: nothing the caller supplied is stored. A known, already-revoked key is.
        known = before is not None and before.get("kind") != "web"
        credential_audit.record(m.db, ctx, "key.revoke", kid if known else "", "noop", "api_key",
                                {"reason": "already_revoked" if known else "not_found",
                                 **({"key_id": kid, "kind": before["kind"]} if known else {})})
        return False
    revoked = await m.db.main.awrite(commit)
    if revoked is None:
        from .hub_claim import RELEASE_COMMAND
        raise HarnessError(409, f"this is the Hub's key; release the Hub on the daemon host with `{RELEASE_COMMAND}`",
                           code="hub_key")
    if not revoked:
        raise HarnessError(404, "no such active key")
    return Response(status_code=204)


class _FramingFastAPI(FastAPI):
    """Apply framing policy outside the error middleware, including its generated 500 responses."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await super().__call__(scope, receive, send)

        async def framed_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = "frame-ancestors 'none'"
                headers["X-Frame-Options"] = "DENY"
            await send(message)

        await super().__call__(scope, receive, framed_send)


def create_app(manager: Manager | None = None) -> FastAPI:
    # Built now, not in the lifespan: which add-on modules are present decides which routes exist.
    manager = manager or Manager(config_mod.load())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = manager
        telemetry.enable_asyncio_debug()
        probe = telemetry.LoopLagProbe()
        probe.start()
        await app.state.manager.start()
        yield
        await app.state.manager.stop()
        await probe.stop()

    app = _FramingFastAPI(title="agent-harness", lifespan=lifespan)
    app.middleware("http")(_access_guard)
    app.add_middleware(WebGzipMiddleware, web=WEB)
    app.add_exception_handler(HarnessError, harness_error)
    web_router.install(app)
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    from . import apps, modules
    apps.register(app, manager.cfg)
    api_router.install(app)
    modules.install_routes(app, manager.cfg, "owner")
    modules.install_routes(app, manager.cfg, "public")
    from . import admin
    admin.register(app, mgr, modules.admin_paths(manager.cfg), manager.cfg)
    # Keep /static for installed bundled clients, while making harness/web directly deployable at a static-site root.
    # This catch-all mount is last so daemon/API routes always win.
    app.mount("/", StaticFiles(directory=WEB), name="web-root")
    # Registered last so Host admission precedes even aliases/module middleware that return early.
    app.middleware("http")(guard)
    return app
