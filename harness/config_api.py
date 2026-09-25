"""App and owner configuration APIs (issue #66)."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

from .settings_service import SettingsError, schedule_exit


class AdminPatch(BaseModel):
    revision: int | None = None
    dry_run: bool = False
    changes: dict = Field(default_factory=dict)
    values: dict | None = None  # alias for changes
    reset: list[str] = Field(default_factory=list)


class AdminRevision(BaseModel):
    revision: int | None = None
    confirm: bool = False
    dry_run: bool = False


def _changes(body: AdminPatch) -> dict:
    changes = dict(body.changes or body.values or {})
    for key in body.reset:
        changes[key] = None
    return changes


@contextmanager
def _settings_errors():
    """Translate SettingsError into the HTTP-facing HarnessError."""
    try:
        yield
    except SettingsError as e:
        _raise(e)


def _raise(error: SettingsError):
    from .manager import HarnessError
    raise HarnessError(error.status, str(error), error.code, keys=error.keys, details=error.details)


def register_admin(app: FastAPI, mgr, require_admin) -> list[dict]:
    prefix = "/api/admin/v1"

    def service(request: Request):
        return mgr(request).settings

    def actor(request: Request):
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token:
            key = mgr(request).db.api_key_by_secret(token)
            if key:
                return key
        ident = getattr(request.state, "access", None)
        return {"id": getattr(ident, "login", None) or "owner", "kind": "owner"}

    @app.get(prefix + "/config/schema")
    async def admin_schema(request: Request):
        require_admin(request, mgr)
        return service(request).admin_schema()

    @app.get(prefix + "/config")
    async def admin_config(request: Request, response: Response):
        require_admin(request, mgr)
        view = service(request).admin_view()
        response.headers["ETag"] = f'"{view["revision"]}"'
        return view

    @app.post(prefix + "/config/validate")
    async def admin_validate(body: AdminPatch, request: Request):
        require_admin(request, mgr)
        with _settings_errors():
            plan = service(request).validate_admin(_changes(body), body.revision, actor=actor(request))
        return plan.as_dict(service(request).registry)

    @app.patch(prefix + "/config")
    async def admin_patch(body: AdminPatch, request: Request, response: Response):
        require_admin(request, mgr)
        with _settings_errors():
            view = service(request).patch_admin(_changes(body), body.revision, dry_run=body.dry_run,
                                                actor=actor(request))
        response.headers["ETag"] = f'"{view.get("revision", 0)}"'
        return view

    @app.post(prefix + "/config/rollback")
    async def admin_rollback(body: AdminRevision, request: Request):
        require_admin(request, mgr)
        if not body.confirm and not body.dry_run:
            raise_confirm("rollback")
        with _settings_errors():
            return service(request).rollback(body.revision, dry_run=body.dry_run, actor=actor(request))

    @app.post(prefix + "/config/restart", status_code=202)
    async def admin_restart(body: AdminRevision, request: Request):
        require_admin(request, mgr)
        if not body.confirm:
            raise_confirm("restart")
        with _settings_errors():
            result = service(request).request_restart(body.revision, actor=actor(request))
        schedule_exit()
        return result

    return [
        {"method": "GET", "path": prefix + "/config/schema"},
        {"method": "GET", "path": prefix + "/config"},
        {"method": "POST", "path": prefix + "/config/validate"},
        {"method": "PATCH", "path": prefix + "/config"},
        {"method": "POST", "path": prefix + "/config/rollback"},
        {"method": "POST", "path": prefix + "/config/restart"},
    ]


def raise_confirm(action: str):
    from .manager import HarnessError
    raise HarnessError(400, f"{action} requires confirm: true", "confirmation_required")


def register_app(app: FastAPI, mgr, auth, owner_key) -> None:
    def require_app(request, scope: str) -> dict:
        from .manager import HarnessError
        key = auth(request, scope)
        if owner_key(key) or key.get("kind") != "app":
            raise HarnessError(403, "only a live app token can use app configuration", "forbidden")
        if key.get("revoked_at"):
            raise HarnessError(401, "missing or invalid app token")
        return key

    @app.get("/api/v1/config/schema")
    async def app_schema(request: Request):
        key = require_app(request, "sessions")
        return mgr(request).settings.app_schema(key)

    @app.get("/api/v1/config")
    async def app_config(request: Request, response: Response):
        key = require_app(request, "sessions")
        view = mgr(request).settings.app_view(key["id"], key)
        response.headers["ETag"] = f'"{view["revision"]}"'
        return view

    @app.patch("/api/v1/config")
    async def app_patch(body: AdminPatch, request: Request, response: Response):
        key = require_app(request, "sessions")
        with _settings_errors():
            view = mgr(request).settings.patch_app(key["id"], key, _changes(body), body.revision,
                                                   dry_run=body.dry_run)
        response.headers["ETag"] = f'"{view.get("revision", 0)}"'
        return view
