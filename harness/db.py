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
import json
import queue
import sqlite3
import threading
import weakref
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Sequence, cast

from . import migrations as migrations_mod
from .migrations.baseline import BASELINE_VERSION, LEGACY_COLUMNS

from .telemetry import TimedLock

if TYPE_CHECKING:
    from .account_store import AccountStore
    from .app_store import AppStore
    from .credential_store import CredentialStore
    from .session_store import SessionStore
    from .usage_store import UsageStore

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


# The core stores a Database builds, by attribute (#992 stage 5). `app_stores` says where each one's rows live.
CORE_STORES = ("credentials", "sessions", "accounts", "apps", "usage")


class Database:
    """The core's one SQLite file (#992): the writer thread, the pooled readers, transactions, migrations and the
    `meta` table, with each core store as an attribute (`db.credentials`, `db.sessions`, `db.accounts`, `db.apps`,
    `db.usage`). It forwards nothing: a query is called on its store (`tests/test_database_shim.py`)."""

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
        from .account_store import AccountStore  # each store module imports this one
        from .app_store import AppStore
        from .credential_store import CredentialStore
        from .session_store import SessionStore
        from .usage_store import UsageStore

        self.credentials: CredentialStore = CredentialStore(self)
        self.sessions: SessionStore = SessionStore(self)
        self.accounts: AccountStore = AccountStore(self)
        self.apps: AppStore = AppStore(self)
        self.usage: UsageStore = UsageStore(self)
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
