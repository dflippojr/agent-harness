"""Audit rows for credential, pairing and provider-grant lifecycle changes (#468).

Every helper writes one `account_audit` row through `Database.insert_audit`, so inside `db.write`/`awrite` the row
commits or rolls back with the change it describes. A failure to write the row raises, and the change is undone.
Only opaque ids, enums and names go in (see `audit_context.METADATA_ALLOWLIST`), never a secret or caller input.
"""
from __future__ import annotations

import logging
import time

from . import audit_context

log = logging.getLogger("harness.credential_audit")

# An unauthenticated redeem can be tried by anyone who reaches the port: past this many unknown-caller denials of one
# action in an hour, more of them are dropped instead of growing the table without bound.
UNKNOWN_DENIAL_HOURLY_CAP = 200


def request_context(request, m, *, source: str | None = None) -> audit_context.AuditContext:
    """Context for a request that already passed its authentication: a validated owner bearer key, else the
    ambient owner/member identity. `source` defaults from the public path (`admin_api` or `legacy_api`)."""
    path = request.scope.get("harness_original_path", request.url.path)
    src = source or ("admin_api" if path.startswith("/api/admin/") else "legacy_api")
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    key = m.db.api_key_by_secret(token) if token else None
    if key is not None and key.get("kind") == "owner" and "admin" in (key.get("scopes") or "").split():
        return audit_context.owner_context(key, src)
    return audit_context.access_context(getattr(request.state, "access", None), src)


def member_context(user_id: str) -> audit_context.AuditContext:
    return audit_context.AuditContext(user_id, "member", "", "app_api")


def device_context(key_id: str, kind: str, source: str = "app_api") -> audit_context.AuditContext:
    """The new key a successful pairing redemption minted: proof of the bootstrap code, not of a person."""
    return audit_context.AuditContext(key_id, "app" if kind == "app" else "device", key_id, source)


def unknown_context(source: str = "app_api") -> audit_context.AuditContext:
    return audit_context.AuditContext(audit_context.UNKNOWN, audit_context.UNKNOWN, "", source)


def record(db, ctx, action: str, target_id: str, outcome: str, target_kind: str, metadata: dict | None = None,
           detail: str = "") -> None:
    """One audit row. Raises a 503 `audit_unavailable` (rolling back an enclosing write) if it cannot be stored."""
    main = getattr(db, "main", db)
    try:
        if ctx.actor_kind == audit_context.UNKNOWN and outcome == "denied" and _flooded(main, action):
            return
        main.insert_audit(ctx.actor_id, target_id, action, outcome, detail, context=ctx,
                          target_kind=target_kind, metadata=metadata)
    except Exception:
        log.exception("audit write failed for %s", action)
        from .manager import HarnessError  # manager imports this module's users, so import it late
        raise HarnessError(503, "audit record could not be written; the change was not applied",
                           code="audit_unavailable") from None


def pairing_reason(error: str) -> str:
    """The refusal enum for a `redeem_*pairing_code` error string; the string itself is never stored."""
    text = (error or "").lower()
    for needle, reason in (("already used", "code_used"), ("expired", "code_expired"),
                           ("origin", "origin_mismatch"), ("no longer configured", "runner_unavailable")):
        if needle in text:
            return reason
    return "unknown_code"


def _flooded(main, action: str) -> bool:
    return main.unknown_denials_since(action, time.time() - 3600) >= UNKNOWN_DENIAL_HOURLY_CAP
