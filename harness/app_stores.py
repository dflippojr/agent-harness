"""Per-App data stores (#330).

Each App's sessions live in their own SQLite file, `<data_dir>/apps/<app_id>/harness.sqlite3`, which runs the same
versioned migrations (#256) and has its own writer thread and read pool (#294). Agent Harness Web is an App too
(`WEB_APP_ID`, #330 decision 4): its store holds the sessions with no App, the owner's and members', and stays open
for the daemon's life. The main store keeps everything else: the App registry (`api_keys`, Web's row included),
per-App usage and error counters, provider credentials, settings, jobs, skills, accounts and the audit log.
`migrate_web_store` moves the sessions the main store held before into Web's store, once.

`SessionStores` stands in for the main `Database`. A call about one session (`get_session(sid)`, `insert_event(sid,
...)`, ...) runs on that session's store; anything else runs on the main store. A transaction (`write`/`awrite`) that
touches a session must run on that session's writer: `db.for_session(sid).write(fn)`, or `db.for_app(app_id)` for a
session not created yet. An App store opens on first use and closes after `APP_STORE_IDLE_SECONDS` without one.

The owner and members never read another App's store (#330 decision 3): their lists, searches and id lookups cover
Web's store only, and an App's cover its own (`db.scope(app_id)`), plus Web's for an owner-granted `sessions:all` read.
A query narrowed to an App (`app_id=...`) never reads Web's store, which holds no App's sessions. The owner sees
per-App metadata instead (`app_metadata`): session counts by status from the index, usage and the last error from the
main store.

An App's sessions keep their files in its folder too (`storage.session_dirs`): `workspaces/`, `transcripts/`,
`checkpoints/` and `artifacts/` next to the store. `drop_app` erases the whole folder once a revoked App's grace is
over (#330 decision 5; the manager's `sweep_app_data` decides when).
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .db import Database
from .sqlite_backup import backup_sqlite

log = logging.getLogger(__name__)

APP_STORE_FILE = "harness.sqlite3"
# An App store nobody has used for this long is closed (its writer thread stopped, its read pool emptied). The next
# call reopens it, so this only bounds the threads and file handles of Apps that have gone quiet.
APP_STORE_IDLE_SECONDS = 300.0
_APP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Rows tied to one session (by session_id) that live in its App's store. The search index is rebuilt there from the
# events; `usage` (metadata) and `stream_tickets` (auth) stay in the main store.
SESSION_TABLES = ("events", "artifacts", "approvals", "app_tool_calls", "smart_reviews", "review_comments",
                  "secret_dismissals", "checkpoints", "namespace_audit")
# Database methods whose first argument is a session id: they run on that session's store.
_BY_SESSION = frozenset({
    "get_session", "get_session_for_user", "session_brief", "add_checkpoint", "checkpoints",
    "show_checkpoints_through", "delete_checkpoints", "put_artifact", "read_artifact", "full_artifact",
    "add_review_comment", "list_review_comments", "add_secret_dismissal", "secret_dismissals",
    "delete_review_comments", "insert_event", "events", "pushed_heads", "last_event_seq", "approval_for_call",
    "approvals", "insert_app_tool_call", "get_app_tool_call", "app_tool_calls", "finish_app_tool_call",
})
# The by-session calls above that write. An erased App session's id leaves the index, so they would land in the main
# store (the owner's): they are refused for it instead.
_SESSION_WRITES = frozenset({
    "add_checkpoint", "delete_checkpoints", "put_artifact", "add_review_comment", "add_secret_dismissal",
    "delete_review_comments", "insert_event", "insert_app_tool_call", "finish_app_tool_call",
})
# Session columns the in-memory index of App sessions mirrors.
_INDEXED = ("app_id", "owner_id", "kind", "status")
_SEQ = itertools.count(1)  # orders the index updates staged by transactions, in the order they ran
_ENDED = ("done", "failed", "cancelled")
# `with_app` for the daemon's own lookups, which reach every App's sessions. Callers acting for a person or an App
# pass "" (the owner and members: the main store only) or the App's id (the main store plus that App's own).
EVERY_APP = "*"
# Lists, searches and id lookups that take `with_app`; `scope(app_id)` fills it in.
_SCOPED = frozenset({"list_sessions", "search_events", "find_session_ids", "pending_approvals"})
_APP_ERROR = "app_error:"  # meta key prefix: an App's failed-session count and last error (metadata, main store)
# Agent Harness Web's reserved App id (#330 decision 4). Its store holds every session with no App (the owner's and
# members'); it has no token, is never revoked or erased, and its sessions keep their owner's folders (`storage`).
WEB_APP_ID = "app-web"
# `dry-run`: log what the move into Web's store would do and leave everything where it is (the main store keeps the
# sessions and serves them, as before #330 stage c).
WEB_MIGRATION_ENV = "HARNESS_WEB_STORE_MIGRATION"
WEB_STORE_META = "web_store"  # main-store meta: when the sessions moved into Web's store, how many, and the backup
# Main-store methods that read sessions or their rows without taking a session id: they run on Web's store.
_ON_WEB = frozenset({"group_sessions", "snippet_events", "smart_review_stats", "smart_reviews", "job_sessions",
                     "session_activity"})


class WebStoreMismatch(RuntimeError):
    """The rows copied into Web's store do not match the main store's: the move is aborted and rolled back."""


def app_dir(data_dir: Path, app_id: str) -> Path:
    """An App's data folder."""
    if not _APP_ID.fullmatch(app_id or ""):
        raise ValueError(f"not an App id: {app_id!r}")
    return Path(data_dir) / "apps" / app_id


def _remove_tree(path: Path) -> None:
    """shutil.rmtree that also removes read-only files (git objects are read-only on Windows)."""
    def on_error(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=on_error)


def _same_path(a: str | Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _chunks(ids: list[str], size: int = 500):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _marks(ids: list[str]) -> str:
    return ",".join("?" * len(ids))


def _session_row_counts(db: Database, sids: list[str]) -> dict[str, int]:
    """Rows of sessions `sids` in `db`, by table (on the connection this thread has: a read or the writer)."""
    counts = {}
    for table in ("sessions", *SESSION_TABLES):
        key = "id" if table == "sessions" else "session_id"
        counts[table] = sum(db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {key} IN ({_marks(part)})",
                                            part).fetchone()[0] for part in _chunks(sids))
    return counts


def _delete_session_rows(conn: sqlite3.Connection, sids: list[str]) -> None:
    for part in _chunks(sids):
        for table in (*SESSION_TABLES, "search_index"):
            conn.execute(f"DELETE FROM {table} WHERE session_id IN ({_marks(part)})", part)
        conn.execute(f"DELETE FROM sessions WHERE id IN ({_marks(part)})", part)


def _unused(path: Path) -> Path:
    """`path`, or `path` with a counter, so that no backup overwrites an earlier one; its folder created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    for n in itertools.count(1):
        if not path.exists():
            return path
        path = path.with_name(f"{stem}-{n}{path.suffix}")
    raise AssertionError("unreachable")


def _kind(name: str) -> str | None:
    return getattr(getattr(Database, name, None), "db_kind", None)


def _like(prefix: str) -> re.Pattern:
    """SQLite's `id LIKE prefix || '%'` as a regex."""
    body = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in prefix)
    return re.compile(body + ".*", re.IGNORECASE | re.DOTALL)


class _OpenStore:
    __slots__ = ("db", "users", "last_used")

    def __init__(self, db: Database):
        self.db, self.users, self.last_used = db, 0, time.monotonic()


class _ThreadAio:
    """`await store.aio.<method>(...)`: the method in a worker thread, off the event loop."""

    def __init__(self, target):
        self._target = target

    def __getattr__(self, name: str):
        method = getattr(self._target, name)

        async def call(*args, **kwargs):
            return await asyncio.to_thread(method, *args, **kwargs)
        return call


class _AppStore:
    """One App's store with the Database interface. It holds no connection: each call opens the store if it was
    closed and keeps it open until the call returns (a write until it has committed)."""

    def __init__(self, stores: SessionStores, app_id: str):
        self._stores, self.app_id = stores, app_id
        self.aio = _ThreadAio(self)

    def for_session(self, sid: str):
        return self._stores.for_session(sid)

    def for_app(self, app_id: str):
        return self._stores.for_app(app_id)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        stores, app_id = self._stores, self.app_id
        if name in ("insert_session", "update_session", "delete_session"):  # they keep the index current
            return getattr(stores, name)
        if name in ("awrite", "aread"):
            async def call_async(*args, **kwargs):
                db = await stores._aacquire(app_id)
                try:
                    return await getattr(db, name)(*args, **kwargs)
                finally:
                    stores._release(app_id)
            return call_async
        if not callable(getattr(Database, name, None)):
            with stores._using(app_id) as db:
                return getattr(db, name)
        write = name == "write" or _kind(name) == "write"

        def call(*args, **kwargs):
            with stores._using(app_id, write=write) as db:
                return getattr(db, name)(*args, **kwargs)
        return call


class _Scoped:
    """`SessionStores.scope(app_id)`: lists, searches and id lookups as one App (or, for "", the owner) may see
    them; everything else as `SessionStores`."""

    def __init__(self, stores: SessionStores, app_id: str):
        self._stores, self.app_id = stores, app_id

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        method = getattr(self._stores, name)
        return functools.partial(method, with_app=self.app_id) if name in _SCOPED else method


def scoped(db, app_id: str):
    """`db` as App `app_id` (or, for "", the owner) may see it. A lone Database has no App stores: it is returned."""
    scope = getattr(db, "scope", None)
    return scope(app_id) if scope is not None else db


def _reap(ref: weakref.ref, stop: threading.Event, every: float) -> None:
    while not stop.wait(every):
        stores = ref()
        if stores is None:
            return
        stores.close_idle()
        del stores


class SessionStores:
    """The main Database plus one lazily opened store per App, behind the Database interface.

    Session calls route by session id through an in-memory index of App sessions (id -> App, owner, kind, status),
    read from the App stores at startup and kept current by `insert_session`, `update_session` and `delete_session`.
    Ids not in the index belong to Web's store (`self.web`; the main store until `migrate_web_store` has moved the
    owner's and members' sessions there), which is not indexed: it is open for the daemon's life. The index never
    runs ahead of what has committed: a new session routes to its App from the insert on (so the transaction creating
    it can use it) but counts nowhere until it commits, and is dropped if it rolls back; owner, kind and status
    change, and a deleted session leaves, only once the transaction that did it commits. Inside an App store's
    transaction the main store may be called
    (its writer is waited for), never the reverse and never another App's store, so two writers can never wait on
    each other: such a write raises instead of deadlocking.

    Lists and searches (list_sessions, search_events, pending_approvals) read Web's store (unless narrowed to an
    App), plus the App stores named by `app_id` (a filter on one App) or `with_app` (an App's own view); never every
    App's, so nothing the owner or a member asks for reads an App store (#330 decision 3). `find_session_ids` reads
    only the index for App sessions; it reaches every App unless `with_app` narrows it, since the daemon resolves its
    own full ids with it. The daemon's sweeps (sessions_with_status, sessions_with_run_flag, count_sessions) cover
    every store. Group, job and chat lists, the metrics and the smart-review stats read Web's store only (`_ON_WEB`):
    App sessions are counted in `app_metadata` instead.
    """

    def __init__(self, main: Database, apps_dir: Path, idle_seconds: float = APP_STORE_IDLE_SECONDS):
        self.main = main
        self.apps_dir = Path(apps_dir)
        self.idle_seconds = idle_seconds
        self.aio = _ThreadAio(self)
        self._lock = threading.RLock()
        self._open: dict[str, _OpenStore] = {}
        self._proxies: dict[str, _AppStore] = {}
        self._sessions: dict[str, dict] = {}
        self._approvals: dict[str, str] = {}   # approval id -> App
        self._tokens: dict[str, str] = {}      # approval token -> App
        self._erased: set[str] = set()       # Apps whose store and folder are gone: never reopened
        self._gone: set[str] = set()         # App sessions erased since startup: writes about them are refused
        self._closed = False
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        self.web: Database = main  # until the sessions are in Web's store (`migrate_web_store`)
        self.migrate_app_sessions()
        self.web = self.migrate_web_store()
        self._load_index()
        self.migrate_app_files()

    # stores -----------------------------------------------------------------------------------------------------------
    def store_path(self, app_id: str) -> Path:
        if not _APP_ID.fullmatch(app_id or ""):
            raise ValueError(f"not an App id: {app_id!r}")
        return self.apps_dir / app_id / APP_STORE_FILE

    def open_apps(self) -> list[str]:
        with self._lock:
            return sorted(self._open)

    def _acquire(self, app_id: str) -> Database:
        with self._lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            if app_id in self._erased:
                raise sqlite3.ProgrammingError(f"App {app_id}'s store was erased")
            entry = self._open.get(app_id)
            if entry is None:
                entry = self._open[app_id] = _OpenStore(Database(self.store_path(app_id)))
                self._start_reaper()
            entry.users += 1
            entry.last_used = time.monotonic()
            return entry.db

    async def _aacquire(self, app_id: str) -> Database:
        with self._lock:
            entry = self._open.get(app_id)
            if entry is not None and not self._closed:
                entry.users += 1
                entry.last_used = time.monotonic()
                return entry.db
        return await asyncio.to_thread(self._acquire, app_id)  # opening runs the migrations: off the loop

    def _release(self, app_id: str) -> None:
        with self._lock:
            entry = self._open.get(app_id)
            if entry is not None:
                entry.users -= 1
                entry.last_used = time.monotonic()

    @contextmanager
    def _using(self, app_id: str, write: bool = False):
        db = self._acquire(app_id)
        try:
            if write:
                here = self._writer_here()
                if here is not None and here is not db:
                    raise RuntimeError(f"a write to App {app_id}'s store from another store's transaction: run the "
                                       "transaction on the session's own store (db.for_session(sid).write)")
            yield db
        finally:
            self._release(app_id)

    def _call(self, app_id: str, name: str, /, *args, **kwargs):
        with self._using(app_id, write=_kind(name) == "write") as db:
            return getattr(db, name)(*args, **kwargs)

    def _writer_here(self) -> Database | None:
        """The store whose writer thread this is, if any."""
        if self.main._on_writer():
            return self.main
        if self.web._on_writer():
            return self.web
        with self._lock:
            entries = list(self._open.values())
        return next((e.db for e in entries if e.db._on_writer()), None)

    def _start_reaper(self) -> None:
        if self._reaper is None and self.idle_seconds > 0:
            every = max(0.05, min(60.0, self.idle_seconds / 4))
            self._reaper = threading.Thread(target=_reap, args=(weakref.ref(self), self._stop, every),
                                            name="harness-app-stores", daemon=True)
            self._reaper.start()

    def close_idle(self, now: float | None = None) -> list[str]:
        """Close every App store unused for `idle_seconds`; returns their App ids."""
        now = time.monotonic() if now is None else now
        with self._lock:
            idle = [a for a, e in self._open.items() if e.users == 0 and now - e.last_used >= self.idle_seconds]
            closing = [self._open.pop(a).db for a in idle]
        for db in closing:
            db.close()  # the writer finishes what is queued, then stops
        return idle

    def close(self) -> None:
        with self._lock:
            self._closed = True
            closing = [e.db for e in self._open.values()]
            self._open.clear()
        self._stop.set()
        for db in closing:
            db.close()
        if self.web is not self.main:
            self.web.close()
        self.main.close()

    # routing ----------------------------------------------------------------------------------------------------------
    def for_app(self, app_id: str):
        """App `app_id`'s store (created on first use), or Web's for "" or `WEB_APP_ID` (owner and member sessions)."""
        if not app_id or app_id == WEB_APP_ID:
            return self.web
        with self._lock:
            proxy = self._proxies.get(app_id)
            if proxy is None:
                self.store_path(app_id)  # refuses a malformed id
                proxy = self._proxies[app_id] = _AppStore(self, app_id)
            return proxy

    def for_session(self, sid: str):
        """The store that holds session `sid`: its App's, or Web's for any other id."""
        with self._lock:
            entry = self._sessions.get(sid)
        return self.for_app(entry["app_id"]) if entry else self.web

    def scope(self, app_id: str) -> _Scoped:
        """Lists, searches and id lookups as App `app_id` sees them: its own store, plus Web's unless the query is
        narrowed to an App; "" for the owner's and members' view, Web's store alone."""
        return _Scoped(self, app_id)

    def app_of(self, sid: str) -> str:
        with self._lock:
            entry = self._sessions.get(sid)
        return entry["app_id"] if entry else ""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in _BY_SESSION:
            def by_session(sid, *args, **kwargs):
                if name in _SESSION_WRITES:
                    self._refuse_erased(sid)
                return getattr(self.for_session(sid), name)(sid, *args, **kwargs)
            return by_session
        return getattr(self.web if name in _ON_WEB else self.main, name)

    def _refuse_erased(self, sid: str) -> None:
        """Refuse a write about an erased App session: nothing of it may be written again, least of all into the
        main store, where its id now routes."""
        with self._lock:
            if sid in self._gone:
                raise sqlite3.ProgrammingError(f"session {sid} was erased")

    def _indexed(self, pred) -> list[tuple[str, dict]]:
        with self._lock:
            return [(sid, e) for sid, e in self._sessions.items() if e["committed"] and pred(e)]

    def _apps(self, pred=lambda e: True) -> list[str]:
        return sorted({e["app_id"] for _, e in self._indexed(pred)})

    @staticmethod
    def _reads_web(app_id: str | None) -> bool:
        """Whether a query reads Web's store: not when it is narrowed to an App, whose sessions Web's store never
        holds (so an App's own queries never reach it)."""
        return not app_id

    def _reach(self, app_id: str | None, with_app: str | None, pred=lambda e: True) -> list[str]:
        """The App stores a list or search reads besides the main store, among those the index says hold a match:
        `app_id`'s and `with_app`'s, or every App's for `with_app=EVERY_APP`."""
        if with_app == EVERY_APP:
            return self._apps(pred)
        wanted = {a for a in (app_id, with_app) if a}
        return self._apps(lambda e: e["app_id"] in wanted and pred(e)) if wanted else []

    # transactions -----------------------------------------------------------------------------------------------------
    def in_transaction(self) -> bool:
        db = self._writer_here()
        return db is not None and db.in_transaction()

    def after_commit(self, callback) -> None:
        (self._writer_here() or self.main).after_commit(callback)

    # sessions ---------------------------------------------------------------------------------------------------------
    def insert_session(self, s: dict) -> None:
        sid, app_id = s["id"], s.get("app_id") or ""
        with self._lock:
            taken = sid in self._sessions
            if app_id and not taken:  # routes the id from here on, counts once committed
                self._sessions[sid] = {k: s.get(k) or "" for k in _INDEXED} | {"app_id": app_id, "seq": 0,
                                                                                "committed": False}
        if taken or (app_id and self.web.get_session(sid) is not None):
            if app_id and not taken:
                self._forget(sid)
            raise sqlite3.IntegrityError(f"session id {sid} is already in use")
        if not app_id:
            return self.web.insert_session(s)

        def insert(db: Database) -> None:
            db.after_rollback(lambda: self._forget(sid))
            db.insert_session(s)
            self._stage(db, sid)
        try:
            with self._using(app_id, write=True) as db:
                db.write(insert, db)
        except BaseException:
            self._forget(sid)
            raise

    def update_session(self, sid: str, **fields) -> None:
        self._refuse_erased(sid)
        app_id = self.app_of(sid)
        if not app_id:
            return self.web.update_session(sid, **fields)

        def update(db: Database) -> None:
            db.update_session(sid, **fields)
            if any(k in fields for k in _INDEXED):
                self._stage(db, sid)
            if fields.get("status") == "failed":
                reason = str(fields.get("stop_reason") or "")
                db.after_commit(lambda: self._record_error(app_id, reason))
        with self._using(app_id, write=True) as db:
            db.write(update, db)

    def _record_error(self, app_id: str, reason: str) -> None:
        """Count a failed session of App `app_id` in the main store, with the kind of error: its stop reason up to the
        first colon, where details that could hold the App's data begin."""
        key, kind = _APP_ERROR + app_id, reason.split(":", 1)[0].strip()[:80] or "failed"

        def bump() -> None:
            old = json.loads(self.main.get_meta(key) or "{}")
            self.main.set_meta(key, json.dumps({"errors": int(old.get("errors") or 0) + 1, "last_error": kind,
                                                "last_error_at": time.time()}))
        try:
            self.main.write(bump)
        except Exception:  # noqa: BLE001 - metadata only: the session's own write has committed
            log.exception("could not count a failed session of App %s", app_id)

    # owner metadata ---------------------------------------------------------------------------------------------------
    def app_metadata(self, app_id: str) -> dict:
        """What the owner may know about App `app_id`'s store (#330 decision 3): its sessions counted by status (from
        the index), its usage and its failed sessions (from the main store). Reads nothing in the App's store."""
        sessions: dict[str, int] = {}
        for _, e in self._indexed(lambda e: e["app_id"] == app_id):
            sessions[e["status"]] = sessions.get(e["status"], 0) + 1
        error = json.loads(self.main.get_meta(_APP_ERROR + app_id) or "{}")
        return {"sessions": sessions, "usage": self.main.app_usage(app_id), "errors": int(error.get("errors") or 0),
                "last_error": error.get("last_error") or "", "last_error_at": error.get("last_error_at")}

    def list_api_keys(self) -> list[dict]:
        """The key list (Settings → Apps), each App's and device's with its store's metadata."""
        keys = self.main.list_api_keys()
        for k in keys:
            if k.get("kind") not in ("owner", "web") and not k.get("erased_at"):
                k["store"] = self.app_metadata(k["id"])
        return keys

    def delete_session(self, sid: str) -> None:
        app_id = self.app_of(sid)
        if not app_id:
            return self.web.delete_session(sid)

        def delete(db: Database) -> None:
            approvals = db.conn.execute("SELECT id, token FROM approvals WHERE session_id = ?", (sid,)).fetchall()
            db.delete_session(sid)

            def forget() -> None:
                self._forget(sid, committed=True)
                with self._lock:
                    self._gone.add(sid)
                for a in approvals:
                    self._forget_approval(a["id"], a["token"])
            db.after_commit(forget)
        with self._using(app_id, write=True) as db:
            db.write(delete, db)

    def app_session_ids(self, app_id: str) -> list[str]:
        """App `app_id`'s sessions, from the index."""
        return [sid for sid, _ in self._indexed(lambda e: e["app_id"] == app_id)]

    def indexed_apps(self) -> list[str]:
        """The Apps that have sessions, from the index."""
        return self._apps()

    def drop_app(self, app_id: str) -> None:
        """Erase App `app_id`'s store and folder: every session it holds and their files. Stop its sessions first;
        the store is closed under any call still using it. It is never reopened: a later call about it raises.
        Web's store is never erased: it holds the owner's and members' sessions."""
        if app_id == WEB_APP_ID:
            raise ValueError("Agent Harness Web's store is never erased")
        folder = self.store_path(app_id).parent
        with self._lock:
            self._erased.add(app_id)
            entry = self._open.pop(app_id, None)
            self._proxies.pop(app_id, None)
            for sid in [s for s, e in self._sessions.items() if e["app_id"] == app_id]:
                del self._sessions[sid]
                self._gone.add(sid)
            for aid in [a for a, owner in self._approvals.items() if owner == app_id]:
                del self._approvals[aid]
            for token in [t for t, owner in self._tokens.items() if owner == app_id]:
                del self._tokens[token]
        if entry is not None:
            entry.db.close()
        _remove_tree(folder)
        log.info("erased the store and folder of App %s", app_id)

    def _stage(self, db: Database, sid: str) -> None:
        """In `db`'s transaction: once it commits, the index holds the session's row as this transaction left it.
        A transaction that ran later wins, whichever of their callbacks runs first."""
        row, seq = db.get_session(sid) or {}, next(_SEQ)

        def apply() -> None:
            with self._lock:
                entry = self._sessions.get(sid)
                if entry is None:  # deleted meanwhile
                    return
                if seq > entry["seq"]:
                    entry.update({k: row.get(k) or "" for k in _INDEXED if k != "app_id"}, seq=seq)
                entry["committed"] = True
        db.after_commit(apply)

    def _forget(self, sid: str, committed: bool = False) -> None:
        """Drop `sid` from the index: a session deleted (`committed`), or one whose insert did not commit."""
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is not None and (committed or not entry["committed"]):
                del self._sessions[sid]

    def find_session_ids(self, prefix: str, user_id: str | None = None, app_id: str | None = None,
                         kind: str | None = None, with_app: str | None = EVERY_APP) -> list[str]:
        ids = self.web.find_session_ids(prefix, user_id=user_id, kind=kind) if self._reads_web(app_id) else []
        apps = set(self._reach(app_id, with_app))
        like = _like(prefix)
        return ids + [sid for sid, _ in self._indexed(
            lambda e: e["app_id"] in apps and (app_id is None or e["app_id"] == app_id)
            and (user_id is None or e["owner_id"] == user_id) and (kind is None or e["kind"] == kind))
            if like.fullmatch(sid)]

    def sessions_with_status(self, *statuses: str, user_id: str | None = None) -> list[dict]:
        rows = self.web.sessions_with_status(*statuses, user_id=user_id)
        apps = self._apps(lambda e: e["status"] in statuses and (user_id is None or e["owner_id"] == user_id))
        if not apps:
            return rows
        for app_id in apps:
            rows += self._call(app_id, "sessions_with_status", *statuses, user_id=user_id)
        return sorted(rows, key=lambda r: r["updated_at"])

    def sessions_with_run_flag(self, flag: str) -> list[dict]:
        rows = self.web.sessions_with_run_flag(flag)
        apps = self._apps()
        if not apps:
            return rows
        for app_id in apps:
            rows += self._call(app_id, "sessions_with_run_flag", flag)
        return sorted(rows, key=lambda r: r["updated_at"])

    def count_sessions(self, user_id: str, *statuses: str) -> int:
        return self.web.count_sessions(user_id, *statuses) + len(
            self._indexed(lambda e: e["owner_id"] == user_id and e["status"] in statuses))

    def count_app_sessions(self, app_id: str, *statuses: str) -> int:
        return len(self._indexed(lambda e: e["app_id"] == app_id and e["status"] in statuses))

    def count_stored_sessions(self, user_id: str, *statuses: str) -> int:
        """`count_sessions` from the rows of each store that may hold one, not from the index, which takes a commit
        only once its callbacks run: what admission caps count (#524). Inside a write, its store's own rows."""
        apps = self._holding(lambda e: e["owner_id"] == user_id and (e["status"] in statuses
                                                                        or e["status"] not in _ENDED))
        return self.web.count_sessions(user_id, *statuses) + sum(
            self._call(app_id, "count_sessions", user_id, *statuses) for app_id in apps)

    def count_stored_app_sessions(self, app_id: str, *statuses: str) -> int:
        """`count_app_sessions` from App `app_id`'s own store, as `count_stored_sessions`; 0 without one."""
        if not self._holding(lambda e: e["app_id"] == app_id):
            return 0
        return self._call(app_id, "count_app_sessions", app_id, *statuses)

    def _holding(self, pred) -> list[str]:
        """The Apps whose stores hold a session matching `pred`, including one whose insert has not committed yet."""
        with self._lock:
            return sorted({e["app_id"] for e in self._sessions.values() if pred(e)})

    def list_sessions(self, limit: int = 50, owner_id: str | None = None, kind: str = "agent",
                      with_app: str | None = None) -> list[dict]:
        rows = self.web.list_sessions(limit, owner_id=owner_id, kind=kind)
        apps = self._reach(None, with_app, lambda e: e["kind"] == kind
                           and (owner_id is None or e["owner_id"] == owner_id))
        if not apps:
            return rows
        for app_id in apps:
            rows += self._call(app_id, "list_sessions", limit, owner_id=owner_id, kind=kind)
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    def search_events(self, fts_query: str, exclude: str = "", max_rows: int = 600, user_id: str | None = None,
                      app_id: str | None = None, session_kind: str | None = "agent",
                      with_app: str | None = None) -> list[dict]:
        rows = self.web.search_events(fts_query, exclude, max_rows, user_id=user_id,
                                      session_kind=session_kind) if self._reads_web(app_id) else []
        apps = self._reach(app_id, with_app, lambda e: (app_id is None or e["app_id"] == app_id)
                           and (user_id is None or e["owner_id"] == user_id)
                           and (session_kind is None or e["kind"] == session_kind))
        if not apps:
            return rows
        for app in apps:
            rows += self._call(app, "search_events", fts_query, exclude, max_rows, user_id=user_id, app_id=app_id,
                               session_kind=session_kind)
        return sorted(rows, key=lambda r: r["rank"])[:max_rows]

    # approvals --------------------------------------------------------------------------------------------------------
    def insert_approval(self, a: dict) -> None:
        self._refuse_erased(a["session_id"])
        app_id = self.app_of(a["session_id"])
        if not app_id:
            return self.web.insert_approval(a)
        def insert(db: Database) -> None:
            db.insert_approval(a)
            token = (db.get_approval(a["id"]) or {}).get("token")
            with self._lock:  # routes from here on, like a new session's id; dropped if the insert rolls back
                self._approvals[a["id"]] = app_id
                if token:
                    self._tokens[token] = app_id
            db.after_rollback(lambda: self._forget_approval(a["id"], token))
        with self._using(app_id, write=True) as db:
            db.write(insert, db)

    def _forget_approval(self, aid: str, token: str | None) -> None:
        with self._lock:
            self._approvals.pop(aid, None)
            self._tokens.pop(token, None)

    def insert_smart_review(self, rid: str, sid: str, approval_id: str, record: dict) -> None:
        return self.for_session(sid).insert_smart_review(rid, sid, approval_id, record)

    def get_approval(self, aid: str) -> dict | None:
        with self._lock:
            app_id = self._approvals.get(aid)
        return self._call(app_id, "get_approval", aid) if app_id else self.web.get_approval(aid)

    def approval_by_token(self, token: str) -> dict | None:
        with self._lock:
            app_id = self._tokens.get(token) if token else None
        return self._call(app_id, "approval_by_token", token) if app_id else self.web.approval_by_token(token)

    def decide_approval(self, aid: str, status: str, note: str = "") -> bool:
        with self._lock:
            app_id = self._approvals.get(aid)
        return self._call(app_id, "decide_approval", aid, status, note) if app_id else \
            self.web.decide_approval(aid, status, note)

    def pending_approvals(self, sid: str | None = None, user_id: str | None = None,
                          with_app: str | None = None) -> list[dict]:
        if sid:
            return self.for_session(sid).pending_approvals(sid, user_id=user_id)
        rows = self.web.pending_approvals(user_id=user_id)
        apps = self._reach(None, with_app, lambda e: e["status"] not in _ENDED
                           and (user_id is None or e["owner_id"] == user_id))
        if not apps:
            return rows
        for app_id in apps:
            rows += self._call(app_id, "pending_approvals", user_id=user_id)
        return sorted(rows, key=lambda r: r["created_at"])

    # startup ----------------------------------------------------------------------------------------------------------
    def _load_index(self) -> None:
        """Read every App store's session ids (and approval ids) without opening the stores."""
        if not self.apps_dir.is_dir():
            return
        for folder in sorted(self.apps_dir.iterdir()):
            path = folder / APP_STORE_FILE
            if folder.name == WEB_APP_ID or not (_APP_ID.fullmatch(folder.name) and path.is_file()):
                continue
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            try:
                sessions = conn.execute("SELECT id, owner_id, kind, status FROM sessions").fetchall()
                approvals = conn.execute("SELECT id, token FROM approvals").fetchall()
            except sqlite3.OperationalError:  # created but never migrated: no sessions yet
                continue
            finally:
                conn.close()
            with self._lock:
                for r in sessions:
                    self._sessions[r["id"]] = {"app_id": folder.name, "owner_id": r["owner_id"] or "",
                                               "kind": r["kind"] or "", "status": r["status"] or "",
                                               "seq": 0, "committed": True}
                for r in approvals:
                    self._approvals[r["id"]] = folder.name
                    self._tokens[r["token"]] = folder.name

    def migrate_app_sessions(self) -> Path | None:
        """Move App sessions still in the main store (written before #330) into their Apps' stores, with everything
        tied to them, then delete them from the main store. Backs up the main store first. Idempotent: once moved
        there is nothing left to do, and a move cut short is redone (the copy overwrites, the delete repeats).
        Returns the backup path, or None when there was nothing to move."""
        rows = self.main.read(lambda: self.main.conn.execute(
            "SELECT id, app_id FROM sessions WHERE app_id != '' ORDER BY created_at").fetchall())
        by_app: dict[str, list[str]] = {}
        for r in rows:
            if _APP_ID.fullmatch(r["app_id"]):
                by_app.setdefault(r["app_id"], []).append(r["id"])
            else:
                log.warning("session %s has App id %r, which cannot name a store; left in the main store",
                            r["id"], r["app_id"])
        if not by_app:
            return None
        backup = Path(self.main.path).parent / "pre-migration" / \
            f"harness-app-stores-{time.strftime('%Y%m%dT%H%M%S')}.sqlite3"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup_sqlite(Path(self.main.path), backup)
        for app_id, sids in by_app.items():
            self._move(app_id, sids)
            log.info("moved %d session(s) of App %s into %s", len(sids), app_id, self.store_path(app_id))
        return backup

    def migrate_web_store(self) -> Database:
        """Move the sessions still in the main store (the owner's and members', #330 decision 4) into Web's store,
        with everything tied to them, and return the store that holds them from now on: Web's, or the main store when
        the move was a dry run (`HARNESS_WEB_STORE_MIGRATION=dry-run`) or was aborted.

        Backs up the main store first (`pre-migration/harness-web-store-<time>.sqlite3` next to it; never deleted
        here). The copy runs in one transaction on Web's store that compares its row counts, table by table, with the
        main store's and rolls back on any difference; only then does one transaction on the main store delete the
        sessions, register Web in the App registry and record the move (`web_store` meta). Idempotent: with nothing
        left in the main store a restart opens Web's store and writes nothing, and a move cut short is redone (the
        copy replaces what an earlier attempt left in Web's store). The search index is rebuilt in Web's store from
        the events. Session files stay where they are: the rows point at them."""
        main = self.main
        dry_run = os.environ.get(WEB_MIGRATION_ENV, "").strip().lower() in ("dry-run", "dry_run", "dryrun")
        migrated = bool(main.get_meta(WEB_STORE_META))
        sids = [r[0] for r in main.read(lambda: main.conn.execute(
            "SELECT id FROM sessions ORDER BY created_at").fetchall())]
        if not sids:
            if dry_run and not migrated:
                log.warning("Web store migration (dry run): no sessions to move; the main store stays in use")
                return main
            web = Database(self.store_path(WEB_APP_ID))
            if not migrated or main.get_api_key(WEB_APP_ID) is None:
                main.write(self._record_web_store, {"sessions": 0, "backup": ""})
            return web
        before = main.read(_session_row_counts, main, sids)
        if dry_run:
            log.warning("Web store migration (dry run): would move %d session(s) into %s: %s; nothing changed",
                        len(sids), self.store_path(WEB_APP_ID), json.dumps(before, sort_keys=True))
            return Database(self.store_path(WEB_APP_ID)) if migrated else main
        backup = _unused(Path(main.path).parent / "pre-migration" /
                         f"harness-web-store-{time.strftime('%Y%m%dT%H%M%S')}.sqlite3")
        backup_sqlite(Path(main.path), backup)
        log.info("Web store migration: moving %d session(s) into %s: %s; main store backed up to %s", len(sids),
                 self.store_path(WEB_APP_ID), json.dumps(before, sort_keys=True), backup)
        web = Database(self.store_path(WEB_APP_ID))
        try:
            self._copy_to_web(web, sids, before)

            def delete() -> None:
                _delete_session_rows(main.conn, sids)
                left = main.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                if left:  # nothing else runs at startup: a session the copy missed
                    raise WebStoreMismatch(f"{left} session(s) still in the main store after the move")
                self._record_web_store({"sessions": len(sids), "backup": str(backup)})
            main.write(delete)
        except Exception:
            log.exception("Web store migration aborted: the sessions stay in the main store, which keeps serving "
                          "them (backup %s)", backup)
            web.close()
            return main
        index = web.read(lambda: web.conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0])
        log.info("Web store migration: moved %d session(s) into %s: %s, %d search index row(s) rebuilt", len(sids),
                 self.store_path(WEB_APP_ID), json.dumps(web.read(_session_row_counts, web, sids), sort_keys=True),
                 index)
        return web

    def _copy_to_web(self, web: Database, sids: list[str], before: dict[str, int]) -> None:
        """Copy sessions `sids` and their rows from the main store into Web's, in one transaction that rolls back
        unless Web's store then holds exactly the main store's rows for them (`before`)."""
        main = self.main

        def read() -> dict[str, tuple[list[str], list[tuple]]]:
            out = {}
            for table in ("sessions", *SESSION_TABLES):
                key, cols, rows = "id" if table == "sessions" else "session_id", None, []
                for part in _chunks(sids):
                    cur = main.conn.execute(f"SELECT * FROM {table} WHERE {key} IN ({_marks(part)})", part)
                    cols = [c[0] for c in cur.description]
                    rows += [tuple(r) for r in cur.fetchall()]
                out[table] = (cols or [], rows)
            return out
        data = main.read(read)

        def insert() -> None:
            _delete_session_rows(web.conn, sids)  # what an attempt cut short left behind
            for table, (cols, values) in data.items():
                if values:
                    web.conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) "
                                         f"VALUES ({','.join('?' * len(cols))})", values)
            web.conn.execute("DELETE FROM meta WHERE key = 'search_index'")
            after = _session_row_counts(web, sids)
            if after != before:
                raise WebStoreMismatch(f"Web store row counts {after} differ from the main store's {before}")
        web.write(insert)
        web._build_search_index()  # the moved events, indexed in Web's store

    def _record_web_store(self, info: dict) -> None:
        """In a main-store transaction: register Web in the App registry and record that its store is in use."""
        self.main.ensure_web_app(WEB_APP_ID)
        self.main.set_meta(WEB_STORE_META, json.dumps({"migrated_at": time.time(), **info}))

    def migrate_app_files(self) -> list[Path]:
        """Move the files of App sessions made before they had their own folder (workspaces, checkpoints,
        transcripts) from the owner's folders into their App's, and point their stored workspace there. Backs up each
        App store it changes first (`<app folder>/pre-migration/`). Idempotent: what has moved is not found again,
        and a move cut short is finished (files first, then the stored paths). Returns the backups made."""
        data_dir = self.apps_dir.parent
        old_ws, old_ckpt, old_tr = data_dir / "workspaces", data_dir / "checkpoints", data_dir / "transcripts"
        by_app: dict[str, list[str]] = {}
        for sid, e in self._indexed(lambda e: True):
            by_app.setdefault(e["app_id"], []).append(sid)
        backups = []
        for app_id, sids in sorted(by_app.items()):
            root = self.store_path(app_id).parent
            stored = dict(self._raw_rows(app_id, "SELECT id, workspace FROM sessions"))
            moves = [(src, root / sub / src.name) for sid in sorted(sids) for sub, src in
                     (("workspaces", old_ws / sid), ("checkpoints", old_ckpt / sid),
                      ("transcripts", old_tr / f"{sid}.md")) if src.exists()]
            repoint = {sid: str(root / "workspaces" / sid) for sid in sids
                       if stored.get(sid) and _same_path(stored[sid], old_ws / sid)}
            if not moves and not repoint:
                continue
            backup = root / "pre-migration" / f"harness-app-files-{time.strftime('%Y%m%dT%H%M%S')}.sqlite3"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup_sqlite(self.store_path(app_id), backup)
            backups.append(backup)
            moved = 0
            for src, dest in moves:
                if dest.exists():
                    log.warning("not moving %s: %s already exists", src, dest)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                moved += 1
            if repoint:
                with self._using(app_id, write=True) as db:
                    db.write(lambda db=db: db.conn.executemany("UPDATE sessions SET workspace = ? WHERE id = ?",
                                                               [(w, sid) for sid, w in repoint.items()]))
            log.info("moved %d file(s) of App %s's sessions into %s and repointed %d workspace(s); backup %s",
                     moved, app_id, root, len(repoint), backup)
        return backups

    def _raw_rows(self, app_id: str, sql: str) -> list[tuple]:
        """Rows read from App `app_id`'s store file without opening the store (no migrations, no writer)."""
        conn = sqlite3.connect(str(self.store_path(app_id)))
        try:
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        except sqlite3.OperationalError:  # created but never migrated
            return []
        finally:
            conn.close()

    def _move(self, app_id: str, sids: list[str]) -> None:
        main, marks = self.main, ",".join("?" * len(sids))

        def copy() -> dict[str, tuple[list[str], list[tuple]]]:
            out = {}
            for table in ("sessions", *SESSION_TABLES):
                key = "id" if table == "sessions" else "session_id"
                cur = main.conn.execute(f"SELECT * FROM {table} WHERE {key} IN ({marks})", sids)
                out[table] = ([c[0] for c in cur.description], [tuple(r) for r in cur.fetchall()])
            return out
        data = main.read(copy)

        with self._using(app_id, write=True) as db:
            def insert() -> None:
                for table, (cols, values) in data.items():
                    if values:
                        db.conn.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                                            f"VALUES ({','.join('?' * len(cols))})", values)
                db.conn.execute("DELETE FROM meta WHERE key = 'search_index'")
            db.write(insert)
            db._build_search_index()  # the moved events, indexed in the App's store

        def delete() -> None:
            for table in (*SESSION_TABLES, "search_index"):
                main.conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", sids)
            main.conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", sids)
        main.write(delete)
