"""Content-free owner operations and durable intent for cross-store effects (#470)."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import contextmanager

from .audit_context import AuditContext, access_context, owner_context
from .principal import OWNER_USER_ID, session_user_id

log = logging.getLogger(__name__)
JOB_FIELDS = frozenset({"name", "prompt", "cron", "project", "backend", "model", "notify", "enabled",
                        "catch_up_minutes"})
SESSION_ACTIONS = frozenset({"approval.decide", "approval.auto_decide", "review.merge", "review.push",
                             "review.discard", "session.taint_clear", "checkpoint.rewind", "checkpoint.fork"})
ACTIONS = SESSION_ACTIONS | {"job.create", "job.update", "job.delete", "job.run", "maintenance.cleanup",
                              "maintenance.backup"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def clean_metadata(action, metadata):
    allowed = {"operation_id"}
    if action in SESSION_ACTIONS:
        allowed.add("session_id")
    if action.startswith("approval."):
        allowed.update({"decision", "reviewer_mode"})
    if action.startswith("checkpoint."):
        allowed.update({"turn", "resulting_session_id"})
    if action.startswith("job."):
        allowed.update({"fields", "enabled", "resulting_session_id", "trigger"})
    if action.startswith("maintenance."):
        allowed.update({"trigger", "removed", "kept", "expired"})
    out = {}
    enums = {"decision": {"approved", "denied"}, "reviewer_mode": {"off", "shadow", "auto"},
             "trigger": {"manual", "scheduled"}}
    for key, value in (metadata or {}).items():
        if key not in allowed:
            continue
        if key in {"operation_id", "session_id", "resulting_session_id"}:
            if isinstance(value, str) and _ID.fullmatch(value):
                out[key] = value
        elif key in enums:
            if isinstance(value, str) and value in enums[key]:
                out[key] = value
        elif key == "fields":
            if isinstance(value, (list, tuple)):
                out[key] = sorted({v for v in value if isinstance(v, str) and v in JOB_FIELDS})
        elif key == "enabled":
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**53:
            out[key] = value
    return out


def request_context(request, manager):
    """Only validated bearer keys or middleware identities; never attribution headers."""
    path = request.scope.get("harness_original_path", request.url.path)
    source = "admin_api" if path.startswith("/api/admin/v1") else "legacy_api"
    if request.headers.get("authorization"):
        from .admin import require_admin
        return owner_context(require_admin(request, lambda _: manager), source)
    return access_context(request.state.access, source)


def owner_session(session):
    return bool(session and not session.get("app_id") and session_user_id(session) == OWNER_USER_ID)


def append(db, context, target, action, outcome, metadata=None):
    kind = "approval" if action.startswith("approval.") else "job" if action.startswith("job.") else (
        "operation" if action.startswith("maintenance.") else "session")
    db.insert_audit(context.actor_id, target, action, outcome, context=context, target_kind=kind,
                    metadata=metadata)


def _incomplete(operation_id):
    from .manager import HarnessError
    error = HarnessError(503, "audit record incomplete; action may have completed; inspect before retry",
                         code="audit_record_incomplete")
    error.operation_id = operation_id
    error.may_have_completed = True
    return error


@contextmanager
def operation(db, context, target, action, metadata=None, *, scheduled=False, enabled=True):
    """Commit intent before effects; never retry or undo an effect on terminal audit failure.

    A cancelled coroutine may still have a worker thread in flight: leave intent unresolved.
    Call inside the service's existing operation lock. The mutable metadata holds safe result counts/ids.
    """
    data = dict(metadata or {})
    if not enabled:
        yield data
        return
    context = context or AuditContext("unknown")
    data["operation_id"] = uuid.uuid4().hex
    try:
        append(db, context, target, action, "started", data)
    except Exception:
        from .manager import HarnessError
        raise HarnessError(503, "audit intent unavailable; action was not started", code="audit_unavailable") from None
    try:
        yield data
    except asyncio.CancelledError:
        log.warning("audit operation interrupted: %s", data["operation_id"])
        raise
    except Exception:
        # External effects may have partially succeeded; unknown is deliberately conservative.
        try:
            append(db, context, target, action, "unknown", data)
        except Exception:
            log.warning("audit settlement unavailable: %s", data["operation_id"])
            if not scheduled:
                raise _incomplete(data["operation_id"]) from None
        raise
    else:
        try:
            append(db, context, target, action, "ok", data)
        except Exception:
            log.warning("audit settlement unavailable: %s", data["operation_id"])
            if not scheduled:
                raise _incomplete(data["operation_id"]) from None
