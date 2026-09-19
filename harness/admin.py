"""Owner API (/api/admin/v1): Agent Harness Web operations that ordinary apps must not receive.

Issue #24. The least-privilege app contract stays at /api/v1. This surface versions the daemon's
operator routes (sessions, search, jobs, keys, GPU, maintenance, Remote Control trust, …) under a
stable prefix. Bundled and separately hosted Agent Harness Web both use this contract.

Auth is an explicit owner credential:
- Tailscale/localhost owner identity (no bearer token), same as bundled Agent Harness Web; or
- a bearer token of kind ``owner`` holding the ``admin`` scope (prefix ``ho-``).

App and device tokens are refused even when the request also has owner Tailscale identity, so a
third-party app cannot reach this surface by presenting its own key.
"""

from __future__ import annotations

import logging
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from . import access as access_mod
from .manager import HarnessError

log = logging.getLogger("harness.admin")

API_VERSION = "1.7"
ADMIN_SCOPE = "admin"
OWNER_KIND = "owner"
ADMIN_SCOPE_HELP = "owner-only Agent Harness Web operations under /api/admin/v1"
PREFIX = "/api/admin/v1"

# Candidate owner operations from issue #24. Runner poll/results and ntfy token buttons stay
# off this surface: they use their own credentials, not owner identity.
ADMIN_PATHS = frozenset({
    "/me",
    "/profile",
    "/projects",
    "/runners",
    "/models",
    "/models/status",
    "/models/warm",
    "/backends",
    "/backends/{name}",
    "/images",
    "/images/warmup",
    "/images/cooldown",
    "/images/{iid}",
    "/images/{iid}/upscale",
    "/gpu",
    "/gpu/{action}",
    "/remote-control",
    "/remote-control/{project}",
    "/remote-control/{project}/trust",
    "/remote-control/{project}/stop",
    "/queue",
    "/sessions",
    "/sessions/{ref}",
    "/sessions/{ref}/messages",
    "/sessions/{ref}/rerun",
    "/sessions/{ref}/changes",
    "/sessions/{ref}/review/{action}",
    "/sessions/{ref}/approvals",
    "/sessions/{ref}/approvals/{approval_id}",
    "/sessions/{ref}/cancel",
    "/sessions/{ref}/transcript",
    "/sessions/{ref}/events",
    "/search",
    "/memory",
    "/memory/profile",
    "/maintenance",
    "/maintenance/cleanup",
    "/maintenance/backup",
    "/maintenance/image-archive/retention/preview",
    "/maintenance/image-archive/retention/apply",
    "/jobs",
    "/jobs/preview",
    "/jobs/{jid}",
    "/jobs/{jid}/run",
    "/templates",
    "/templates/{tid}",
    "/notify/test",
    "/events",
    "/keys",
    "/keys/{kid}",
    "/pairing-codes",
    "/pairing-codes/{pid}",
    "/runner-pairing-codes",
    "/runner-pairing-codes/{pid}",
})


def parse_key_spec(body: dict | None) -> tuple[str, str, str]:
    """Validate POST /keys (and /api/admin/v1/keys). Returns (name, scopes, kind)."""
    from .apps import SCOPES
    body = body or {}
    name = str(body.get("name") or "").strip()
    if not name:
        raise HarnessError(400, "name is required")
    scopes = body.get("scopes") or ["inference"]
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise HarnessError(400, f"unknown scopes {scopes!r}; known: {', '.join(SCOPES)}")
    unknown = [s for s in scopes if s not in SCOPES and s != ADMIN_SCOPE]
    if unknown:
        raise HarnessError(400, f"unknown scopes {unknown}; known: {', '.join(SCOPES)}")
    kind_in = body.get("kind") or ""
    wants_admin = ADMIN_SCOPE in scopes or kind_in == OWNER_KIND
    if wants_admin:
        if kind_in == "app":
            raise HarnessError(400, "admin is an owner scope; app tokens cannot hold it")
        if kind_in == "device":
            raise HarnessError(400, "admin is an owner scope; device tokens cannot hold it")
        ordered = [ADMIN_SCOPE, *[s for s in dict.fromkeys(scopes) if s != ADMIN_SCOPE]]
        return name[:60], " ".join(ordered), OWNER_KIND
    kind = "app" if kind_in == "app" else "device"
    return name[:60], " ".join(dict.fromkeys(scopes)), kind


def require_admin(request: Request, mgr) -> dict | None:
    """Accept Tailscale/localhost owner, or an owner bearer token with admin scope.

    Returns the key row when a bearer token was used, else None. App and device tokens
    always raise, even from localhost, so tests can prove they cannot reach this surface.
    """
    m = mgr(request)
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token:
        key = m.db.api_key_by_secret(token)
        if key is None:
            raise HarnessError(401, "missing or invalid owner token")
        scopes = set((key.get("scopes") or "").split())
        if key.get("kind") != OWNER_KIND or ADMIN_SCOPE not in scopes:
            raise HarnessError(403, "app tokens cannot use the owner API")
        raw_origin = request.headers.get("origin", "")
        if raw_origin:
            from .apps import daemon_origins, normalize_origin
            try:
                origin = normalize_origin(raw_origin)
            except ValueError as e:
                raise HarnessError(403, str(e))
            if origin not in daemon_origins(m.cfg) and origin not in (key.get("origins") or []):
                raise HarnessError(403, "this owner token is not approved for this origin")
        return key
    raw_origin = request.headers.get("origin", "")
    if raw_origin:
        from .apps import daemon_origins, normalize_origin
        try:
            origin = normalize_origin(raw_origin)
        except ValueError as e:
            raise HarnessError(403, str(e))
        if origin not in daemon_origins(m.cfg):
            raise HarnessError(401, "cross-origin browser requests require an owner token")
    elif request.headers.get("sec-fetch-site") == "cross-site":
        raise HarnessError(401, "cross-origin browser requests require an owner token")
    ident = getattr(request.state, "access", None)
    if ident is None:
        ident = access_mod.resolve_access(m.cfg, request.headers.get("tailscale-user-login"), m.db)
    if ident.role == "guest":
        raise HarnessError(403, "demo access cannot use the owner API")
    if ident.role == "member":
        raise HarnessError(403, "members cannot use the owner API")
    if ident.role != "owner" or not ident.allowed:
        raise HarnessError(403, "owner credentials required")
    return None


def _template_re(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\{[^}/]+\}", r"[^/]+", path) + "$")


class ProviderCredentialRequest(BaseModel):
    app_id: str
    backend: str
    secret_ref: str = ""
    policy: str = "api_key"
    models: list[str] = Field(default_factory=list)


class AccountCreateRequest(BaseModel):
    login: str
    display_name: str
    disk_quota_bytes: int | None = None
    max_running: int | None = None
    max_queued: int | None = None


class AccountUpdateRequest(BaseModel):
    display_name: str | None = None
    login: str | None = None
    enabled: bool | None = None
    disk_quota_bytes: int | None = None
    max_running: int | None = None
    max_queued: int | None = None


def register(app: FastAPI, mgr) -> None:
    matchers = [_template_re(path) for path in ADMIN_PATHS]
    operations: list[dict] = []
    existing = [route for route in app.routes if isinstance(route, APIRoute) and route.path in ADMIN_PATHS]
    missing = ADMIN_PATHS - {route.path for route in existing}
    if missing:
        log.warning("admin API has no unversioned handler for %s", ", ".join(sorted(missing)))
    for route in existing:
        for method in sorted(m for m in route.methods if m != "HEAD"):
            operations.append({"method": method, "path": PREFIX + route.path})
    operations.extend([
        {"method": "GET", "path": PREFIX + "/provider-credentials"},
        {"method": "POST", "path": PREFIX + "/provider-credentials"},
        {"method": "DELETE", "path": PREFIX + "/provider-credentials/{credential_id}"},
        {"method": "GET", "path": PREFIX + "/accounts"},
        {"method": "POST", "path": PREFIX + "/accounts"},
        {"method": "GET", "path": PREFIX + "/accounts/audit"},
        {"method": "GET", "path": PREFIX + "/accounts/{user_id}"},
        {"method": "PATCH", "path": PREFIX + "/accounts/{user_id}"},
    ])
    from . import config_api
    operations.extend(config_api.register_admin(app, mgr, require_admin))
    operations.sort(key=lambda row: (row["path"], row["method"]))

    @app.get(PREFIX)
    async def admin_root(request: Request):
        require_admin(request, mgr)
        return {
            "api_version": API_VERSION,
            "server": "agent-harness",
            "capabilities": mgr(request).cfg.capabilities(),
            "scopes": {ADMIN_SCOPE: ADMIN_SCOPE_HELP},
            "auth": {
                "tailscale_owner": True,
                "bearer": f"{OWNER_KIND} token with {ADMIN_SCOPE} scope",
            },
            "operations": operations,
        }

    @app.get(PREFIX + "/provider-credentials")
    async def provider_credentials(request: Request):
        require_admin(request, mgr)
        return mgr(request).provider_credentials()

    @app.post(PREFIX + "/provider-credentials", status_code=201)
    async def set_provider_credential(body: ProviderCredentialRequest, request: Request):
        require_admin(request, mgr)
        manager = mgr(request)
        row = manager.set_app_provider_credential(body.app_id, body.backend, body.secret_ref,
                                                  body.policy, body.models)
        return next(item for item in manager.provider_credentials() if item["id"] == row["id"])

    @app.delete(PREFIX + "/provider-credentials/{credential_id}", status_code=204)
    async def revoke_provider_credential(credential_id: str, request: Request):
        require_admin(request, mgr)
        if not mgr(request).revoke_app_provider_credential(credential_id):
            raise HarnessError(404, "no active provider credential with that id")

    def _actor(request: Request) -> str:
        ident = getattr(request.state, "access", None)
        return ident.user_id if ident is not None else "owner"

    def _accounts(request: Request):
        from .accounts import AccountService
        return AccountService(mgr(request))

    @app.get(PREFIX + "/accounts")
    async def list_accounts(request: Request):
        require_admin(request, mgr)
        return _accounts(request).list_public()

    @app.post(PREFIX + "/accounts", status_code=201)
    async def create_account(body: AccountCreateRequest, request: Request):
        require_admin(request, mgr)
        return _accounts(request).create(_actor(request), body.login, body.display_name,
                                         body.disk_quota_bytes, body.max_running, body.max_queued)

    @app.get(PREFIX + "/accounts/audit")
    async def account_audit(request: Request, limit: int = 200):
        require_admin(request, mgr)
        return mgr(request).db.list_audit(limit)

    @app.get(PREFIX + "/accounts/{user_id}")
    async def get_account(user_id: str, request: Request):
        require_admin(request, mgr)
        svc = _accounts(request)
        return svc.public_account(svc._require(user_id))

    @app.patch(PREFIX + "/accounts/{user_id}")
    async def update_account(user_id: str, body: AccountUpdateRequest, request: Request):
        require_admin(request, mgr)
        svc = _accounts(request)
        actor = _actor(request)
        row = None
        if body.display_name is not None:
            row = svc.rename(actor, user_id, body.display_name)
        if body.login is not None:
            row = svc.rebind_login(actor, user_id, body.login)
        if body.enabled is not None:
            row = await svc.set_enabled(actor, user_id, body.enabled)
        if body.disk_quota_bytes is not None:
            row = svc.set_quota(actor, user_id, body.disk_quota_bytes)
        if body.max_running is not None or body.max_queued is not None:
            row = svc.set_concurrency(actor, user_id, body.max_running, body.max_queued)
        if row is None:
            row = svc.public_account(svc._require(user_id))
        return row

    @app.middleware("http")
    async def admin_alias(request: Request, call_next):
        path = request.url.path
        if path == PREFIX or not path.startswith(PREFIX + "/"):
            return await call_next(request)
        rest = path[len(PREFIX):]
        if not any(matcher.fullmatch(rest) for matcher in matchers):
            return await call_next(request)
        # The inner access/CORS middleware handles preflight without credentials. Preserve the public path
        # when rewriting actual requests so it still recognizes this as a versioned browser API call.
        if request.method == "OPTIONS":
            return await call_next(request)
        try:
            require_admin(request, mgr)
        except HarnessError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)
        request.scope["harness_original_path"] = path
        request.scope["path"] = rest
        if "raw_path" in request.scope:
            request.scope["raw_path"] = rest.encode("ascii")
        return await call_next(request)
