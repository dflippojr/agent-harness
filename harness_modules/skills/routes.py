"""Owner skills routes, unchanged on both API surfaces."""
from __future__ import annotations

import sqlite3
from typing import Any, NotRequired, TypedDict

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from harness.modules import HarnessError, RouteTable, documented, require_owner, manager as mgr
from .service import SkillError

owner_routes = RouteTable()

class SkillInstall(BaseModel):
    content_hash: str


class SkillAllowlist(BaseModel):
    projects: list[str] = []


class SkillReject(BaseModel):
    reason: str = ""


class _Reply(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow")  # documentation only: extra fields allowed


class InstalledSkill(_Reply):
    slug: str
    title: str
    purpose: str
    version: int
    content_hash: str
    enabled: bool
    projects: list[str]
    installed_at: NotRequired[float]


class SkillFinding(_Reply):
    code: str
    message: str
    path: NotRequired[str]
    severity: NotRequired[str]


class SkillReference(_Reply):
    path: str
    content: str


class SkillExample(_Reply):
    prompt: str
    expected: NotRequired[str]
    expected_behavior: NotRequired[str]


class SkillProposal(_Reply):
    """A proposal; the body (`skill_md`, references, examples, diff) only when one proposal is read."""
    id: str
    slug: str
    title: str
    purpose: str
    content_hash: str
    status: str
    activation_suggestion: NotRequired[str]
    review_status: NotRequired[str]
    review: NotRequired[dict[str, Any] | None]
    static_findings: NotRequired[list[SkillFinding]]
    skill_md: NotRequired[str]
    references: NotRequired[list[SkillReference]]
    examples: NotRequired[list[SkillExample]]
    source_session_id: NotRequired[str]
    diff: NotRequired[str]


class SkillsOverview(_Reply):
    enabled: bool
    proposals: list[SkillProposal]
    installed: list[InstalledSkill]


class SkillReviewStarted(_Reply):
    job_id: str
    usage_notice: str
    reviewer_model: str


class SkillManifest(_Reply):
    slug: str
    title: str
    purpose: str
    activation_suggestion: str
    content_hash: str
    validator_version: str


class SkillExport(_Reply):
    """The installed version as a bundle another Server can import."""
    slug: str
    title: str
    purpose: str
    version: int
    content_hash: str
    skill_md: str
    references: list[SkillReference]
    examples: list[SkillExample]
    manifest: SkillManifest


class SkillUninstalled(_Reply):
    ok: bool


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


@owner_routes.get("/skills", responses=documented(SkillsOverview))
async def skills_overview(request: Request):
    """List proposals and installed skills; returns empty lists with enabled false when the module is off."""
    m = require_owner(request)
    if m.skills is None:
        return {"enabled": False, "proposals": [], "installed": []}
    return m.skills.list_overview()


@owner_routes.get("/skills/enabled", responses=documented(list[InstalledSkill]))
async def skills_enabled(request: Request):
    """List installed skills that are enabled; returns an empty list when the module is off."""
    m = require_owner(request)
    if m.skills is None:
        return []
    return m.skills.list_enabled()


@owner_routes.get("/skills/proposals/{pid}", responses=documented(SkillProposal))
async def skill_proposal(pid: str, request: Request):
    """Return one proposal, including its body; 404 if no proposal has that id."""
    store = skills_owner(request)
    return skill_op(lambda: store.get_proposal(pid, include_body=True))


@owner_routes.post("/skills/proposals/{pid}/install", responses=documented(InstalledSkill))
async def skill_install(pid: str, body: SkillInstall, request: Request):
    """Install a proposal whose content hash matches the reviewed bytes; 409 if the hash is stale, rejected, or fails validation."""
    store = skills_owner(request)
    return skill_op(lambda: store.install(pid, body.content_hash))


@owner_routes.post("/skills/proposals/{pid}/reject", responses=documented(SkillProposal))
async def skill_reject(pid: str, body: SkillReject, request: Request):
    """Reject a proposal and block its content hash; an installed proposal must be uninstalled first."""
    store = skills_owner(request)
    return skill_op(lambda: store.reject(pid, body.reason))


@owner_routes.post("/skills/proposals/{pid}/reopen", responses=documented(SkillProposal))
async def skill_reopen(pid: str, request: Request):
    """Reopen a rejected proposal and lift its content-hash block; it returns to validated or draft."""
    store = skills_owner(request)
    return skill_op(lambda: store.reopen(pid))


@owner_routes.post("/skills/proposals/{pid}/review", responses=documented(SkillReviewStarted))
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


@owner_routes.post("/skills/{slug}/enable", responses=documented(InstalledSkill))
async def skill_enable(slug: str, request: Request):
    """Turn an installed skill on without changing its version or project allowlist."""
    store = skills_owner(request)
    return skill_op(lambda: store.set_enabled(slug, True))


@owner_routes.post("/skills/{slug}/disable", responses=documented(InstalledSkill))
async def skill_disable(slug: str, request: Request):
    """Turn an installed skill off without changing its version or project allowlist."""
    store = skills_owner(request)
    return skill_op(lambda: store.set_enabled(slug, False))


@owner_routes.post("/skills/{slug}/rollback", responses=documented(InstalledSkill))
async def skill_rollback(slug: str, request: Request):
    """Roll an installed skill back to its previous version; 409 when no older version exists."""
    store = skills_owner(request)
    return skill_op(lambda: store.rollback(slug))


@owner_routes.post("/skills/{slug}/uninstall", responses=documented(SkillUninstalled))
async def skill_uninstall(slug: str, request: Request):
    """Remove an installed skill's files and project allowlist."""
    store = skills_owner(request)
    skill_op(lambda: store.uninstall(slug))
    return {"ok": True}


@owner_routes.put("/skills/{slug}/projects", responses=documented(InstalledSkill))
async def skill_projects(slug: str, body: SkillAllowlist, request: Request):
    """Replace the projects an installed skill is allowed in; unknown project names get 400."""
    require_owner(request)
    m = mgr(request)
    return skill_op(lambda: skills_or_400(m).set_allowlist(slug, body.projects, list(m.cfg.projects)))


@owner_routes.get("/skills/{slug}/export", responses=documented(SkillExport))
async def skill_export(slug: str, request: Request):
    """Return the installed skill's current version as an exportable bundle with its content hash."""
    store = skills_owner(request)
    return skill_op(lambda: store.export_bundle(slug))
