"""Owner API (/api/admin/v1): Agent Harness Web operations that ordinary apps must not receive.

Issue #24. The least-privilege app contract stays at /api/v1. This surface versions the daemon's
operator routes (sessions, search, jobs, keys, GPU, maintenance, Remote Control trust, …) under a
stable prefix. Bundled and separately hosted Agent Harness Web both use this contract.

Auth is an explicit owner credential:
- Tailscale/localhost owner identity (no bearer token), same as bundled Agent Harness Web; or
- a bearer token of kind ``owner`` holding the ``admin`` scope (prefix ``ho-``). The Hub's key (#543, role ``hub``)
  is one of these, except on the host-only Hub claim routes (harness/hub_claim.py).

App and device tokens are refused even when the request also has owner Tailscale identity, so a
third-party app cannot reach this surface by presenting its own key.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from . import access as access_mod
from . import audit_context, credential_audit
from . import compat
from .manager import HarnessError

log = logging.getLogger("harness.admin")

API_VERSION = "1.24"
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
    "/models",
    "/backends",
    "/backends/{name}",
    "/smart-approvals",
    "/queue",
    "/sessions",
    "/sessions/{ref}",
    "/sessions/{ref}/messages",
    "/sessions/{ref}/rerun",
    "/sessions/{ref}/changes",
    "/sessions/{ref}/review/{action}",
    "/sessions/{ref}/review-comments",
    "/sessions/{ref}/review-comments/{comment_id}",
    "/sessions/{ref}/review-comments/send",
    "/sessions/{ref}/approvals",
    "/sessions/{ref}/approvals/{approval_id}",
    "/sessions/{ref}/cancel",
    "/sessions/{ref}/transcript",
    "/sessions/{ref}/events",
    "/sessions/{ref}/metrics",
    "/sessions/{ref}/checkpoints",
    "/sessions/{ref}/checkpoints/{turn}/rewind",
    "/sessions/{ref}/checkpoints/{turn}/fork",
    "/sessions/{ref}/secret-findings/fix",
    "/sessions/{ref}/secret-findings/{fingerprint}/dismiss",
    "/sessions/{ref}/taint/clear",
    "/github/sessions",
    "/github/projects/{project}/items",
    "/github/projects/{project}/items/{number}",
    "/chats",
    "/chats/options",
    "/chats/{ref}",
    "/chats/{ref}/messages",
    "/chats/{ref}/cancel",
    "/chats/{ref}/events",
    "/chats/snippet-languages",
    "/chats/{ref}/snippets",
    "/chats/{ref}/snippets/{run_id}/cancel",
    "/maintenance",
    "/maintenance/cleanup",
    "/events",
    "/keys",
    "/keys/{kid}",
    "/pairing-codes",
    "/pairing-codes/{pid}",
})


def _validated_scopes(scopes) -> list[str]:
    from .apps import all_scopes
    known = all_scopes()
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise HarnessError(400, f"unknown scopes {scopes!r}; known: {', '.join(known)}")
    unknown = [s for s in scopes if s not in known and s != ADMIN_SCOPE]
    if unknown:
        raise HarnessError(400, f"unknown scopes {unknown}; known: {', '.join(known)}")
    return scopes


def parse_key_spec(body: dict | None) -> tuple[str, str, str]:
    """Validate POST /keys (and /api/admin/v1/keys). Returns (name, scopes, kind)."""
    body = body or {}
    name = str(body.get("name") or "").strip()
    if not name:
        raise HarnessError(400, "name is required")
    scopes = _validated_scopes(body.get("scopes") or ["inference"])
    kind_in = body.get("kind") or ""
    if kind_in == "hub" or body.get("role"):
        raise HarnessError(400, "a Hub key comes only from a Hub claim approved on the daemon host "
                                "(`harness hub approve`); no key can be made with a role")
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


def _normalized_origin(raw_origin: str) -> str:
    from .apps import normalize_origin
    try:
        return normalize_origin(raw_origin)
    except ValueError as e:
        raise HarnessError(403, str(e))


def _check_token_origin(request: Request, m, key: dict) -> None:
    raw_origin = request.headers.get("origin", "")
    if not raw_origin:
        return
    from .apps import daemon_origins
    origin = _normalized_origin(raw_origin)
    if origin not in daemon_origins(m.cfg) and origin not in (key.get("origins") or []):
        raise HarnessError(403, "this owner token is not approved for this origin")


def _check_browser_origin(request: Request, m) -> None:
    raw_origin = request.headers.get("origin", "")
    if raw_origin:
        from .apps import daemon_origins
        if _normalized_origin(raw_origin) not in daemon_origins(m.cfg):
            raise HarnessError(401, "cross-origin browser requests require an owner token")
    elif request.headers.get("sec-fetch-site") == "cross-site":
        raise HarnessError(401, "cross-origin browser requests require an owner token")


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
        _check_token_origin(request, m, key)
        return key
    _check_browser_origin(request, m)
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


class AppRetentionRequest(BaseModel):
    retention_days: float | None = Field(default=None, gt=0, le=36500)


class GitHubMemberAuthRequest(BaseModel):
    enabled: bool


class GitHubResetRequest(BaseModel):
    confirm: bool = False


def _collect_operations(app: FastAPI, mgr, paths: frozenset[str] = ADMIN_PATHS, cfg=None) -> list[dict]:
    operations: list[dict] = []
    existing = [route for route in app.routes if isinstance(route, APIRoute) and route.path in paths]
    missing = paths - {route.path for route in existing}
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
        {"method": "GET", "path": PREFIX + "/audit"},
        {"method": "GET", "path": PREFIX + "/accounts/{user_id}"},
        {"method": "PATCH", "path": PREFIX + "/accounts/{user_id}"},
        {"method": "GET", "path": PREFIX + "/github-member-auth"},
        {"method": "PUT", "path": PREFIX + "/github-member-auth"},
        {"method": "POST", "path": PREFIX + "/accounts/{user_id}/github-connection/reset"},
        {"method": "GET", "path": PREFIX + "/apps/erasures"},
        {"method": "POST", "path": PREFIX + "/apps/{app_id}/restore"},
        {"method": "PUT", "path": PREFIX + "/apps/{app_id}/retention"},
    ])
    from . import config_api, hub_claim, pairing_requests
    operations.extend(config_api.register_admin(app, mgr, require_admin))
    operations.extend(pairing_requests.register_admin(app, mgr, require_admin))
    operations.extend(hub_claim.register_admin(app, mgr, require_admin))
    from .modules import present
    for module in present(cfg) if cfg is not None else ():
        if module.register_admin:
            operations.extend(module.register_admin(app, mgr, require_admin))
    from . import google_signin_api
    operations.extend(google_signin_api.register(app, mgr))
    operations.sort(key=lambda row: (row["path"], row["method"]))
    return operations


async def _apply_account_update(svc, actor: str, user_id: str, body: AccountUpdateRequest) -> dict:
    row = None
    if body.display_name is not None:
        row = await asyncio.to_thread(svc.rename, actor, user_id, body.display_name)
    if body.login is not None:
        row = await asyncio.to_thread(svc.rebind_login, actor, user_id, body.login)
    if body.enabled is not None:
        row = await svc.set_enabled(actor, user_id, body.enabled)
    if body.disk_quota_bytes is not None:
        row = await asyncio.to_thread(svc.set_quota, actor, user_id, body.disk_quota_bytes)
    if body.max_running is not None or body.max_queued is not None:
        row = await asyncio.to_thread(svc.set_concurrency, actor, user_id, body.max_running, body.max_queued)
    if row is None:
        row = svc.public_account(svc._require(user_id))
    return row


def register(app: FastAPI, mgr, module_paths: frozenset[str] = frozenset(), cfg=None) -> None:
    """``module_paths``: the present add-on modules' owner routes to serve here too (Module.admin_paths)."""
    paths = ADMIN_PATHS | module_paths
    matchers = [_template_re(path) for path in paths]
    operations = _collect_operations(app, mgr, paths, cfg)

    @app.get(PREFIX)
    async def admin_root(request: Request):
        require_admin(request, mgr)
        return {
            "api_version": API_VERSION,
            "server": "agent-harness",
            **compat.metadata(mgr(request).cfg.capabilities()),
            "scopes": {ADMIN_SCOPE: ADMIN_SCOPE_HELP},
            "hub": {"claimed": mgr(request).db.main.hub_claim() is not None},
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
        key = require_admin(request, mgr)
        manager = mgr(request)
        ctx = _context(request, key)

        def commit():
            previous = manager.db.main.app_provider_credential(body.app_id, body.backend)
            row = manager.set_app_provider_credential(body.app_id, body.backend, body.secret_ref,
                                                      body.policy, body.models)
            fields = ["backend", "policy", "models", "secret_ref"] if previous is None else [
                f for f in ("policy", "models", "secret_ref") if previous.get(f) != row.get(f)]
            meta = {"grant_id": row["id"], "app_id": body.app_id, "backend": body.backend, "policy": row["policy"],
                    "fields": fields, "replaced": previous is not None}
            if previous is not None:
                meta["previous_grant_id"] = previous["id"]
            credential_audit.record(manager.db, ctx, "provider_grant.set", row["id"], "ok", "provider_grant", meta)
            return row
        try:
            row = await manager.db.main.awrite(commit)
        except HarnessError as e:
            if e.code != "audit_unavailable":  # a refused request; the audit failure itself is the error otherwise
                reason = "not_found" if e.status == 404 else "invalid_request"
                await manager.db.main.awrite(credential_audit.record, manager.db, ctx, "provider_grant.set", "",
                                             "denied", "provider_grant", {"reason": reason})
            raise
        return next(item for item in manager.provider_credentials() if item["id"] == row["id"])

    @app.delete(PREFIX + "/provider-credentials/{credential_id}", status_code=204)
    async def revoke_provider_credential(credential_id: str, request: Request):
        key = require_admin(request, mgr)
        manager = mgr(request)
        ctx = _context(request, key)

        def commit() -> bool:
            grant = manager.db.main.app_provider_credential_by_id(credential_id)
            if manager.revoke_app_provider_credential(credential_id):
                credential_audit.record(manager.db, ctx, "provider_grant.revoke", credential_id, "ok",
                                        "provider_grant", {"grant_id": credential_id, "app_id": grant["app_id"],
                                                           "backend": grant["backend"], "policy": grant["policy"]})
                return True
            known = grant is not None
            credential_audit.record(manager.db, ctx, "provider_grant.revoke", credential_id if known else "", "noop",
                                    "provider_grant", {"reason": "already_revoked" if known else "not_found"})
            return False
        if not await manager.db.main.awrite(commit):
            raise HarnessError(404, "no active provider credential with that id")

    # #330 decision 5: an App's default retention, and the erasures that revoking Apps scheduled.
    @app.get(PREFIX + "/apps/erasures")
    async def app_erasures(request: Request):
        require_admin(request, mgr)
        return mgr(request).db.pending_erasures()

    @app.post(PREFIX + "/apps/{app_id}/restore")
    async def restore_app(app_id: str, request: Request):
        admin_key = require_admin(request, mgr)
        m = mgr(request)
        ctx = _context(request, admin_key)

        def commit():
            try:
                row, token = m.restore_app(app_id)
            except HarnessError as e:
                if e.status != 404:
                    raise
                credential_audit.record(m.db, ctx, "app.restore", "", "noop", "api_key", {"reason": "grace_over"})
                return None, None
            credential_audit.record(m.db, ctx, "app.restore", app_id, "ok", "api_key",
                                    {"key_id": app_id, "app_id": app_id, "kind": row.get("kind")})
            return row, token
        row, key = await m.db.main.awrite(commit)
        if row is None:
            raise HarnessError(404, "no App with a pending erasure has that id")
        return JSONResponse({**row, "key": key},
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.put(PREFIX + "/apps/{app_id}/retention")
    async def set_app_retention(app_id: str, body: AppRetentionRequest, request: Request):
        admin_key = require_admin(request, mgr)
        m = mgr(request)
        ctx = _context(request, admin_key)

        def commit() -> bool:
            before = m.db.main.get_api_key(app_id)
            if m.db.main.set_app_retention(app_id, body.retention_days):
                credential_audit.record(m.db, ctx, "app.retention", app_id, "ok", "api_key", {
                    "app_id": app_id, "old_retention_days": before.get("retention_days"),
                    "new_retention_days": body.retention_days})
                return True
            credential_audit.record(m.db, ctx, "app.retention", "", "noop", "api_key", {"reason": "not_found"})
            return False
        if not await m.db.main.awrite(commit):
            raise HarnessError(404, "no App or device key has that id")
        return next(k for k in m.db.list_api_keys() if k["id"] == app_id)

    def _actor(request: Request, key: dict | None) -> str:
        return _context(request, key).actor_id

    def _context(request: Request, key: dict | None):
        """Audit context for a request `require_admin` accepted: a validated bearer key wins over ambient identity."""
        return audit_context.owner_context(key)

    def _accounts(request: Request):
        from .accounts import AccountService
        return AccountService(mgr(request))

    @app.get(PREFIX + "/accounts")
    async def list_accounts(request: Request):
        require_admin(request, mgr)
        return _accounts(request).list_public()

    @app.post(PREFIX + "/accounts", status_code=201)
    async def create_account(body: AccountCreateRequest, request: Request):
        key = require_admin(request, mgr)
        return await asyncio.to_thread(_accounts(request).create, _context(request, key), body.login,
                                       body.display_name, body.disk_quota_bytes, body.max_running, body.max_queued)

    @app.get(PREFIX + "/accounts/audit")
    async def account_audit(request: Request, limit: int = 200):
        require_admin(request, mgr)
        return await asyncio.to_thread(mgr(request).db.list_audit, limit)

    @app.get(PREFIX + "/audit")
    async def audit_review(request: Request, limit: int = 200, before_id: int | None = None,
                           actor_id: str | None = None, key_id: str | None = None, target_id: str | None = None,
                           action: str | None = None, outcome: str | None = None, since: float | None = None,
                           until: float | None = None):
        """Owner-only review of every retained audit row, newest first, with a cursor (#467)."""
        require_admin(request, mgr)
        if not 1 <= limit <= 500:
            raise HarnessError(400, "limit must be between 1 and 500")
        if before_id is not None and before_id < 1:
            raise HarnessError(400, "before_id must be a positive row id")
        for name, value in (("since", since), ("until", until)):
            if value is not None and not math.isfinite(value):
                raise HarnessError(400, f"{name} must be a finite timestamp")
        if since is not None and until is not None and since >= until:
            raise HarnessError(400, "since must be earlier than until")
        return await asyncio.to_thread(
            mgr(request).db.audit_page, limit, before_id, actor_id=actor_id, key_id=key_id, target_id=target_id,
            action=action, outcome=outcome, since=since, until=until)

    @app.get(PREFIX + "/accounts/{user_id}")
    async def get_account(user_id: str, request: Request):
        require_admin(request, mgr)
        svc = _accounts(request)
        return svc.public_account(svc._require(user_id))

    @app.patch(PREFIX + "/accounts/{user_id}")
    async def update_account(user_id: str, body: AccountUpdateRequest, request: Request):
        key = require_admin(request, mgr)
        svc = _accounts(request)
        return await _apply_account_update(svc, _context(request, key), user_id, body)

    # Issue #63: the owner switches member GitHub sign-in on or off, sees each member's coarse state, and can
    # erase a member's credential. The owner cannot connect, test, list repositories, or use it.
    @app.get(PREFIX + "/github-member-auth")
    async def github_member_auth(request: Request, refresh: bool = False):
        require_admin(request, mgr)
        gh = mgr(request).github_auth
        if gh.configured() and (refresh or gh.cached_preflight() is None):
            await asyncio.to_thread(gh.preflight, True)
        return gh.owner_view()

    @app.put(PREFIX + "/github-member-auth")
    async def set_github_member_auth(body: GitHubMemberAuthRequest, request: Request):
        from .github_auth import GitHubAuthError
        key = require_admin(request, mgr)
        try:
            return await asyncio.to_thread(mgr(request).github_auth.set_enabled, _actor(request, key), body.enabled)
        except GitHubAuthError as e:
            raise HarnessError(e.status, str(e), code=e.code) from None

    @app.post(PREFIX + "/accounts/{user_id}/github-connection/reset")
    async def reset_member_github(user_id: str, body: GitHubResetRequest, request: Request):
        from .github_auth import GitHubAuthError
        key = require_admin(request, mgr)
        if not body.confirm:
            raise HarnessError(400, "confirm the erase-only reset")
        _accounts(request)._require(user_id)
        try:
            await asyncio.to_thread(mgr(request).github_auth.disconnect, user_id, actor_id=_actor(request, key))
        except GitHubAuthError as e:
            raise HarnessError(e.status, str(e), code=e.code) from None
        return mgr(request).github_auth.owner_view()

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
