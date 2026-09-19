"""Central principal model: owner | member | guest | app | device.

Authorization and ownership use opaque `user_id` values. Display names and Tailscale logins are
labels only. The durable machine-owner scope stays `user_id = "owner"` so existing paths do not move.

Resolution for a Tailscale login is: owner allowlist, enabled member, active guest, deny. A login
cannot occupy more than one role. Localhost (missing login) is always the owner.

Open-owner legacy mode — empty `allowed_logins` treats every tailnet login as owner — remains only
when no member accounts exist. Member records plus an empty allowlist fail closed at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config, GuestAccess

OWNER_USER_ID = "owner"
KINDS = ("owner", "member", "guest", "app", "device")
PUBLIC_CLONE_HOSTS = ("github.com", "gitlab.com", "codeberg.org")
DEFAULT_DISK_QUOTA_BYTES = 20 * 2**30  # 20 GiB
DEFAULT_MAX_RUNNING = 1
DEFAULT_MAX_QUEUED = 2
AUDIT_RETENTION_DAYS = 365


@dataclass(frozen=True)
class Principal:
    kind: str  # owner | member | guest | app | device
    user_id: str
    allowed: bool
    login: str | None = None
    display_name: str = ""
    until: datetime | None = None
    detail: str = ""
    enabled: bool = True
    key_id: str = ""
    scopes: frozenset[str] = frozenset()
    bundled: bool = False

    @property
    def role(self) -> str:
        """Human Tailscale role, or token kind. Guests and members keep their kind."""
        return self.kind

    def until_iso(self) -> str | None:
        if self.until is None:
            return None
        return self.until.isoformat()

    @property
    def is_human(self) -> bool:
        return self.kind in ("owner", "member", "guest")

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner" and self.allowed

    @property
    def is_member(self) -> bool:
        return self.kind == "member" and self.allowed and self.enabled

    def owns(self, user_id: str) -> bool:
        """Whether this principal may read/write objects stored under `user_id`."""
        if not self.allowed:
            return False
        if self.kind == "guest":
            return False
        if self.kind in ("owner", "app", "device"):
            return user_id == OWNER_USER_ID
        return user_id == self.user_id


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


def open_owner_mode(cfg: Config, member_count: int) -> bool:
    """Every tailnet login is owner only when the allowlist is empty and no members exist."""
    return not cfg.allowed_logins and member_count == 0


def require_owner_allowlist(cfg: Config, member_count: int) -> None:
    """Startup fail-closed: member records require an explicit owner login allowlist."""
    if member_count > 0 and not cfg.allowed_logins:
        raise ValueError(
            "member accounts require an explicit allowed_logins owner allowlist; "
            "open-owner mode is only valid when no household members exist"
        )


def _owner(login: str | None) -> Principal:
    return Principal(kind="owner", user_id=OWNER_USER_ID, allowed=True, login=login,
                     display_name="", bundled=True)


def resolve_human(cfg: Config, login: str | None, accounts=None) -> Principal:
    """Classify a Tailscale/localhost caller. `accounts` is a Database or any object with
    `member_count()` and `account_by_login(login)`."""
    if login is None:
        return _owner(None)
    member_count = accounts.member_count() if accounts is not None else 0
    member = accounts.account_by_login(login) if accounts is not None else None
    in_allowlist = login in cfg.allowed_logins
    if in_allowlist:
        return _owner(login)
    if member is not None:
        enabled = bool(member.get("enabled", 1))
        if not enabled:
            return Principal(kind="member", user_id=member["user_id"], allowed=False, login=login,
                             display_name=member.get("display_name") or "", enabled=False,
                             detail="this household account is disabled")
        return Principal(kind="member", user_id=member["user_id"], allowed=True, login=login,
                         display_name=member.get("display_name") or "", enabled=True, bundled=True)
    if not cfg.allowed_logins:
        if member_count > 0:
            return Principal(kind="owner", user_id=OWNER_USER_ID, allowed=False, login=login,
                             detail="this tailnet login is not allowed")
        return _owner(login)
    guest, reason = guest_for(cfg, login)
    if guest is not None:
        if reason == "expired":
            return Principal(kind="guest", user_id=f"guest:{login}", allowed=False, login=login,
                             detail="demo access expired")
        if reason == "invalid-until":
            return Principal(kind="guest", user_id=f"guest:{login}", allowed=False, login=login,
                             detail="this tailnet login is not allowed")
        until = None
        if guest.until:
            try:
                until = parse_guest_until(guest.until)
            except ValueError:
                until = None
        return Principal(kind="guest", user_id=f"guest:{login}", allowed=True, login=login, until=until)
    return Principal(kind="owner", user_id=OWNER_USER_ID, allowed=False, login=login,
                     detail="this tailnet login is not allowed")


def principal_from_key(key: dict) -> Principal:
    """App, device, or owner bearer token. Tokens always act in the owner household scope."""
    kind = key.get("kind") or "device"
    if kind not in ("app", "device", "owner"):
        kind = "device"
    scopes = frozenset((key.get("scopes") or "").split())
    if kind == "owner":
        return Principal(kind="owner", user_id=OWNER_USER_ID, allowed=True, key_id=key.get("id") or "",
                         scopes=scopes, display_name=key.get("name") or "")
    return Principal(kind=kind, user_id=OWNER_USER_ID, allowed=True, key_id=key.get("id") or "",
                     scopes=scopes, display_name=key.get("name") or "")


def session_user_id(session: dict | None) -> str:
    if not session:
        return OWNER_USER_ID
    return session.get("owner_id") or OWNER_USER_ID
