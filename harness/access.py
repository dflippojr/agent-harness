"""Tailscale login roles for Agent Harness Web: owner, household member, and time-boxed guest.

The PWA is the first-party Agent Harness Web. `allowed_logins` is the owner allowlist. Household
members are owner-provisioned SQLite accounts keyed by opaque `user_id` and authenticated only by
the exact `Tailscale-User-Login`. `guests` lets a named tailnet login look around for a limited time
without owner or member powers.

Guests are ignored when `allowed_logins` is empty AND no member accounts exist (the daemon then
treats every tailnet login as owner, as before). Creating the first member requires an explicit
owner allowlist; startup fails closed if member records exist while open-owner mode is configured.
"""

from __future__ import annotations

import re

from .principal import (  # re-exported for existing imports
    OWNER_USER_ID,
    Principal,
    guest_for,
    parse_guest_until,
    require_owner_allowlist,
    resolve_human,
)

Access = Principal

KEYS = "/keys"
METRICS = "/metrics"
MAINTENANCE = "/maintenance"
SKILLS = "/skills"
CHATS = "/chats"
JOBS = "/jobs"
MEMORY = "/memory"
TEMPLATES = "/templates"
SMART_APPROVALS = "/smart-approvals"
ADMIN_API = "/api/admin"

# Only reviewed demo reads belong here. Exact routes keep new status/admin subroutes closed by default.
GUEST_GET_PATHS = frozenset({
    "/", "/index.html", "/sw.js", "/manifest.webmanifest", "/app.js", "/client.mjs", "/style.css",
    "/icon-180.png", "/icon-192.png", "/icon-512.png", "/health", "/me", "/profile", "/projects",
    "/models", "/sessions", "/events", "/queue", "/search", "/templates", "/jobs", "/images", "/api/v1",
})
GUEST_GET_PATTERNS = (
    r"/(?:static|lib|pages)/[^?]+",
    r"/sessions/[^/]+(?:/(?:checkpoints|changes|review-comments|approvals|transcript|metrics|events))?",
    r"/compare/[^/]+",
    r"/jobs/[^/]+",
    r"/images/[^/]+",
)
RUNNER_PREFIX = "/runners/"
MEMBER_FORBIDDEN_PREFIXES = (
    KEYS, METRICS, MAINTENANCE, JOBS,
    "/remote-control", MEMORY, TEMPLATES, "/notify", "/pairing-codes",
    "/runner-pairing-codes", SMART_APPROVALS, ADMIN_API,
    "/api/v1/remote-control", SKILLS, CHATS,
)
MEMBER_FORBIDDEN_EXACT = frozenset({CHATS, KEYS, METRICS, MAINTENANCE, JOBS,
                                    MEMORY, TEMPLATES, "/notify/test", SMART_APPROVALS})
# First match wins; a forbidden prefix matching none of these gets a generic message.
MEMBER_FORBIDDEN_DETAILS = (
    ((JOBS,), "members cannot use scheduled jobs"),
    (("/remote-control", "/api/v1/remote-control"), "members cannot use Remote Control"),
    ((MEMORY,), "members cannot use the memory library"),
    ((KEYS, "/pairing-codes", "/runner-pairing-codes"),
     "members cannot manage owner, app, or device credentials"),
    ((MAINTENANCE,), "members cannot use maintenance or backups"),
    ((TEMPLATES,), "members cannot manage owner templates"),
    (("/notify",), "members cannot use notifications"),
    ((METRICS,), "members cannot view owner metrics"),
    ((SMART_APPROVALS,), "members cannot use smart approvals"),
    ((SKILLS,), "members cannot manage instruction skills"),
    ((CHATS,), "Chat is only available to the owner"),
)


def resolve_access(cfg, login: str | None, db=None) -> Access:
    """Classify a Tailscale login. Missing login (localhost) is always the owner."""
    return resolve_human(cfg, login, db)


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _under_any_prefix(path: str, prefixes) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _member_owner_only_detail(path: str) -> str:
    for prefixes, detail in MEMBER_FORBIDDEN_DETAILS:
        if path.startswith(prefixes):
            return detail
    return "members cannot use owner-only operations"


def guest_forbidden(access: Access, method: str, path: str) -> str | None:
    """Return an error detail if this guest request is refused, else None."""
    if access.role != "guest":
        return None
    if path == ADMIN_API or path.startswith(ADMIN_API + "/"):
        return "demo access cannot use the owner API"
    if method in SAFE_METHODS:
        if path in GUEST_GET_PATHS or any(re.fullmatch(pattern, path) for pattern in GUEST_GET_PATTERNS):
            return None
        return "demo access cannot view owner credentials"
    if path.startswith(RUNNER_PREFIX):
        return None
    return "demo access is read-only"


def member_forbidden(access: Access, method: str, path: str, cfg=None) -> str | None:
    """Return an error detail if this member request is refused, else None.

    Members use the account-scoped session surface. Server authorization is authoritative even when
    Agent Harness Web hides the matching navigation.
    """
    if access.role != "member":
        return None
    if not access.allowed:
        return access.detail or "this household account is disabled"
    if path == ADMIN_API or path.startswith(ADMIN_API + "/"):
        return "members cannot use the owner API"
    if cfg is not None:
        from .modules import member_forbidden as module_refusal
        detail = module_refusal(cfg, path)
        if detail:
            return detail
    if path == "/runners" or path.startswith(RUNNER_PREFIX):
        # GET /runners is the status list; /runners/{name}/… is poll/results (runner tokens, not members).
        return "members cannot use Mac or other runners"
    if _under_any_prefix(path, MEMBER_FORBIDDEN_PREFIXES):
        return _member_owner_only_detail(path)
    if method in SAFE_METHODS:
        return None
    if path.startswith("/backends/"):
        return "members cannot change machine settings"
    if path == "/profile":
        return "members cannot change the owner profile"
    return None
