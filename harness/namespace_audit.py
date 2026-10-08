"""Private operational audit (#471). Explicit authenticated context, never caller headers/content."""
from __future__ import annotations

import logging
import asyncio
import functools
import inspect
from contextvars import ContextVar

from . import audit_context, operation_audit

log = logging.getLogger(__name__)
gap = ContextVar("namespace_audit_gap", default=None)
FIELDS = frozenset({"prompt", "context", "title", "tools", "metadata", "content", "output", "ok"})
REASONS = frozenset({"manual", "retention", "revoked_app", "account_erasure"})


def key_context(key):
    if key.get("kind") == "member":
        return audit_context.AuditContext(key["user_id"], "member", "" if key.get("bundled") else key.get("id", ""), "app_api")
    return audit_context.AuditContext(key["id"], "app", key["id"], "app_api")


def namespace(session):
    return session.get("app_id") or (session.get("owner_id") if session.get("owner_id") != "owner" else "")


def clean(metadata):
    out = {}
    for key, value in (metadata or {}).items():
        if key == "fields" and isinstance(value, (list, tuple)):
            out[key] = sorted({v for v in value if isinstance(v, str) and v in FIELDS})
        elif key in {"count", "sessions"} and isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**53:
            out[key] = value
        elif key == "decision" and value in ("approved", "denied"):
            out[key] = value
        elif key == "reason" and value in REASONS:
            out[key] = value
        elif key == "subject_trust" and value == "caller_asserted":
            out[key] = value
    return out


def record(db, session, context, action, *, target=None, kind="session", outcome="ok", metadata=None):
    scope = namespace(session)
    if not scope:
        return
    data = clean(metadata)
    # Delegated ids stay solely in this namespace; signing in to a provider does not verify an App's human label.
    if session.get("app_id") and session.get("end_user"):
        data.update(subject=session["end_user"], subject_trust="caller_asserted")
    try:
        store = db.for_app(session.get("app_id") or "")
        def commit():
            if store.get_session(session["id"]) is not None:
                store.insert_namespace_audit(scope, session["id"], context or audit_context.SYSTEM,
                                             target or session["id"], kind, action, outcome, data)
        store.write(commit)
    except Exception:
        warning = gap.get()
        if warning is not None:
            warning.append("audit_gap")
        log.warning("private audit gap: %s", action)


def failures(action):
    """Rejected/interrupting service actions on a known session carry truthful content-free outcomes."""
    def decorate(fn):
        def failure(manager, ref, context, exc):
            session = manager.db.get_session(ref)
            if session:
                record(manager.db, session, context, action,
                       outcome="unknown" if isinstance(exc, asyncio.CancelledError) else "failure")

        @functools.wraps(fn)
        def sync(manager, ref, *args, **kwargs):
            try:
                return fn(manager, ref, *args, **kwargs)
            except Exception as exc:
                failure(manager, ref, kwargs.get("context"), exc)
                raise

        @functools.wraps(fn)
        async def asynchronous(manager, ref, *args, **kwargs):
            try:
                return await fn(manager, ref, *args, **kwargs)
            except (Exception, asyncio.CancelledError) as exc:
                await asyncio.to_thread(failure, manager, ref, kwargs.get("context"), exc)
                raise
        return asynchronous if inspect.iscoroutinefunction(fn) else sync
    return decorate


class LoginStore:
    """Adapt #470's protocol to the private store; none of its rows enter account_audit."""
    def __init__(self, db, app_id, end_user, days=30, backend=""):
        self.db, self.app_id, self.end_user = db, app_id, end_user
        self.days = min(30, days or 30)
        self.backend = backend

    def insert_audit(self, actor, target, action, outcome, *, context, target_kind, metadata):
        data = {"operation_id": metadata["operation_id"], "subject": self.end_user,
                "subject_trust": "caller_asserted"}
        if self.backend in {"claude", "codex"}:
            data["backend"] = self.backend
        self.db.for_app(self.app_id).insert_namespace_audit(
            self.app_id, "", context, target, "login", action, outcome, data, self.days)

    def finish(self, context, operation_id):
        def record_finish(attempt):
            try:
                operation_audit.append(self, context or audit_context.SYSTEM, attempt.attempt_id,
                                       "login.finish", "ok" if attempt.state == "completed" else "failure",
                                       {"operation_id": operation_id})
            except Exception:
                attempt.audit_incomplete = operation_id
                log.warning("login audit settlement unavailable: %s", operation_id)
        return record_finish


class ReceiptStore:
    """Only aggregate erasure evidence enters main. Fresh operation ids never identify erased work."""
    def __init__(self, db, category, reason, count):
        self.db, self.category, self.reason, self.count = db, category, reason, count

    def insert_audit(self, actor, target, action, outcome, *, context, target_kind, metadata):
        self.db.main.insert_audit(actor, target, "namespace.erase", outcome, context=context,
                                  target_kind=self.category,
                                  metadata={"operation_id": metadata["operation_id"], "count": self.count,
                                            "reason": self.reason, "category": self.category})


def login_operation(db, app_id, end_user, context, action, target="", backend=""):
    app = db.main.get_api_key(app_id) or {}
    return operation_audit.async_operation(LoginStore(db, app_id, end_user, app.get("retention_days"), backend),
                                           context or audit_context.SYSTEM, target, action)


def erase_operation(db, scope, context, category, reason, count):
    if reason not in REASONS:
        raise ValueError("invalid erasure reason")
    return operation_audit.async_operation(ReceiptStore(db, category, reason, count),
                                           context or audit_context.SYSTEM, scope, "namespace.erase")


def refuse_unresolved_erase(db, scope):
    # Deliberately block at namespace granularity: retaining a private session id here would violate erasure.
    before = None
    settled = set()
    while True:
        page = db.main.audit_page(target_id=scope, action="namespace.erase", before_id=before, limit=500)
        for row in page["items"]:
            op = row["metadata"].get("operation_id")
            if row["outcome"] == "ok":
                settled.add(op)
            elif row["outcome"] == "started" and op not in settled:
                raise operation_audit._incomplete(op)
        before = page["next_before_id"]
        if before is None:
            return
