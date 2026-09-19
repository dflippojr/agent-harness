"""Project lookup keyed by (user_id, slug).

Owner projects stay in the checked-in catalog and `data_dir/projects.yaml` overlay, always under
`user_id = owner`. Member project metadata is non-secret durable state in SQLite. Different accounts
may reuse the same display slug. Member projects force owner-only capabilities off.
"""

from __future__ import annotations

from .config import PROJECT_NAME, Project
from .principal import OWNER_USER_ID
from .storage import repos_dir


def member_project(cfg, row: dict) -> Project:
    """Build a Project for a member row. Owner-only modules are forced off regardless of defaults."""
    repo = (row.get("repo") or "").strip()
    return Project(
        name=row["slug"],
        description=row.get("description") or "",
        repo=repo,
        target="tower",
        homelab=False,
        memory_library=False,
        images=False,
        web=True,
        session_search=True,
        owner_id=row["user_id"],
        managed=True,
        quota_mb=int(row.get("quota_mb") or 0),
    )


def get_project(cfg, db, user_id: str, slug: str) -> Project | None:
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    if user_id == OWNER_USER_ID:
        project = cfg.projects.get(slug)
        if project is None:
            return None
        if getattr(project, "owner_id", OWNER_USER_ID) not in ("", OWNER_USER_ID):
            return None
        return project
    if db is None:
        return None
    row = db.get_member_project(user_id, slug)
    return member_project(cfg, row) if row else None


def list_projects(cfg, db, user_id: str) -> list[Project]:
    if user_id == OWNER_USER_ID:
        return [p for p in cfg.projects.values() if getattr(p, "owner_id", OWNER_USER_ID) in ("", OWNER_USER_ID)]
    if db is None:
        return []
    return [member_project(cfg, row) for row in db.list_member_projects(user_id)]


def public_project(project: Project, *, include_repo: bool = False) -> dict:
    out = {
        "name": project.name,
        "description": project.description,
        "repo": bool(project.repo) if not include_repo else project.repo,
        "homelab": bool(project.homelab),
        "target": project.target,
        "managed": bool(project.managed),
        "owner_id": project.owner_id,
    }
    if not include_repo:
        out["repo"] = bool(project.repo)
    return out


def validate_slug(name: str) -> str:
    slug = (name or "").strip().lower()
    if not PROJECT_NAME.fullmatch(slug):
        raise ValueError("project name must be 1-64 lowercase letters, numbers, dots, dashes, or underscores")
    return slug


def member_managed_repo(cfg, user_id: str, slug: str) -> "Path":
    from pathlib import Path
    return repos_dir(cfg, user_id) / slug
