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

from .principal import (  # re-exported for existing imports
    OWNER_USER_ID,
    Principal,
    guest_for,
    parse_guest_until,
    require_owner_allowlist,
    resolve_human,
)

Access = Principal

OWNER_GET_PREFIXES = ("/keys", "/metrics", "/maintenance", "/skills")
RUNNER_PREFIX = "/runners/"
MEMBER_FORBIDDEN_PREFIXES = (
    "/keys", "/metrics", "/maintenance", "/jobs", "/images", "/gpu",
    "/remote-control", "/memory", "/templates", "/notify", "/pairing-codes",
    "/runner-pairing-codes", "/api/admin", "/api/v1/images", "/api/v1/remote-control",
)
MEMBER_FORBIDDEN_EXACT = frozenset({"/keys", "/metrics", "/maintenance", "/jobs", "/images", "/gpu",
                                    "/memory", "/templates", "/notify/test"})


def resolve_access(cfg, login: str | None, db=None) -> Access:
    """Classify a Tailscale login. Missing login (localhost) is always the owner."""
    return resolve_human(cfg, login, db)


def guest_forbidden(access: Access, method: str, path: str) -> str | None:
    """Return an error detail if this guest request is refused, else None."""
    if access.role != "guest":
        return None
    if path == "/api/admin" or path.startswith("/api/admin/"):
        return "demo access cannot use the owner API"
    if method in ("GET", "HEAD", "OPTIONS"):
        if any(path == prefix or path.startswith(prefix + "/") for prefix in OWNER_GET_PREFIXES):
            return "demo access cannot view owner credentials"
        return None
    if path.startswith(RUNNER_PREFIX):
        return None
    return "demo access is read-only"


def member_forbidden(access: Access, method: str, path: str) -> str | None:
    """Return an error detail if this member request is refused, else None.

    Members use the account-scoped session surface. Server authorization is authoritative even when
    Agent Harness Web hides the matching navigation.
    """
    if access.role != "member":
        return None
    if not access.allowed:
        return access.detail or "this household account is disabled"
    if path == "/api/admin" or path.startswith("/api/admin/"):
        return "members cannot use the owner API"
    if path == "/runners" or path.startswith(RUNNER_PREFIX):
        # GET /runners is the status list; /runners/{name}/… is poll/results (runner tokens, not members).
        return "members cannot use Mac or other runners"
    if any(path == prefix or path.startswith(prefix + "/") for prefix in MEMBER_FORBIDDEN_PREFIXES):
        if path.startswith("/jobs") or path == "/jobs":
            return "members cannot use scheduled jobs"
        if path.startswith("/images") or path == "/images" or path.startswith("/api/v1/images"):
            return "members cannot use image generation"
        if path.startswith("/gpu"):
            return "members cannot change GPU or machine settings"
        if path.startswith("/remote-control") or path.startswith("/api/v1/remote-control"):
            return "members cannot use Remote Control"
        if path.startswith("/memory"):
            return "members cannot use the memory library"
        if path.startswith("/keys") or path.startswith("/pairing-codes") or path.startswith("/runner-pairing-codes"):
            return "members cannot manage owner, app, or device credentials"
        if path.startswith("/maintenance"):
            return "members cannot use maintenance or backups"
        if path.startswith("/templates"):
            return "members cannot manage owner templates"
        if path.startswith("/notify"):
            return "members cannot use notifications"
        if path.startswith("/metrics"):
            return "members cannot view owner metrics"
        return "members cannot use owner-only operations"
    if path.startswith("/backends/") and method not in ("GET", "HEAD", "OPTIONS"):
        return "members cannot change machine settings"
    if path == "/profile" and method not in ("GET", "HEAD", "OPTIONS"):
        return "members cannot change the owner profile"
    return None
