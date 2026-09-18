"""Tailscale login roles for Agent Harness Web: owner vs time-boxed guest.

The PWA is an owner console. `allowed_logins` is the owner allowlist. `guests` lets a named
tailnet login look around for a limited time without owner powers. Guests are ignored when
`allowed_logins` is empty (the daemon then treats every tailnet login as owner, as before).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config, GuestAccess

OWNER_GET_PREFIXES = ("/keys", "/metrics", "/maintenance", "/skills")
RUNNER_PREFIX = "/runners/"


@dataclass(frozen=True)
class Access:
    role: str  # owner | guest
    allowed: bool
    login: str | None
    until: datetime | None = None
    detail: str = ""

    def until_iso(self) -> str | None:
        if self.until is None:
            return None
        return self.until.isoformat()


def parse_guest_until(value: str) -> datetime | None:
    """Parse an ISO-8601 guest expiry. Naive values are local time. Empty means no expiry."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def guest_for(cfg: Config, login: str) -> tuple[GuestAccess | None, str]:
    """Return (guest, reason). reason is `expired`, `invalid-until`, or empty if active."""
    for guest in cfg.guests:
        if guest.login != login:
            continue
        if not guest.until:
            return guest, ""
        try:
            until = parse_guest_until(guest.until)
        except ValueError:
            return guest, "invalid-until"
        if until is None:
            return guest, ""
        if until <= datetime.now(timezone.utc):
            return guest, "expired"
        return guest, ""
    return None, ""


def resolve_access(cfg: Config, login: str | None) -> Access:
    """Classify a Tailscale login. Missing login (localhost) is always the owner."""
    if login is None:
        return Access(role="owner", allowed=True, login=None)
    if login in cfg.allowed_logins:
        return Access(role="owner", allowed=True, login=login)
    if not cfg.allowed_logins:
        return Access(role="owner", allowed=True, login=login)
    guest, reason = guest_for(cfg, login)
    if guest is not None:
        if reason == "expired":
            return Access(role="guest", allowed=False, login=login, detail="demo access expired")
        if reason == "invalid-until":
            return Access(role="guest", allowed=False, login=login,
                          detail="this tailnet login is not allowed")
        until = None
        if guest.until:
            try:
                until = parse_guest_until(guest.until)
            except ValueError:
                until = None
        return Access(role="guest", allowed=True, login=login, until=until)
    return Access(role="owner", allowed=False, login=login, detail="this tailnet login is not allowed")


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
