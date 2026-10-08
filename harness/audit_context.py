"""Who did what, through which door: the immutable context stamped on `account_audit` rows (#467).

A context is built only after authentication succeeded, from server-observed facts. It never reads a
caller-supplied actor or source header, and a credential that failed validation never becomes an identity: such
callers are `unknown` and the supplied token is neither stored nor hashed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .principal import OWNER_USER_ID

SOURCES = frozenset({"admin_api", "legacy_api", "app_api", "notification_link", "job", "maintenance", "agent",
                     "local_cli", "system", "unknown"})
UNKNOWN = "unknown"
MAX_METADATA_BYTES = 1024

ACCOUNT_FIELDS = ("display_name", "login", "enabled", "disk_quota_bytes", "max_running", "max_queued")
DENIAL_REASONS = frozenset({"login_is_owner", "login_is_guest", "login_is_google_device", "login_already_member",
                            "login_occupies_role"})
_INT_KEYS = ("old_disk_quota_bytes", "new_disk_quota_bytes", "old_max_running", "new_max_running",
             "old_max_queued", "new_max_queued")
_BOOL_KEYS = ("old_enabled", "new_enabled")
_LIMITS = _INT_KEYS
# Per action: the only metadata keys that may be stored. Everything else is dropped, not stored.
METADATA_ALLOWLIST: dict[str, frozenset[str]] = {
    "create": frozenset({"fields", "new_disk_quota_bytes", "new_max_running", "new_max_queued", "reason"}),
    "rename": frozenset({"fields"}),
    "rebind": frozenset({"fields", "reason"}),
    "enable": frozenset({"fields", "old_enabled", "new_enabled"}),
    "disable": frozenset({"fields", "old_enabled", "new_enabled"}),
    "quota": frozenset({"fields", "old_disk_quota_bytes", "new_disk_quota_bytes"}),
    "concurrency": frozenset({"fields", *(k for k in _LIMITS if "max_" in k)}),
}


@dataclass(frozen=True)
class AuditContext:
    actor_id: str
    actor_kind: str = UNKNOWN
    key_id: str = ""
    source: str = UNKNOWN

    def __post_init__(self):
        if self.source not in SOURCES:
            object.__setattr__(self, "source", UNKNOWN)


SYSTEM = AuditContext("system", "system", "", "system")


def owner_context(key: dict | None, source: str = "admin_api") -> AuditContext:
    """The owner behind an authenticated admin request: a validated owner bearer key (which wins over any
    ambient identity) or the ambient localhost/Tailscale owner. `key` is the row `require_admin` returned."""
    if key is not None:
        return AuditContext(OWNER_USER_ID, "owner_key", str(key.get("id") or ""), source)
    return AuditContext(OWNER_USER_ID, "owner", "", source)


def access_context(access, source: str) -> AuditContext:
    """An ambient human identity (`Access`) for routes that are not token-authenticated."""
    if access is None or not getattr(access, "allowed", False) or not getattr(access, "user_id", ""):
        return AuditContext(UNKNOWN, UNKNOWN, "", source)
    kind = "owner" if access.role == "owner" else "member" if access.role == "member" else UNKNOWN
    return AuditContext(access.user_id, kind, "", source)


def coerce(actor) -> AuditContext:
    """Accept a context or a bare actor id (legacy callers); a bare id carries no further attribution."""
    if isinstance(actor, AuditContext):
        return actor
    return AuditContext(str(actor or UNKNOWN))


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) < 2 ** 53


def clean_metadata(action: str, metadata: dict | None) -> dict:
    """Keep only allowlisted, safely-typed values for `action`; drop everything else without storing it."""
    allowed = METADATA_ALLOWLIST.get(action, frozenset())
    out: dict = {}
    for key, value in (metadata or {}).items():
        if key not in allowed:
            continue
        if key == "fields":
            if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                names = sorted({v for v in value if v in ACCOUNT_FIELDS})
                if names:
                    out[key] = names
        elif key == "reason":
            if value in DENIAL_REASONS:
                out[key] = value
        elif key in _BOOL_KEYS:
            if isinstance(value, bool):
                out[key] = value
        elif key in _INT_KEYS:
            if _int(value):
                out[key] = value
    return out


def dump_metadata(action: str, metadata: dict | None) -> str:
    text = json.dumps(clean_metadata(action, metadata), sort_keys=True, separators=(",", ":"))
    return text if len(text) <= MAX_METADATA_BYTES else "{}"
