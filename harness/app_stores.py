"""Per-App data stores (#330).

Each App's sessions live in their own SQLite file, `<data_dir>/apps/<app_id>/harness.sqlite3`, which runs the same
versioned migrations (#256) and has its own writer thread and read pool (#294). The main store keeps everything else:
the App registry (`api_keys`), per-App usage and error counters, provider credentials, and the owner's and members'
sessions.

`SessionStores` stands in for the main `Database`. A call about one session (`get_session(sid)`, `insert_event(sid,
...)`, ...) runs on that session's store; anything else runs on the main store. A transaction (`write`/`awrite`) that
touches a session must run on that session's writer: `db.for_session(sid).write(fn)`, or `db.for_app(app_id)` for a
session not created yet. An App store opens on first use and closes after `APP_STORE_IDLE_SECONDS` without one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
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
                  "secret_dismissals", "checkpoints")
# Database methods whose first argument is a session id: they run on that session's store.
_BY_SESSION = frozenset({
    "get_session", "get_session_for_user", "session_brief", "add_checkpoint", "checkpoints",
    "show_checkpoints_through", "delete_checkpoints", "put_artifact", "read_artifact", "full_artifact",
    "add_review_comment", "list_review_comments", "add_secret_dismissal", "secret_dismissals",
    "delete_review_comments", "insert_event", "events", "pushed_heads", "last_event_seq", "approval_for_call",
    "approvals", "insert_app_tool_call", "get_app_tool_call", "app_tool_calls", "finish_app_tool_call",
})
# Session columns the in-memory index of App sessions mirrors.
_INDEXED = ("app_id", "owner_id", "kind", "status")
_ENDED = ("done", "failed", "cancelled")


def app_dir(data_dir: Path, app_id: str) -> Path:
    """An App's data folder."""
    if not _APP_ID.fullmatch(app_id or ""):
        raise ValueError(f"not an App id: {app_id!r}")
    return Path(data_dir) / "apps" / app_id


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
    Ids not in the index belong to the main store. Inside an App store's transaction the main store may be called
    (its writer is waited for), never the reverse and never another App's store, so two writers can never wait on
    each other: such a write raises instead of deadlocking.

    Lists, searches and sweeps that span sessions (list_sessions, search_events, find_session_ids,
    sessions_with_status, sessions_with_run_flag, count_sessions, pending_approvals) cover the main store plus the
    App stores the index says hold a match, so what the owner sees is unchanged in this stage; narrowing the owner's
    view to metadata (#330 decision 3) comes later. Group, job and chat lists and the metrics read the main store
    only: App sessions have none of those.
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
        self._closed = False
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        self.migrate_app_sessions()
        self._load_index()

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
        self.main.close()

    # routing ----------------------------------------------------------------------------------------------------------
    def for_app(self, app_id: str):
        """App `app_id`'s store (created on first use), or the main store for "" (owner and member sessions)."""
        if not app_id:
            return self.main
        with self._lock:
            proxy = self._proxies.get(app_id)
            if proxy is None:
                self.store_path(app_id)  # refuses a malformed id
                proxy = self._proxies[app_id] = _AppStore(self, app_id)
            return proxy

    def for_session(self, sid: str):
        """The store that holds session `sid`."""
        with self._lock:
            entry = self._sessions.get(sid)
        return self.for_app(entry["app_id"]) if entry else self.main

    def app_of(self, sid: str) -> str:
        with self._lock:
            entry = self._sessions.get(sid)
        return entry["app_id"] if entry else ""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in _BY_SESSION:
            def by_session(sid, *args, **kwargs):
                return getattr(self.for_session(sid), name)(sid, *args, **kwargs)
            return by_session
        return getattr(self.main, name)

    def _indexed(self, pred) -> list[tuple[str, dict]]:
        with self._lock:
            return [(sid, e) for sid, e in self._sessions.items() if pred(e)]

    def _apps(self, pred=lambda e: True) -> list[str]:
        return sorted({e["app_id"] for _, e in self._indexed(pred)})

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
            if app_id and not taken:
                self._sessions[sid] = {k: s.get(k) or "" for k in _INDEXED} | {"app_id": app_id}
        if taken or (app_id and self.main.get_session(sid) is not None):
            if app_id and not taken:
                with self._lock:
                    self._sessions.pop(sid, None)
            raise sqlite3.IntegrityError(f"session id {sid} is already in use")
        if not app_id:
            return self.main.insert_session(s)
        try:
            with self._using(app_id, write=True) as db:
                db.insert_session(s)
                row = db.get_session(sid)
        except BaseException:
            with self._lock:
                self._sessions.pop(sid, None)
            raise
        with self._lock:  # the stored values, column defaults included
            self._sessions[sid].update({k: (row or s).get(k) or "" for k in _INDEXED if k != "app_id"})

    def update_session(self, sid: str, **fields) -> None:
        with self._lock:
            entry = self._sessions.get(sid)
        if entry is None:
            return self.main.update_session(sid, **fields)
        self._call(entry["app_id"], "update_session", sid, **fields)
        with self._lock:
            entry.update({k: fields[k] for k in _INDEXED if k in fields and k != "app_id"})

    def delete_session(self, sid: str) -> None:
        with self._lock:
            entry = self._sessions.get(sid)
        if entry is None:
            return self.main.delete_session(sid)
        self._call(entry["app_id"], "delete_session", sid)
        with self._lock:
            self._sessions.pop(sid, None)

    def find_session_ids(self, prefix: str, user_id: str | None = None, app_id: str | None = None,
                         kind: str | None = None) -> list[str]:
        ids = self.main.find_session_ids(prefix, user_id=user_id, app_id=app_id, kind=kind)
        like = _like(prefix)
        return ids + [sid for sid, _ in self._indexed(
            lambda e: (app_id is None or e["app_id"] == app_id) and (user_id is None or e["owner_id"] == user_id)
            and (kind is None or e["kind"] == kind))
            if like.fullmatch(sid)]

    def sessions_with_status(self, *statuses: str, user_id: str | None = None) -> list[dict]:
        rows = self.main.sessions_with_status(*statuses, user_id=user_id)
        apps = self._apps(lambda e: e["status"] in statuses and (user_id is None or e["owner_id"] == user_id))
        if not apps:
            return rows
        for app_id in apps:
            rows += self._call(app_id, "sessions_with_status", *statuses, user_id=user_id)
        return sorted(rows, key=lambda r: r["updated_at"])

    def sessions_with_run_flag(self, flag: str) -> list[dict]:
        rows = self.main.sessions_with_run_flag(flag)
        apps = self._apps()
        if not apps:
            return rows
        for app_id in apps:
            rows += self._call(app_id, "sessions_with_run_flag", flag)
        return sorted(rows, key=lambda r: r["updated_at"])

    def count_sessions(self, user_id: str, *statuses: str) -> int:
        return self.main.count_sessions(user_id, *statuses) + len(
            self._indexed(lambda e: e["owner_id"] == user_id and e["status"] in statuses))

    def list_sessions(self, limit: int = 50, owner_id: str | None = None, kind: str = "agent") -> list[dict]:
        rows = self.main.list_sessions(limit, owner_id=owner_id, kind=kind)
        apps = self._apps(lambda e: e["kind"] == kind and (owner_id is None or e["owner_id"] == owner_id))
        if not apps:
            return rows
        for app_id in apps:
            rows += self._call(app_id, "list_sessions", limit, owner_id=owner_id, kind=kind)
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    def search_events(self, fts_query: str, exclude: str = "", max_rows: int = 600, user_id: str | None = None,
                      app_id: str | None = None, session_kind: str | None = "agent") -> list[dict]:
        """One App's store when `app_id` is given, every store otherwise."""
        rows = self.main.search_events(fts_query, exclude, max_rows, user_id=user_id, app_id=app_id,
                                       session_kind=session_kind)
        apps = self._apps(lambda e: (app_id is None or e["app_id"] == app_id)
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
        app_id = self.app_of(a["session_id"])
        if not app_id:
            return self.main.insert_approval(a)
        with self._using(app_id, write=True) as db:
            db.insert_approval(a)
            row = db.get_approval(a["id"])
        with self._lock:
            self._approvals[a["id"]] = app_id
            if row:
                self._tokens[row["token"]] = app_id

    def insert_smart_review(self, rid: str, sid: str, approval_id: str, record: dict) -> None:
        return self.for_session(sid).insert_smart_review(rid, sid, approval_id, record)

    def get_approval(self, aid: str) -> dict | None:
        with self._lock:
            app_id = self._approvals.get(aid)
        return self._call(app_id, "get_approval", aid) if app_id else self.main.get_approval(aid)

    def approval_by_token(self, token: str) -> dict | None:
        with self._lock:
            app_id = self._tokens.get(token) if token else None
        return self._call(app_id, "approval_by_token", token) if app_id else self.main.approval_by_token(token)

    def decide_approval(self, aid: str, status: str, note: str = "") -> bool:
        with self._lock:
            app_id = self._approvals.get(aid)
        return self._call(app_id, "decide_approval", aid, status, note) if app_id else \
            self.main.decide_approval(aid, status, note)

    def pending_approvals(self, sid: str | None = None, user_id: str | None = None) -> list[dict]:
        if sid:
            return self.for_session(sid).pending_approvals(sid, user_id=user_id)
        rows = self.main.pending_approvals(user_id=user_id)
        apps = self._apps(lambda e: e["status"] not in _ENDED and (user_id is None or e["owner_id"] == user_id))
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
            if not (_APP_ID.fullmatch(folder.name) and path.is_file()):
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
                                               "kind": r["kind"] or "", "status": r["status"] or ""}
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
