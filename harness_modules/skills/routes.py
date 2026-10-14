"""Owner skills routes, unchanged on both API surfaces."""
from __future__ import annotations

import sqlite3
from fastapi import Request
from pydantic import BaseModel
from harness.modules import HarnessError, RouteTable, require_owner, manager as mgr
from .service import SkillError

owner_routes = RouteTable()

class SkillInstall(BaseModel):
    content_hash: str


class SkillAllowlist(BaseModel):
    projects: list[str] = []


class SkillReject(BaseModel):
    reason: str = ""


def skills_or_400(m):
    if m.skills is None:
        raise HarnessError(400, "instruction skills are disabled")
    return m.skills


def skill_op(fn):
    try:
        return fn()
    except SkillError as e:
        raise HarnessError(e.status, str(e)) from e
    except sqlite3.IntegrityError as e:
        raise HarnessError(409, "skill store constraint failed") from e


def skills_owner(request: Request):
    require_owner(request)
    return skills_or_400(mgr(request))


@owner_routes.get("/skills")
async def skills_overview(request: Request):
    """List proposals and installed skills; returns empty lists with enabled false when the module is off."""
    m = require_owner(request)
    if m.skills is None:
        return {"enabled": False, "proposals": [], "installed": []}
    return m.skills.list_overview()


@owner_routes.get("/skills/enabled")
async def skills_enabled(request: Request):
    """List installed skills that are enabled; returns an empty list when the module is off."""
    m = require_owner(request)
    if m.skills is None:
        return []
    return m.skills.list_enabled()


@owner_routes.get("/skills/proposals/{pid}")
async def skill_proposal(pid: str, request: Request):
    """Return one proposal, including its body; 404 if no proposal has that id."""
    store = skills_owner(request)
    return skill_op(lambda: store.get_proposal(pid, include_body=True))


@owner_routes.post("/skills/proposals/{pid}/install")
async def skill_install(pid: str, body: SkillInstall, request: Request):
    """Install a proposal whose content hash matches the reviewed bytes; 409 if the hash is stale, rejected, or fails validation."""
    store = skills_owner(request)
    return skill_op(lambda: store.install(pid, body.content_hash))


@owner_routes.post("/skills/proposals/{pid}/reject")
async def skill_reject(pid: str, body: SkillReject, request: Request):
    """Reject a proposal and block its content hash; an installed proposal must be uninstalled first."""
    store = skills_owner(request)
    return skill_op(lambda: store.reject(pid, body.reason))


@owner_routes.post("/skills/proposals/{pid}/reopen")
async def skill_reopen(pid: str, request: Request):
    """Reopen a rejected proposal and lift its content-hash block; it returns to validated or draft."""
    store = skills_owner(request)
    return skill_op(lambda: store.reopen(pid))


@owner_routes.post("/skills/proposals/{pid}/review")
async def skill_hosted_review(pid: str, request: Request):
    """Queue an advisory hosted review that uses reviewer quota and returns before it runs; 400 if none is configured."""
    store = skills_owner(request)
    if store.reviewer is None:
        raise HarnessError(400, "skill review is not available")
    return skill_op(lambda: store.reviewer.request_hosted(pid))


@owner_routes.delete("/skills/proposals/{pid}", status_code=204)
async def skill_delete_draft(pid: str, request: Request):
    """Delete a proposal and its draft files; 409 if it has been installed."""
    store = skills_owner(request)
    skill_op(lambda: store.delete_draft(pid))


@owner_routes.post("/skills/{slug}/enable")
async def skill_enable(slug: str, request: Request):
    """Turn an installed skill on without changing its version or project allowlist."""
    store = skills_owner(request)
    return skill_op(lambda: store.set_enabled(slug, True))


@owner_routes.post("/skills/{slug}/disable")
async def skill_disable(slug: str, request: Request):
    """Turn an installed skill off without changing its version or project allowlist."""
    store = skills_owner(request)
    return skill_op(lambda: store.set_enabled(slug, False))


@owner_routes.post("/skills/{slug}/rollback")
async def skill_rollback(slug: str, request: Request):
    """Roll an installed skill back to its previous version; 409 when no older version exists."""
    store = skills_owner(request)
    return skill_op(lambda: store.rollback(slug))


@owner_routes.post("/skills/{slug}/uninstall")
async def skill_uninstall(slug: str, request: Request):
    """Remove an installed skill's files and project allowlist."""
    store = skills_owner(request)
    skill_op(lambda: store.uninstall(slug))
    return {"ok": True}


@owner_routes.put("/skills/{slug}/projects")
async def skill_projects(slug: str, body: SkillAllowlist, request: Request):
    """Replace the projects an installed skill is allowed in; unknown project names get 400."""
    require_owner(request)
    m = mgr(request)
    return skill_op(lambda: skills_or_400(m).set_allowlist(slug, body.projects, list(m.cfg.projects)))


@owner_routes.get("/skills/{slug}/export")
async def skill_export(slug: str, request: Request):
    """Return the installed skill's current version as an exportable bundle with its content hash."""
    store = skills_owner(request)
    return skill_op(lambda: store.export_bundle(slug))
