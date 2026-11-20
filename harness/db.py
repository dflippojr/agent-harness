"""SQLite persistence. Every state change is committed before the daemon acts on it, so a restart can resume.

Threading (#294): one writer thread owns the read-write connection and drains a queue of writes; reads use a small
pool of read-only WAL connections. The sync methods keep working from any thread (a write blocks its caller until
the writer has committed it). Event-loop code awaits instead: `await db.aio.<method>(...)` for one method, and
`await db.awrite(fn)` for a multi-statement transaction (`fn` runs whole on the writer thread, so it cannot await).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import queue
import sqlite3
import threading
import time
import weakref
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence, cast

from . import migrations as migrations_mod
from .digests import sha256_hex
from .migrations.baseline import BASELINE_VERSION, LEGACY_COLUMNS

from .telemetry import TimedLock

if TYPE_CHECKING:
    from .credential_store import CredentialStore
    from .session_store import SessionStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    target TEXT NOT NULL,
    model TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    stop_reason TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    context TEXT NOT NULL,          -- JSON: messages sent to the model (rewritten by compaction)
    run TEXT NOT NULL DEFAULT '{}', -- JSON: counters for the current run
    totals TEXT NOT NULL DEFAULT '{}',
    inbox TEXT NOT NULL DEFAULT '[]' -- JSON: user messages sent while a run was active
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
    session_id TEXT NOT NULL,
    hash TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (session_id, hash)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,           -- pending | approved | denied | cancelled
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS approvals_session ON approvals(session_id, status);
CREATE TABLE IF NOT EXISTS api_keys (   -- inference endpoint keys, one per device or app (endpoint.py)
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,           -- first characters, shown to tell keys apart
    hash TEXT NOT NULL UNIQUE,      -- sha256 of the key; the key itself is shown once and never stored
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS pairing_codes ( -- owner-approved, short-lived browser bootstrap codes
    id TEXT PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    origin TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    key_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stream_tickets ( -- short-lived credentials for native EventSource
    hash TEXT PRIMARY KEY,
    key_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS stream_tickets_expiry ON stream_tickets(expires_at);
CREATE TABLE IF NOT EXISTS endpoint_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    route TEXT NOT NULL,
    model TEXT NOT NULL,
    stream INTEGER NOT NULL,
    status INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    wait_ms INTEGER NOT NULL DEFAULT 0,       -- time spent waiting for the GPU
    total_ms INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS endpoint_requests_ts ON endpoint_requests(ts);
CREATE TABLE IF NOT EXISTS app_tool_calls (    -- agent calls to app-registered tools (apps.py)
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending | done | expired
    output TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY (session_id, call_id)
);
CREATE TABLE IF NOT EXISTS images (    -- image generation jobs (images.py)
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,           -- phone | agent
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    aspect_ratio TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,           -- queued | running | done | failed
    error TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (       -- scheduled jobs (jobs.py)
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cron TEXT NOT NULL,             -- five fields, tower local time
    project TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    notify TEXT NOT NULL DEFAULT 'low',   -- OK results: attention (none) | low | always
    enabled INTEGER NOT NULL DEFAULT 1,
    catch_up_minutes INTEGER NOT NULL DEFAULT 360,
    next_run_at REAL,
    last_run_at REAL,
    last_session_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    last_skip TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS backend_usage (
    backend TEXT PRIMARY KEY,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend TEXT NOT NULL,
    session_id TEXT NOT NULL,
    app_id TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 1,
    billing TEXT NOT NULL DEFAULT 'subscription',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_backend_time ON usage(backend, created_at);
CREATE TABLE IF NOT EXISTS app_provider_credentials (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    secret_ref TEXT NOT NULL DEFAULT '', -- opaque key into local config; never a path or credential value
    policy TEXT NOT NULL,                -- subscription | api_key | subscription_then_api_key
    models TEXT NOT NULL DEFAULT '[]',   -- empty allows every configured model for this backend
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS runner_pairing_codes ( -- owner-approved Agent Harness for Mac bootstrap codes
    id TEXT PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    runner TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    key_id TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS app_provider_credentials_active
ON app_provider_credentials(app_id, backend) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS smart_reviews (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    approval_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    policy_fingerprint TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    confidence REAL,
    risk_flags TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    escalate_reason TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS smart_reviews_created ON smart_reviews(created_at);
CREATE TABLE IF NOT EXISTS skill_proposals (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL,
    activation_suggestion TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    source_session_id TEXT NOT NULL DEFAULT '',
    skill_md TEXT NOT NULL,
    "references" TEXT NOT NULL DEFAULT '[]',
    examples TEXT NOT NULL DEFAULT '[]',
    manifest TEXT NOT NULL DEFAULT '{}',
    static_findings TEXT NOT NULL DEFAULT '[]',
    review TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT '',
    target_slug TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_proposals_hash ON skill_proposals(content_hash);
CREATE INDEX IF NOT EXISTS skill_proposals_slug ON skill_proposals(slug, created_at);
CREATE TABLE IF NOT EXISTS skill_installed (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL,
    current_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    installed_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_versions (
    slug TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    skill_md TEXT NOT NULL,
    "references" TEXT NOT NULL DEFAULT '[]',
    examples TEXT NOT NULL DEFAULT '[]',
    manifest TEXT NOT NULL DEFAULT '{}',
    installed_at REAL NOT NULL,
    PRIMARY KEY (slug, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS skill_versions_hash ON skill_versions(content_hash);
CREATE TABLE IF NOT EXISTS skill_project_allowlist (
    project TEXT NOT NULL,
    slug TEXT NOT NULL,
    PRIMARY KEY (project, slug)
);
CREATE TABLE IF NOT EXISTS skill_rejected (
    content_hash TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    rejected_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_review_jobs (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    findings TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS skill_review_jobs_status ON skill_review_jobs(status, created_at);
-- Session search (search.py): one row per indexed event.
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    text, session_id UNINDEXED, seq UNINDEXED, kind UNINDEXED, ts UNINDEXED, user_id UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    login TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    disk_quota_bytes INTEGER NOT NULL,
    max_running INTEGER NOT NULL DEFAULT 1,
    max_queued INTEGER NOT NULL DEFAULT 2,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_activity_at REAL
);
CREATE TABLE IF NOT EXISTS account_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS account_audit_ts ON account_audit(ts);
CREATE TABLE IF NOT EXISTS review_comments (   -- draft diff line comments, sent as one follow-up (review_comments.py)
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    side TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    quoted TEXT NOT NULL,
    comment TEXT NOT NULL,
    base TEXT NOT NULL DEFAULT '',
    head TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS review_comments_session ON review_comments(session_id, created_at);
CREATE TABLE IF NOT EXISTS secret_dismissals (   -- owner-dismissed secret-scan findings (secret_scan.py, #263)
    session_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    rule TEXT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS member_projects (
    user_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, slug)
);
CREATE INDEX IF NOT EXISTS member_projects_user ON member_projects(user_id);
-- Issue #63: non-secret GitHub connection state per household member. Never a token, code, or URL.
CREATE TABLE IF NOT EXISTS github_connections (
    user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'disconnected',
    namespace_version INTEGER NOT NULL DEFAULT 1,
    connected_at REAL,
    last_used_at REAL,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
-- Issue #64: a household member's linked Google identity. `sub` is the identity key; email is display metadata.
CREATE TABLE IF NOT EXISTS google_identities (
    user_id TEXT PRIMARY KEY,
    sub TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL DEFAULT '',
    linked_at REAL NOT NULL,
    last_sign_in_at REAL
);
-- At most one pending owner-created link invitation per member. Only the code's SHA-256 is stored.
CREATE TABLE IF NOT EXISTS google_link_invitations (
    user_id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
-- Stables member sessions. Only the cookie value's SHA-256 is stored.
CREATE TABLE IF NOT EXISTS web_sessions (
    id_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS web_sessions_user ON web_sessions(user_id);
"""


AND_OWNER = " AND owner_id = ?"
AND_APP = " AND app_id = ?"
AND_KIND = " AND kind = ?"
AND_JOIN = " AND "


def merge_newest_first(*lists: list[dict]) -> list[dict]:
    """Session rows of several lists once each, newest first by (created_at, id): the order a page cursor follows."""
    seen: dict[str, dict] = {}
    for rows in lists:
        for r in rows:
            seen.setdefault(r["id"], r)
    return sorted(seen.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True)

APP_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    app_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

JSON_COLUMNS = {"context", "run", "totals", "inbox", "args", "app_tools", "app_metadata", "app_defaults", "data",
                "origins", "models", "smart", "risk_flags", "skills", "provenance", "taint"}


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        val = out[key]
        if isinstance(val, str) and val[:1] in "{[":
            try:
                out[key] = json.loads(val)
            except json.JSONDecodeError:
                pass
    if "data" in out and isinstance(out["data"], str) and out["data"][:1] in "{[":
        try:
            out["data"] = json.loads(out["data"])
        except json.JSONDecodeError:
            pass
    return out


def _rows(rows) -> list[dict]:
    """Each fetched row as `_row` decodes it (a fetched row is never None)."""
    return [cast(dict, _row(r)) for r in rows]


READ_CONNECTIONS = 4
# A revoked App's (or device's) store and folder are erased this long after the revoke, unless the owner undoes it
# first (#330 decision 5). The nightly maintenance does the erasing (maintenance.py).
APP_ERASE_GRACE_SECONDS = 7 * 86400
APP_LIMITS_META = "app_limits:"           # meta key prefix: an App's owner-set session caps (#524)
APP_LIMIT_KEYS = ("max_running", "max_queued")


def _writes(fn):
    """A method that writes: it runs on the writer thread as one transaction (joining the caller's when inside a
    `write`), so a pooled reader never sees it half-done."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        return self.write(fn, self, *args, **kwargs)
    wrapper.db_kind = "write"
    return wrapper


def _reads(fn):
    """A method that only reads: it runs on a pooled read-only connection (the writer's own inside a write)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if getattr(self._local, "conn", None) is not None:
            return fn(self, *args, **kwargs)
        with self.reading():
            return fn(self, *args, **kwargs)
    wrapper.db_kind = "read"
    return wrapper


async def finish_then_cancel(awaitable):
    """Await `awaitable` to the end even if the awaiting task is cancelled meanwhile, then raise that cancel (its
    own error, if it failed, wins)."""
    future = asyncio.ensure_future(awaitable)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.cancelled():
                raise
            cancelled = True
    result = future.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _run_callbacks(callbacks: list) -> None:
    for cb in callbacks:
        cb()


class _Writer(threading.Thread):
    """Runs queued jobs one at a time on the read-write connection. Holds no reference to the Database, so an
    unclosed Database can still be collected (its finalizer stops this thread)."""

    def __init__(self, conn: sqlite3.Connection, lock: TimedLock, local: threading.local):
        super().__init__(name="harness-db-writer", daemon=True)
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._conn, self._lock, self._local = conn, lock, local

    def run(self) -> None:
        self._local.conn, self._local.lock = self._conn, self._lock
        self._local.after_commit = self._local.after_rollback = None
        while True:
            job = self.jobs.get()
            if job is None:
                return
            future, fn, args, kwargs = job
            del job
            if future.set_running_or_notify_cancel():
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:  # handed to the waiting caller
                    future.set_exception(exc)
                else:
                    future.set_result(result)
                    del result
            del future, fn, args, kwargs  # an idle writer keeps nothing alive

    def stop(self) -> None:
        if self.is_alive():
            self.jobs.put(None)
            if threading.current_thread() is not self:
                self.join(timeout=10)


class AsyncDatabase:
    """`await db.aio.<method>(...)`: any Database method without blocking the event loop. A write goes straight to
    the writer queue; a read (or a composite method) runs in a worker thread on a pooled read connection."""

    def __init__(self, db: Database | _Store):
        self._db = db

    def __getattr__(self, name: str):
        db = self._db
        forwarded = inspect.getattr_static(type(db), name, None)
        if isinstance(forwarded, _Forwarded):  # the store's own method, on the store (#992 stage 5)
            return getattr(getattr(db, forwarded.store).aio, name)
        method = getattr(type(db), name)
        if getattr(method, "db_kind", None) == "write":
            inner = method.__wrapped__

            async def write(*args, **kwargs):
                return await db.awrite(inner, db, *args, **kwargs)
            return write
        bound = getattr(db, name)

        async def read(*args, **kwargs):
            return await asyncio.to_thread(bound, *args, **kwargs)
        return read


class _Store:
    """Queries on a Database's connection machinery (#992): its writer queue, pooled readers and transactions. A
    subclass decorates each query method with `_reads` or `_writes` (`harness.modules.database_reads` or
    `database_writes` in a module), as `Database` does its own.

    It wraps the main Database (a `SessionStores`' `main`), so a store's write joins the caller's transaction, and it
    exposes only what those decorators and `aio` need. It forwards nothing else: a core method is called on the
    Database itself (`core` inside a store method), never through the store."""

    def __init__(self, db):
        main = getattr(db, "main", None)  # a SessionStores' main Database, or `db` itself
        self._db: Database = main if isinstance(main, Database) else db
        self.aio = AsyncDatabase(self)

    # `conn`, `lock` and `_local` are per thread (the writer's, or a pooled reader's inside `reading()`): looked up
    # on every access, never kept.
    @property
    def _local(self) -> threading.local:
        return self._db._local

    @property
    def conn(self) -> sqlite3.Connection:
        return self._db.conn

    @property
    def lock(self) -> TimedLock:
        return self._db.lock

    @property
    def core(self) -> Database:
        """The main Database, for a store method that also calls a public core method in its transaction."""
        return self._db

    def reading(self):
        return self._db.reading()

    def read(self, fn, *args, **kwargs):
        return self._db.read(fn, *args, **kwargs)

    def write(self, fn, *args, **kwargs):
        return self._db.write(fn, *args, **kwargs)

    async def awrite(self, fn, *args, **kwargs):
        return await self._db.awrite(fn, *args, **kwargs)

    def in_transaction(self) -> bool:
        return self._db.in_transaction()

    def after_commit(self, callback) -> None:
        self._db.after_commit(callback)


class ModuleStore(_Store):
    """A module's own queries on the core's shared database (#992): subclass it and decorate each query method with
    `harness.modules.database_reads` or `database_writes`. It forwards nothing to the Database."""


class CoreStore(_Store):
    """One domain of the core's own queries (#992 stage 5), such as `CredentialStore`: the Database builds it and
    exposes it as an attribute (`db.credentials`). A method that writes in several statements opens `_tx()`, which
    inside a `_writes` method joins that method's transaction."""

    def _tx(self, label: str | None = None):
        return self._db._tx(label)


def _store_class(attribute: str) -> type[CoreStore]:
    """The class of the core store a Database exposes as `attribute`. Imported on first use: each store module
    imports this one."""
    from .credential_store import CredentialStore
    from .session_store import SessionStore

    return {"credentials": CredentialStore, "sessions": SessionStore}[attribute]


class _Forwarded:
    """One name of the temporary forwarding shim (#992 stage 5): `db.<name>` is `db.<store>.<name>`, so call sites
    keep working while they move to the store. On the class, it is the store's method, whose `db_kind` routes a call
    (`app_stores`); it has no `__set__`, so an instance attribute of that name (a test's patch) wins.
    `tests/test_database_shim.py` pins the forwarded names: the set may only shrink."""

    __slots__ = ("store", "name")

    def __init__(self, store: str):
        self.store = store
        self.name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, db: object, owner: type | None = None) -> Any:
        if db is None:
            return getattr(_store_class(self.store), self.name)
        return getattr(getattr(db, self.store), self.name)


class Database:
    # The temporary forwarding shim (#992 stage 5): these `db.<name>` calls run on a core store (`db.credentials`,
    # `db.sessions`) until their call sites move there. tests/test_database_shim.py pins the names; remove one once
    # nothing calls it here, and never add one.
    # credentials: API keys
    create_api_key = _Forwarded("credentials")
    api_key_by_secret = _Forwarded("credentials")
    get_api_key = _Forwarded("credentials")
    touch_api_key = _Forwarded("credentials")
    list_api_keys = _Forwarded("credentials")
    get_api_key_listing = _Forwarded("credentials")
    ensure_web_app = _Forwarded("credentials")
    revoke_api_key = _Forwarded("credentials")
    restore_api_key = _Forwarded("credentials")
    origin_allowed = _Forwarded("credentials")
    # pairing codes
    create_pairing_code = _Forwarded("credentials")
    list_pairing_codes = _Forwarded("credentials")
    revoke_pairing_code = _Forwarded("credentials")
    pairing_id_for_key = _Forwarded("credentials")
    pairing_origin_active = _Forwarded("credentials")
    redeem_pairing_code = _Forwarded("credentials")
    # pairing requests and the Hub claim
    insert_pairing_request = _Forwarded("credentials")
    get_pairing_request = _Forwarded("credentials")
    list_pairing_requests = _Forwarded("credentials")
    open_pairing_match_codes = _Forwarded("credentials")
    pending_pairing_catalog_origins = _Forwarded("credentials")
    pairing_request_load = _Forwarded("credentials")
    update_pairing_request = _Forwarded("credentials")
    expire_pairing_requests = _Forwarded("credentials")
    pairing_request_origin_known = _Forwarded("credentials")
    redeem_pairing_request = _Forwarded("credentials")
    hub_claim = _Forwarded("credentials")
    redeem_hub_claim = _Forwarded("credentials")
    release_hub = _Forwarded("credentials")
    hub_minted_owner_keys = _Forwarded("credentials")
    # stream tickets
    create_stream_ticket = _Forwarded("credentials")
    stream_ticket_key = _Forwarded("credentials")
    stream_ticket_origin_active = _Forwarded("credentials")
    # App provider credentials
    set_app_provider_credential = _Forwarded("credentials")
    app_provider_credential = _Forwarded("credentials")
    app_provider_credential_by_id = _Forwarded("credentials")
    app_provider_managed = _Forwarded("credentials")
    list_app_provider_credentials = _Forwarded("credentials")
    revoke_app_provider_credential = _Forwarded("credentials")
    # sessions
    insert_session = _Forwarded("sessions")
    update_session = _Forwarded("sessions")
    patch_run = _Forwarded("sessions")
    unset_run = _Forwarded("sessions")
    get_session = _Forwarded("sessions")
    get_session_for_user = _Forwarded("sessions")
    find_session_ids = _Forwarded("sessions")
    list_sessions = _Forwarded("sessions")
    session_page = _Forwarded("sessions")
    active_sessions = _Forwarded("sessions")
    group_sessions = _Forwarded("sessions")
    sessions_with_status = _Forwarded("sessions")
    sessions_with_run_flag = _Forwarded("sessions")
    count_sessions = _Forwarded("sessions")
    count_app_sessions = _Forwarded("sessions")
    count_stored_sessions = _Forwarded("sessions")
    count_stored_app_sessions = _Forwarded("sessions")
    delete_session = _Forwarded("sessions")
    job_sessions = _Forwarded("sessions")
    session_activity = _Forwarded("sessions")
    # sessions: checkpoints and artifacts
    add_checkpoint = _Forwarded("sessions")
    checkpoints = _Forwarded("sessions")
    show_checkpoints_through = _Forwarded("sessions")
    delete_checkpoints = _Forwarded("sessions")
    put_artifact = _Forwarded("sessions")
    read_artifact = _Forwarded("sessions")
    full_artifact = _Forwarded("sessions")
    # sessions: review comments and secret dismissals
    add_review_comment = _Forwarded("sessions")
    list_review_comments = _Forwarded("sessions")
    add_secret_dismissal = _Forwarded("sessions")
    secret_dismissals = _Forwarded("sessions")
    delete_review_comments = _Forwarded("sessions")
    # sessions: events and search
    insert_event = _Forwarded("sessions")
    search_events = _Forwarded("sessions")
    session_brief = _Forwarded("sessions")
    events = _Forwarded("sessions")
    has_tool_result = _Forwarded("sessions")
    pushed_heads = _Forwarded("sessions")
    snippet_events = _Forwarded("sessions")
    session_card_bits = _Forwarded("sessions")
    last_event_seq = _Forwarded("sessions")
    # sessions: approvals and smart reviews
    insert_approval = _Forwarded("sessions")
    approval_by_token = _Forwarded("sessions")
    get_approval = _Forwarded("sessions")
    approval_for_call = _Forwarded("sessions")
    pending_approvals = _Forwarded("sessions")
    approvals = _Forwarded("sessions")
    decide_approval = _Forwarded("sessions")
    insert_smart_review = _Forwarded("sessions")
    smart_review_stats = _Forwarded("sessions")
    smart_reviews = _Forwarded("sessions")

    def __init__(self, path: Path, migrations: Sequence[migrations_mod.Step] | None = None):
        """Open (creating if needed) and migrate the database. `migrations` overrides the discovered steps."""
        steps = migrations_mod.discover() if migrations is None else migrations_mod.validate(migrations)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._closed = False
        self._wconn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        try:
            # Refuse a database from a newer harness before anything writes to it.
            version = migrations_mod.user_version(self.conn)
            migrations_mod.check_not_too_new(version, steps)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            # A pre-versioning file that already holds tables has real data: after the add-only bootstrap it
            # still gets a snapshot before any numbered step. Only a fresh, empty database skips the backup.
            had_tables = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if version < BASELINE_VERSION:
                self._bootstrap()
            backup = version >= BASELINE_VERSION or had_tables is not None
            migrations_mod.apply_pending(self.conn, path, steps, backup=backup)
        except BaseException:
            self._wconn.close()
            raise
        self._ensure_search_index_columns()
        self.path = path
        self._wlock = TimedLock()
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._readers: list[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        self._writer = _Writer(self._wconn, self._wlock, self._local)
        self._writer.start()
        self._finalizer = weakref.finalize(self, self._writer.stop)
        self.aio = AsyncDatabase(self)
        from .credential_store import CredentialStore  # each store module imports this one
        from .session_store import SessionStore

        self.credentials: CredentialStore = CredentialStore(self)
        self.sessions: SessionStore = SessionStore(self)
        self.sessions.build_search_index()

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection: the writer's on the writer thread, a pooled reader inside `reading()`.
        Anywhere else the read-write connection itself, which only startup code and tests should touch."""
        return getattr(self._local, "conn", None) or self._wconn

    @property
    def lock(self) -> TimedLock:
        """Times (and serializes) the work on `conn`: one lock per connection."""
        return getattr(self._local, "lock", None) or self._wlock

    def _on_writer(self) -> bool:
        return getattr(self._local, "conn", None) is self._wconn

    def for_session(self, sid: str) -> Database:
        """The store that holds session `sid`. A lone Database holds them all; `SessionStores` routes (#330)."""
        return self

    def for_app(self, app_id: str) -> Database:
        """The store that holds App `app_id`'s sessions: this one, unless `SessionStores` routes (#330)."""
        return self

    def _submit(self, fn, *args, **kwargs) -> Future:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        future: Future = Future()
        # Request-local audit warnings must reach writer transactions without leaking to the next queued request.
        context = contextvars.copy_context()
        self._writer.jobs.put((future, context.run, (fn, *args), kwargs))
        return future

    def _open_reader(self) -> tuple[sqlite3.Connection, TimedLock]:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn, TimedLock()

    @contextmanager
    def reading(self):
        """Check out a read-only WAL connection for this thread; `conn` and `lock` refer to it inside the block."""
        local = self._local
        if getattr(local, "conn", None) is not None:  # already reading, or on the writer
            yield local.conn
            return
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        try:
            conn, lock = self._idle.get_nowait()
        except queue.Empty:
            with self._pool_lock:
                create = len(self._readers) < READ_CONNECTIONS
                if create:
                    conn, lock = self._open_reader()
                    self._readers.append(conn)
            if not create:
                conn, lock = self._idle.get()
        local.conn, local.lock = conn, lock
        try:
            yield conn
        finally:
            local.conn = local.lock = None
            self._idle.put((conn, lock))

    def read(self, fn, *args, **kwargs):
        """Run fn(*args, **kwargs) with a read-only connection checked out for this thread."""
        with self.reading():
            return fn(*args, **kwargs)

    async def aread(self, fn, *args, **kwargs):
        """`read` in a worker thread, for the event loop."""
        return await asyncio.to_thread(self.read, fn, *args, **kwargs)

    def in_transaction(self) -> bool:
        """True inside a `write`/`awrite` callable (which runs on the writer thread)."""
        return self._on_writer() and self._local.after_commit is not None

    def after_commit(self, callback) -> None:
        """Run callback() in the caller's thread once the current transaction commits; at once outside one."""
        if self.in_transaction():
            self._local.after_commit.append(callback)
        else:
            callback()

    def after_rollback(self, callback) -> None:
        """Run callback() on the writer thread if the current transaction rolls back; nothing outside one."""
        if self.in_transaction():
            self._local.after_rollback.append(callback)

    def _run_tx(self, fn, args, kwargs):
        callbacks: list = []
        undo: list = []
        self._local.after_commit, self._local.after_rollback = callbacks, undo
        try:
            with self._tx(label=getattr(fn, "__name__", None)):
                value = fn(*args, **kwargs)
        except BaseException:
            _run_callbacks(undo)
            raise
        finally:
            self._local.after_commit = self._local.after_rollback = None
        return value, callbacks

    def write(self, fn, *args, **kwargs):
        """Run fn(*args, **kwargs) as one transaction on the writer thread and return its result once committed.
        `fn` must not wait on the event loop. Blocks the calling thread: event-loop code uses `awrite`."""
        if self.in_transaction():  # nested: part of the outer transaction
            return fn(*args, **kwargs)
        if self._on_writer():
            value, callbacks = self._run_tx(fn, args, kwargs)
        else:
            value, callbacks = self._submit(self._run_tx, fn, args, kwargs).result()
        _run_callbacks(callbacks)
        return value

    async def awrite(self, fn, *args, **kwargs):
        """`write` for the event loop: the loop stays free until the transaction has committed. Once submitted, the
        write commits whether or not its awaiter is cancelled, so its after_commit callbacks run on the loop
        regardless, exactly once, and a cancel reaches the awaiter only after both (as it would a blocking write)."""
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        done = loop.create_future()

        def deliver(job: Future) -> None:  # on the loop
            try:
                value, callbacks = job.result()
                _run_callbacks(callbacks)
            except Exception as e:  # noqa: BLE001 - handed to the awaiter
                done.set_exception(e)
            else:
                done.set_result(value)

        def committed(job: Future) -> None:  # on the writer (or here, if it already finished)
            if not loop.is_closed():
                loop.call_soon_threadsafe(deliver, job, context=context)
        self._submit(self._run_tx, fn, args, kwargs).add_done_callback(committed)
        return await finish_then_cancel(done)

    def _bootstrap(self) -> None:
        """Bring a pre-versioning (or empty) database to the frozen baseline; idempotent, so it also repairs."""
        self.conn.executescript(SCHEMA)
        self.conn.executescript(APP_SETTINGS_SCHEMA)
        for table, column, definition in LEGACY_COLUMNS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.execute(f"PRAGMA user_version = {BASELINE_VERSION}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._finalizer()  # the writer finishes the writes already queued, then stops
        with self._pool_lock:
            readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()
        self._wconn.close()

    @contextmanager
    def _tx(self, label: str | None = None):
        """One transaction on the writer thread; a nested block joins the outer one."""
        conn = self._wconn
        with self.lock.labelled(label) if label else self.lock:
            if conn.in_transaction:
                yield self
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _ensure_search_index_columns(self) -> None:
        """FTS5 tables cannot ALTER; rebuild when the household user_id column is missing."""
        try:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(search_index)")}
        except sqlite3.DatabaseError:
            cols = set()
        if "user_id" in cols:
            return
        self.conn.execute("DROP TABLE IF EXISTS search_index")
        self.conn.execute(
            "CREATE VIRTUAL TABLE search_index USING fts5("
            "text, session_id UNINDEXED, seq UNINDEXED, kind UNINDEXED, ts UNINDEXED, user_id UNINDEXED, "
            "tokenize = 'porter unicode61 remove_diacritics 2')"
        )
        self.conn.execute("DELETE FROM meta WHERE key = 'search_index'")

    # small user-facing preferences (profile emoji, later display choices)
    @_reads
    def get_meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    @_writes
    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # hosted backend usage and limits
    @_writes
    def set_backend_usage(self, backend: str, data: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO backend_usage (backend, data, updated_at) VALUES (?, ?, ?) "
                              "ON CONFLICT(backend) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                              (backend, json.dumps(data), time.time()))

    @_reads
    def get_backend_usage(self, backend: str) -> dict:
        with self.lock:
            row = self.conn.execute("SELECT * FROM backend_usage WHERE backend = ?", (backend,)).fetchone()
        return _row(row) or {"backend": backend, "data": {}, "updated_at": None}

    @_writes
    def record_usage(self, backend: str, sid: str, app_id: str, prompt_tokens: int,
                     completion_tokens: int, cost_usd: float, billing: str,
                     credential_source: str = "subscription", end_user: str = "",
                     tool_profile: str = "default") -> None:
        with self.lock:
            self.conn.execute("INSERT INTO usage (backend, session_id, app_id, prompt_tokens, completion_tokens, "
                              "cost_usd, billing, credential_source, end_user, tool_profile, created_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (backend, sid, app_id, prompt_tokens, completion_tokens, cost_usd, billing,
                               credential_source, end_user, tool_profile, time.time()))

    @_reads
    def end_user_usage(self, app_id: str, end_user: str) -> dict:
        """What one end user's sessions used (#365): the usage log's tally for that (App, end user)."""
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) sessions, COALESCE(SUM(prompt_tokens),0) prompt_tokens, "
                                    "COALESCE(SUM(completion_tokens),0) completion_tokens, "
                                    "COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE app_id = ? AND end_user = ?",
                                    (app_id, end_user)).fetchone()
        return dict(row)

    @_reads
    def member_backend_usage(self, end_user: str, backend: str) -> dict:
        """What a member's hosted sessions on `backend` used (#393): the usage log's tally for their end user id."""
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) sessions, COALESCE(SUM(prompt_tokens),0) prompt_tokens, "
                                    "COALESCE(SUM(completion_tokens),0) completion_tokens FROM usage "
                                    "WHERE app_id = '' AND end_user = ? AND backend = ?", (end_user, backend)).fetchone()
        return dict(row)

    @_writes
    def register_end_user(self, end_user: str) -> None:
        """Record an App's end user in the store it is called on (the App's own, `db.for_app(app_id)`)."""
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO end_users (id, created_at) VALUES (?, ?)",
                              (end_user, time.time()))

    @_reads
    def end_users(self) -> list[str]:
        with self.lock:
            return [r["id"] for r in self.conn.execute("SELECT id FROM end_users ORDER BY created_at, id")]

    @_writes
    def forget_end_user(self, end_user: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM end_users WHERE id = ?", (end_user,))

    @_reads
    def app_usage(self, app_id: str) -> dict:
        """What App `app_id`'s sessions used, from the usage log (metadata the main store keeps, #330)."""
        with self.lock:
            row = self.conn.execute("SELECT COALESCE(SUM(requests),0) requests, "
                                    "COALESCE(SUM(prompt_tokens),0) prompt_tokens, "
                                    "COALESCE(SUM(completion_tokens),0) completion_tokens, "
                                    "COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE app_id = ?",
                                    (app_id,)).fetchone()
        return dict(row)

    @_reads
    def app_usage_daily(self, since: float, until: float) -> list[dict]:
        """One metadata-only scan for registered Apps/devices, including revoked/erased keys and idle Apps."""
        # SQLite rounds fractional epochs to milliseconds; truncate so 23:59:59.9999 stays on its own day.
        with self.lock:
            rows = self.conn.execute(
                "SELECT k.id app_id, k.name, k.kind, date(CAST(u.created_at AS INTEGER), 'unixepoch', 'localtime') day, u.billing, "
                "COALESCE(SUM(u.requests),0) requests, COALESCE(SUM(u.prompt_tokens),0) prompt_tokens, "
                "COALESCE(SUM(u.completion_tokens),0) completion_tokens, COALESCE(SUM(u.cost_usd),0) cost_usd "
                "FROM api_keys k LEFT JOIN usage u ON u.app_id = k.id AND u.created_at >= ? AND u.created_at <= ? "
                "WHERE k.kind IN ('app', 'device') GROUP BY k.id, day, u.billing ORDER BY k.id, day, u.billing",
                (since, until)).fetchall()
        return [dict(row) for row in rows]

    @_reads
    def usage_tally(self, backend: str, since: float, app_id: str | None = None) -> dict:
        app_clause, params = (AND_APP, [backend, since, app_id]) if app_id is not None else ("", [backend, since])
        with self.lock:
            row = self.conn.execute("SELECT COALESCE(SUM(requests),0) requests, "
                                    "COALESCE(SUM(prompt_tokens),0) prompt_tokens, "
                                    "COALESCE(SUM(completion_tokens),0) completion_tokens, "
                                    "COALESCE(SUM(cost_usd),0) cost_usd FROM usage "
                                    f"WHERE backend = ? AND created_at >= ?{app_clause}", params).fetchone()
        return dict(row)

    @_reads
    def usage_by_source(self, backend: str, since: float, app_id: str | None = None) -> dict[str, dict]:
        app_clause, params = (AND_APP, [backend, since, app_id]) if app_id is not None else ("", [backend, since])
        with self.lock:
            rows = self.conn.execute(
                "SELECT credential_source, COALESCE(SUM(requests),0) requests, "
                "COALESCE(SUM(prompt_tokens),0) prompt_tokens, COALESCE(SUM(completion_tokens),0) completion_tokens, "
                "COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE backend = ? AND created_at >= ?"
                f"{app_clause} GROUP BY credential_source", params).fetchall()
        return {row["credential_source"]: {k: row[k] for k in
                                            ("requests", "prompt_tokens", "completion_tokens", "cost_usd")}
                for row in rows}

    @_reads
    def usage_by_profile(self, backend: str, since: float, app_id: str | None = None) -> dict[str, dict]:
        """The usage log's tally per tool profile (#638): `default` or `bash-only`."""
        app_clause, params = (AND_APP, [backend, since, app_id]) if app_id is not None else ("", [backend, since])
        with self.lock:
            rows = self.conn.execute(
                "SELECT tool_profile, COALESCE(SUM(requests),0) requests, "
                "COALESCE(SUM(prompt_tokens),0) prompt_tokens, COALESCE(SUM(completion_tokens),0) completion_tokens, "
                "COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE backend = ? AND created_at >= ?"
                f"{app_clause} GROUP BY tool_profile", params).fetchall()
        return {row["tool_profile"]: {k: row[k] for k in
                                       ("requests", "prompt_tokens", "completion_tokens", "cost_usd")}
                for row in rows}

    # idempotent App session creation (#462, harness/idempotency.py): the hash of the key and a digest of the
    # request, never the request itself
    @_writes
    def insert_idempotency_key(self, row: dict) -> None:
        """Record a key in the transaction creating its session; a key still protected raises IntegrityError and the
        session's insert rolls back with it. Start the protection window at this successful insert."""
        from . import idempotency

        now = idempotency.clock()
        self.drop_expired_idempotency_keys(now)
        with self.lock:
            self.conn.execute("INSERT INTO idempotency_keys (key_hash, request_digest, session_id, created_at, "
                              "expires_at) VALUES (?, ?, ?, ?, ?)",
                              (row["key_hash"], row["request_digest"], row["session_id"], now,
                               now + idempotency.WINDOW_SECONDS))

    @_writes
    def drop_expired_idempotency_keys(self, now: float) -> None:
        """Expired keys (tombstones included) are free again: their rows go."""
        with self.lock:
            self.conn.execute("DELETE FROM idempotency_keys WHERE expires_at <= ?", (now,))

    @_reads
    def find_idempotency_key(self, key_hash: str, now: float) -> dict | None:
        """The key's row while it is protected; None once it has expired or was never used."""
        with self.lock:
            row = self.conn.execute("SELECT * FROM idempotency_keys WHERE key_hash = ? AND expires_at > ?",
                                    (key_hash, now)).fetchone()
        return dict(row) if row else None

    # app tool calls
    @_writes
    def insert_app_tool_call(self, sid: str, call_id: str, name: str, args: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO app_tool_calls (session_id, call_id, name, args, status, "
                              "created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                              (sid, call_id, name, json.dumps(args), time.time()))

    @_reads
    def get_app_tool_call(self, sid: str, call_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_tool_calls WHERE session_id = ? AND call_id = ?",
                                    (sid, call_id)).fetchone()
        return _row(row)

    @_reads
    def app_tool_calls(self, sid: str, status: str | None = None) -> list[dict]:
        query, params = "SELECT * FROM app_tool_calls WHERE session_id = ?", [sid]
        if status:
            query += " AND status = ?"
            params.append(status)
        with self.lock:
            return [_row(r) for r in self.conn.execute(query + " ORDER BY created_at", params).fetchall()]

    @_writes
    def finish_app_tool_call(self, sid: str, call_id: str, status: str, output: str, ok: bool) -> bool:
        with self.lock:
            return self.conn.execute(
                "UPDATE app_tool_calls SET status = ?, output = ?, ok = ?, finished_at = ? "
                "WHERE session_id = ? AND call_id = ? AND status = 'pending'",
                (status, output, int(ok), time.time(), sid, call_id)).rowcount == 1

    @_reads
    def pending_erasures(self, due_by: float | None = None) -> list[dict]:
        """Revoked Apps (and devices) whose store and folder are still to be erased, soonest first; with `due_by`,
        only those due by then."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, name, kind, revoked_at, erase_after FROM api_keys WHERE erase_after IS NOT NULL AND "
                "erased_at IS NULL AND erase_after <= ? ORDER BY erase_after",
                (float("inf") if due_by is None else due_by,)).fetchall()
        return [dict(r) for r in rows]

    @_writes
    def mark_app_erased(self, kid: str) -> None:
        """Leave only a tombstone of an erased App in the registry: its id, name, kind and dates. Its settings,
        provider credentials and error counts go with its store."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM api_keys WHERE id = ? AND kind = 'web'", (kid,)).fetchone():
                raise ValueError("Stables is never erased")
            self.conn.execute("UPDATE api_keys SET erased_at = ?, erase_after = NULL, scopes = '', origins = '[]', "
                              "retention_days = NULL WHERE id = ?", (time.time(), kid))
            self.conn.execute("DELETE FROM app_settings WHERE app_id = ?", (kid,))
            self.conn.execute("DELETE FROM app_provider_credentials WHERE app_id = ?", (kid,))
            self.conn.execute("DELETE FROM meta WHERE key IN (?, ?)", ("app_error:" + kid, APP_LIMITS_META + kid))

    @_writes
    def set_app_retention(self, kid: str, days: float | None) -> bool:
        """An App's default retention for its sessions, in days (None: keep them until deleted)."""
        with self.lock:
            return self.conn.execute("UPDATE api_keys SET retention_days = ? WHERE id = ? "
                                     "AND kind NOT IN ('owner', 'web') "
                                     "AND erased_at IS NULL", (days, kid)).rowcount == 1

    @_reads
    def app_limits(self, kid: str) -> dict:
        """The session caps the owner set for App `kid` (#524): `max_running` and `max_queued`, each None when unset."""
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (APP_LIMITS_META + kid,)).fetchone()
        stored = json.loads(row["value"]) if row else {}
        return {k: (int(stored[k]) if stored.get(k) is not None else None) for k in APP_LIMIT_KEYS}

    @_writes
    def set_app_limits(self, kid: str, limits: dict) -> bool:
        """Replace App `kid`'s session caps (None: the daemon default). False when no live App has that id."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM api_keys WHERE id = ? AND kind = 'app' AND erased_at IS NULL",
                                 (kid,)).fetchone() is None:
                return False
            values = {k: limits.get(k) for k in APP_LIMIT_KEYS if limits.get(k) is not None}
            if values:
                self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                  (APP_LIMITS_META + kid, json.dumps(values)))
            else:
                self.conn.execute("DELETE FROM meta WHERE key = ?", (APP_LIMITS_META + kid,))
            return True

    @_reads
    def get_app_settings(self, app_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_settings WHERE app_id = ?", (app_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            out["values"] = json.loads(out["payload"])
        except (ValueError, KeyError):
            out["values"] = {}
        return out

    @_writes
    def set_app_settings(self, app_id: str, revision: int, values: dict) -> None:
        payload = json.dumps(values)
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO app_settings (app_id, revision, payload, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(app_id) DO UPDATE SET revision = excluded.revision, payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (app_id, revision, payload, now),
            )

    @_writes
    def delete_app_settings(self, app_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM app_settings WHERE app_id = ?", (app_id,))

    # household accounts (non-secret metadata only: never tokens, credentials, or session material)
    @_reads
    def member_count(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE role = 'member'").fetchone()
        return int(row["n"] if row else 0)

    @_reads
    def account_by_login(self, login: str) -> dict | None:
        if not login:
            return None
        with self.lock:
            row = self.conn.execute("SELECT * FROM accounts WHERE login = ?", (login,)).fetchone()
        return dict(row) if row else None

    @_reads
    def account_by_id(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @_reads
    def list_accounts(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM accounts WHERE role = 'member' ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    @_writes
    def insert_account(self, row: dict) -> None:
        cols = ("user_id", "role", "login", "display_name", "enabled", "disk_quota_bytes",
                "max_running", "max_queued", "created_at", "updated_at", "last_activity_at")
        with self.lock:
            self.conn.execute(
                f"INSERT INTO accounts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )

    @_writes
    def update_account(self, user_id: str, **fields) -> bool:
        if not fields:
            return False
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            return self.conn.execute(
                f"UPDATE accounts SET {sets} WHERE user_id = ?", [*fields.values(), user_id]
            ).rowcount == 1

    @_writes
    def touch_account(self, user_id: str) -> None:
        if not user_id or user_id == "owner":
            return
        with self.lock:
            self.conn.execute("UPDATE accounts SET last_activity_at = ? WHERE user_id = ?",
                              (time.time(), user_id))

    @_writes
    def insert_audit(self, actor_id: str, target_id: str, action: str, outcome: str, detail: str = "", *,
                     context=None, target_kind: str = "", metadata: dict | None = None) -> None:
        """Append one audit row (joins the caller's transaction). `context` is an AuditContext built after
        authentication; without one the row carries no credential/source attribution."""
        from . import audit_context
        ctx = context if context is not None else audit_context.AuditContext(actor_id)
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO account_audit (ts, actor_id, target_id, action, outcome, detail, actor_kind, key_id, "
                "source, target_kind, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, ctx.actor_id or actor_id, target_id, action, outcome, detail, ctx.actor_kind, ctx.key_id,
                 ctx.source, target_kind, audit_context.dump_metadata(action, metadata)),
            )
            # Retention (365 days) is applied here, on insertion; nothing else deletes audit rows.
            self.conn.execute("DELETE FROM account_audit WHERE ts < ?", (now - 365 * 86400,))

    @_reads
    def unknown_denials_since(self, action: str, since: float) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM account_audit WHERE action = ? AND outcome = 'denied' "
                "AND actor_kind = 'unknown' AND ts >= ?", (action, since)).fetchone()[0]

    @_reads
    def list_audit(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, actor_id, target_id, action, outcome, detail FROM account_audit "
                "ORDER BY ts DESC, id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [dict(r) for r in rows]

    @_writes
    def insert_namespace_audit(self, namespace: str, session_id: str, context, target: str, kind: str,
                               action: str, outcome: str, metadata: dict, days: float = 30) -> None:
        now = time.time()
        values = (now, namespace, session_id, context.actor_id, context.actor_kind, context.key_id, context.source,
                  target, kind, action, outcome, json.dumps(metadata, sort_keys=True, separators=(',', ':')),
                  None if session_id else now + min(30, days) * 86400)
        checksum = sha256_hex(json.dumps(values, separators=(',', ':')))
        with self.lock:
            self.conn.execute(
                "INSERT INTO namespace_audit (ts, namespace, session_id, actor_id, actor_kind, key_id, source, "
                "target_id, target_kind, action, outcome, metadata, expires_at, checksum) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values, checksum))
            self.conn.execute("DELETE FROM namespace_audit WHERE expires_at <= ?", (now,))

    @_writes
    def prune_namespace_audit(self, now: float | None = None, days: float = 30) -> None:
        now = time.time() if now is None else now
        with self.lock:
            self.conn.execute("DELETE FROM namespace_audit WHERE session_id = '' AND "
                              "(expires_at <= ? OR ts <= ?)", (now, now - min(30, days or 30) * 86400))

    @_reads
    def namespace_audit_page(self, namespace: str, limit: int = 200, before_id: int | None = None, *,
                             target_id: str | None = None, actor_id: str | None = None, key_id: str | None = None,
                             action: str | None = None, login_days: float = 30,
                             outcome: str | None = None, since: float | None = None,
                             until: float | None = None) -> dict:
        limit = max(1, min(int(limit), 500))
        now = time.time()
        where = ["namespace = ?", "(expires_at IS NULL OR expires_at > ?)", "(session_id <> '' OR ts > ?)"]
        args = [namespace, now, now - min(30, login_days or 30) * 86400]
        for column, operator, value in (("id", "<", before_id), ("target_id", "=", target_id),
                                         ("actor_id", "=", actor_id), ("key_id", "=", key_id),
                                         ("action", "=", action), ("outcome", "=", outcome),
                                         ("ts", ">=", since), ("ts", "<", until)):
            if value is not None:
                where.append(f"{column} {operator} ?")
                args.append(value)
        with self.lock:
            rows = self.conn.execute("SELECT id, ts, actor_id, actor_kind, key_id, source, target_id, "
                                     "target_kind, action, outcome, metadata FROM namespace_audit WHERE "
                                     + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
                                     [*args, limit + 1]).fetchall()
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            item["metadata"] = json.loads(item["metadata"])
        return {"items": items, "next_before_id": items[-1]["id"] if len(rows) > limit else None}

    @_reads
    def audit_page(self, limit: int = 200, before_id: int | None = None, *, actor_id: str | None = None,
                   key_id: str | None = None, target_id: str | None = None, action: str | None = None,
                   outcome: str | None = None, since: float | None = None, until: float | None = None) -> dict:
        """Newest-first page by immutable id; `next_before_id` is the last id of a full page that has more rows."""
        limit = max(1, min(int(limit), 500))
        where, args = [], []
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        for column, value in (("actor_id", actor_id), ("key_id", key_id), ("target_id", target_id),
                              ("action", action), ("outcome", outcome)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts < ?")
            args.append(until)
        sql = ("SELECT id, ts, actor_id, actor_kind, key_id, source, target_id, target_kind, action, outcome, "
               "detail, metadata FROM account_audit")
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.lock:
            rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit + 1]).fetchall()
        more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item["metadata"] or "{}")
            except ValueError:
                item["metadata"] = {}
            items.append(item)
        return {"items": items, "next_before_id": items[-1]["id"] if more and items else None}

    @_reads
    def get_member_project(self, user_id: str, slug: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM member_projects WHERE user_id = ? AND slug = ?", (user_id, slug)
            ).fetchone()
        return dict(row) if row else None

    @_reads
    def list_member_projects(self, user_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM member_projects WHERE user_id = ? ORDER BY slug", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    @_writes
    def insert_member_project(self, row: dict) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO member_projects (user_id, slug, description, repo, source_url, source_auth, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["user_id"], row["slug"], row.get("description") or "", row.get("repo") or "",
                 row.get("source_url") or "", row.get("source_auth") or "", now, now),
            )

    @_reads
    def get_github_connection(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM github_connections WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @_reads
    def list_github_connections(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM github_connections ORDER BY user_id").fetchall()
        return [dict(r) for r in rows]

    @_writes
    def set_github_connection(self, user_id: str, **fields) -> None:
        """Upsert non-secret connection state: status, timestamps, and a sanitized error class only."""
        allowed = {"status", "namespace_version", "connected_at", "last_used_at", "last_error"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown github connection fields: {sorted(bad)}")
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO github_connections (user_id, updated_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO NOTHING", (user_id, now))
            if fields:
                cols = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE github_connections SET {cols}, updated_at = ? WHERE user_id = ?",
                                  (*fields.values(), now, user_id))

    @_writes
    def delete_member_project(self, user_id: str, slug: str) -> bool:
        with self.lock:
            return self.conn.execute(
                "DELETE FROM member_projects WHERE user_id = ? AND slug = ?", (user_id, slug)
            ).rowcount == 1

    # Issue #393: a member's own provider API key. `ciphertext` is sealed by `member_keys`; `last4` is all that shows.
    @_reads
    def member_api_key(self, user_id: str, backend: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM member_api_keys WHERE user_id = ? AND backend = ?",
                                    (user_id, backend)).fetchone()
        return dict(row) if row else None

    @_reads
    def member_api_keys(self, user_id: str) -> list[dict]:
        """The member's keys without their ciphertext."""
        with self.lock:
            rows = self.conn.execute("SELECT user_id, backend, last4, created_at, updated_at FROM member_api_keys "
                                     "WHERE user_id = ? ORDER BY backend", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    @_writes
    def set_member_api_key(self, user_id: str, backend: str, ciphertext: bytes, last4: str) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO member_api_keys (user_id, backend, ciphertext, last4, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, backend) DO UPDATE SET "
                "ciphertext = excluded.ciphertext, last4 = excluded.last4, updated_at = excluded.updated_at",
                (user_id, backend, ciphertext, last4, now, now))

    @_writes
    def delete_member_api_keys(self, user_id: str, backend: str | None = None) -> int:
        """Delete one backend's key, or every key of the member."""
        with self.lock:
            if backend is None:
                return self.conn.execute("DELETE FROM member_api_keys WHERE user_id = ?", (user_id,)).rowcount
            return self.conn.execute("DELETE FROM member_api_keys WHERE user_id = ? AND backend = ?",
                                     (user_id, backend)).rowcount

    # Issue #64: Google identities, link invitations, and Stables sessions (hashes only).
    @_reads
    def google_identity(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM google_identities WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @_reads
    def google_identity_by_sub(self, sub: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM google_identities WHERE sub = ?", (sub,)).fetchone()
        return dict(row) if row else None

    @_writes
    def link_google_identity(self, user_id: str, sub: str, email: str, now: float) -> bool:
        """Insert the link unless the member or the `sub` is already linked. Callers hold `tx()` when combining."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO google_identities (user_id, sub, email, linked_at, last_sign_in_at) "
                    "VALUES (?, ?, ?, ?, ?)", (user_id, sub, email, now, now))
            except sqlite3.IntegrityError:
                return False
        return True

    @_writes
    def unlink_google_identity(self, user_id: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM google_identities WHERE user_id = ?", (user_id,)).rowcount == 1

    @_writes
    def touch_google_sign_in(self, user_id: str, email: str, now: float) -> None:
        with self.lock:
            self.conn.execute("UPDATE google_identities SET email = ?, last_sign_in_at = ? WHERE user_id = ?",
                              (email, now, user_id))

    @_writes
    def put_google_invitation(self, user_id: str, code_hash: str, now: float, expires_at: float) -> None:
        """Replace any pending invitation for this member."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO google_link_invitations (user_id, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET code_hash = excluded.code_hash, "
                "created_at = excluded.created_at, expires_at = excluded.expires_at",
                (user_id, code_hash, now, expires_at))

    @_reads
    def google_invitation(self, user_id: str, now: float) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id, created_at, expires_at FROM google_link_invitations "
                "WHERE user_id = ? AND expires_at > ?", (user_id, now)).fetchone()
        return dict(row) if row else None

    @_reads
    def google_invitation_user(self, code_hash: str, now: float) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id FROM google_link_invitations WHERE code_hash = ? AND expires_at > ?",
                (code_hash, now)).fetchone()
        return row["user_id"] if row else None

    @_writes
    def consume_google_invitation(self, user_id: str, code_hash: str, now: float) -> bool:
        with self.lock:
            return self.conn.execute(
                "DELETE FROM google_link_invitations WHERE user_id = ? AND code_hash = ? AND expires_at > ?",
                (user_id, code_hash, now)).rowcount == 1

    @_writes
    def cancel_google_invitation(self, user_id: str) -> bool:
        with self.lock:
            return self.conn.execute(
                "DELETE FROM google_link_invitations WHERE user_id = ?", (user_id,)).rowcount == 1

    @_writes
    def insert_web_session(self, id_hash: str, user_id: str, now: float, expires_at: float) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO web_sessions (id_hash, user_id, created_at, last_seen_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)", (id_hash, user_id, now, now, expires_at))

    @_reads
    def web_session(self, id_hash: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM web_sessions WHERE id_hash = ?", (id_hash,)).fetchone()
        return dict(row) if row else None

    @_writes
    def touch_web_session(self, id_hash: str, now: float) -> None:
        with self.lock:
            self.conn.execute("UPDATE web_sessions SET last_seen_at = ? WHERE id_hash = ? AND revoked_at IS NULL",
                              (now, id_hash))

    @_writes
    def revoke_web_session(self, id_hash: str, now: float) -> str | None:
        """Revoke one session; returns its member `user_id` when it was active."""
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id FROM web_sessions WHERE id_hash = ? AND revoked_at IS NULL", (id_hash,)).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE web_sessions SET revoked_at = ? WHERE id_hash = ?", (now, id_hash))
        return row["user_id"]

    @_writes
    def revoke_web_sessions(self, user_id: str, now: float) -> int:
        with self.lock:
            return self.conn.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id)).rowcount

    @_reads
    def count_web_sessions(self, user_id: str, now: float, idle_seconds: float) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM web_sessions WHERE user_id = ? AND revoked_at IS NULL "
                "AND expires_at > ? AND last_seen_at > ?", (user_id, now, now - idle_seconds)).fetchone()
        return int(row["n"])

    @_writes
    def purge_web_sessions(self, before: float) -> int:
        """Drop rows that expired or were revoked before `before`."""
        with self.lock:
            return self.conn.execute(
                "DELETE FROM web_sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
                (before, before)).rowcount
