"""Content-free owner operations and durable intent for cross-store effects (#470)."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager, contextmanager

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
ENUMS = {"decision": {"approved", "denied"}, "reviewer_mode": {"off", "shadow", "auto"},
         "trigger": {"manual", "scheduled"}, "reason": {"already_decided"}}


def _clean_value(key, value):
    if key in {"operation_id", "session_id", "resulting_session_id"}:
        return value if isinstance(value, str) and _ID.fullmatch(value) else None
    if key in ENUMS:
        return value if isinstance(value, str) and value in ENUMS[key] else None
    if key == "fields":
        if isinstance(value, (list, tuple)):
            return sorted({v for v in value if isinstance(v, str) and v in JOB_FIELDS})
        return None
    if key == "enabled":
        return value if isinstance(value, bool) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**53 else None


def clean_metadata(action, metadata):
    allowed = {"operation_id"}
    if action in SESSION_ACTIONS:
        allowed.add("session_id")
    if action.startswith("approval."):
        allowed.update({"decision", "reviewer_mode", "reason"})
    if action.startswith("checkpoint."):
        allowed.update({"turn", "resulting_session_id"})
    if action.startswith("job."):
        allowed.update({"fields", "enabled", "resulting_session_id", "trigger"})
    if action.startswith("maintenance."):
        allowed.update({"trigger", "removed", "kept", "expired"})
    out = {}
    for key, value in (metadata or {}).items():
        if key not in allowed:
            continue
        cleaned = _clean_value(key, value)
        if cleaned is not None:
            out[key] = cleaned
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
    kind = {"approval": "approval", "job": "job", "maintenance": "operation"}.get(action.split(".")[0], "session")
    data = dict(metadata or {})
    data.setdefault("operation_id", uuid.uuid4().hex)
    db.insert_audit(context.actor_id, target, action, outcome, context=context, target_kind=kind,
                    metadata=data)


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


@asynccontextmanager
async def async_operation(*args, **kwargs):
    """The same protocol without blocking the event loop on the main-store writer.

    Finish queued audit commits on cancellation; an interrupted action itself stays unresolved.
    """
    from .db import finish_then_cancel
    scope = operation(*args, **kwargs)
    data = await finish_then_cancel(asyncio.to_thread(scope.__enter__))
    try:
        yield data
    except BaseException as exc:
        await finish_then_cancel(asyncio.to_thread(scope.__exit__, type(exc), exc, exc.__traceback__))
        raise
    else:
        await finish_then_cancel(asyncio.to_thread(scope.__exit__, None, None, None))
