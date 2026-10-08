"""Who did what, through which door: the immutable context stamped on `account_audit` rows (#467).

A context is built only after authentication succeeded, from server-observed facts. It never reads a
caller-supplied actor or source header, and a credential that failed validation never becomes an identity: such
callers are `unknown` and the supplied token is neither stored nor hashed.
"""
from __future__ import annotations

import json
import re
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

# Credential, pairing and provider-grant lifecycle (#468). Metadata is opaque ids, enums, booleans, scope and field
# names: never a secret, code, origin, URL, secret reference, path or model list.
CREDENTIAL_REASONS = frozenset({"invalid_request", "not_found", "already_revoked", "unknown_code", "code_used",
                                "code_expired", "origin_mismatch", "runner_unavailable", "unsupported_backend",
                                "invalid_key", "no_key", "grace_over", "audit_unavailable"})
KEY_KINDS = frozenset({"owner", "app", "device", "web", "member"})
POLICIES = frozenset({"subscription", "api_key", "subscription_then_api_key"})
MEMBER_KEY_OUTCOMES = frozenset({"ok", "rejected", "unavailable", "noop"})
GRANT_FIELDS = frozenset({"backend", "policy", "models", "secret_ref"})
_CRED_ID_KEYS = ("key_id", "pairing_id", "app_id", "grant_id", "previous_grant_id")
_CRED_BOOL_KEYS = ("configured", "replaced")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAME = re.compile(r"[a-z][a-z0-9_:.-]{0,39}")
_BACKEND = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CRED_COMMON = frozenset({*_CRED_ID_KEYS, "reason"})
METADATA_ALLOWLIST.update({
    "key.create": _CRED_COMMON | {"kind", "scopes"},
    "key.revoke": _CRED_COMMON | {"kind"},
    "pairing.create": _CRED_COMMON | {"scopes"},
    "pairing.revoke": _CRED_COMMON,
    "pairing.redeem": _CRED_COMMON | {"kind", "scopes"},
    "runner_pairing.create": _CRED_COMMON,
    "runner_pairing.revoke": _CRED_COMMON,
    "runner_pairing.redeem": _CRED_COMMON | {"kind"},
    "app.restore": _CRED_COMMON | {"kind"},
    "app.retention": _CRED_COMMON | {"old_retention_days", "new_retention_days"},
    "provider_grant.set": _CRED_COMMON | {"backend", "policy", "fields", "replaced"},
    "provider_grant.revoke": _CRED_COMMON | {"backend", "policy"},
    "member_key.set": _CRED_COMMON | {"backend", "configured", "replaced", "result"},
    "member_key.delete": _CRED_COMMON | {"backend", "configured", "result"},
    "member_key.test": _CRED_COMMON | {"backend", "configured", "result"},
})


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


def _clean_credential(key: str, value, out: dict) -> bool:
    """Handle the #468 metadata keys; returns False for a key that belongs to the account actions."""
    if key in _CRED_ID_KEYS:
        if isinstance(value, str) and _ID.fullmatch(value):
            out[key] = value
    elif key == "reason":
        if value in CREDENTIAL_REASONS:
            out[key] = value
    elif key == "kind":
        if value in KEY_KINDS:
            out[key] = value
    elif key == "policy":
        if value in POLICIES:
            out[key] = value
    elif key == "result":
        if value in MEMBER_KEY_OUTCOMES:
            out[key] = value
    elif key == "backend":
        if isinstance(value, str) and _BACKEND.fullmatch(value):
            out[key] = value
    elif key in _CRED_BOOL_KEYS:
        if isinstance(value, bool):
            out[key] = value
    elif key == "scopes":
        if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            names = sorted({v for v in value if _NAME.fullmatch(v)})[:20]
            if names:
                out[key] = names
    elif key in ("old_retention_days", "new_retention_days"):
        if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)
                             and 0 < value <= 36500):
            out[key] = value
    else:
        return False
    return True


def clean_metadata(action: str, metadata: dict | None) -> dict:
    """Keep only allowlisted, safely-typed values for `action`; drop everything else without storing it."""
    from .operation_audit import ACTIONS, clean_metadata as clean_operation
    if action in ACTIONS:
        return clean_operation(action, metadata)
    allowed = METADATA_ALLOWLIST.get(action, frozenset())
    out: dict = {}
    credential = "." in action
    for key, value in (metadata or {}).items():
        if key not in allowed:
            continue
        if credential:
            if key == "fields":
                if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                    names = sorted({v for v in value if v in GRANT_FIELDS})
                    if names:
                        out[key] = names
            else:
                _clean_credential(key, value, out)
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
