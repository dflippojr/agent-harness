"""SQLite persistence. Every state change is committed before the daemon acts on it, so a restart can resume."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

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
-- Session search (search.py): one row per indexed event.
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    text, session_id UNINDEXED, seq UNINDEXED, kind UNINDEXED, ts UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = [
    # Secret for deciding one approval from a notification button, without a session cookie or JSON body.
    ("approvals", "token", "TEXT NOT NULL DEFAULT ''"),
    # Phase 3: git-backed projects. `review` is '' | merged | pushed | discarded.
    ("sessions", "branch", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "base_branch", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "base_commit", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "review", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "review_detail", "TEXT NOT NULL DEFAULT ''"),
    # Set when cleanup deleted the workspace (or the user discarded it).
    ("sessions", "workspace_removed", "INTEGER NOT NULL DEFAULT 0"),
    # Phase 6e: sessions created through the app API, their registered tools and metadata; key scopes.
    ("sessions", "app_id", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "app_tools", "TEXT NOT NULL DEFAULT '[]'"),
    ("sessions", "app_metadata", "TEXT NOT NULL DEFAULT '{}'"),
    ("api_keys", "scopes", "TEXT NOT NULL DEFAULT 'inference'"),
    ("api_keys", "kind", "TEXT NOT NULL DEFAULT 'device'"),
    # Phase 7d: sessions started by a scheduled job, and the STATUS the job's answer ended with (ok | attention).
    ("sessions", "job_id", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "job_status", "TEXT NOT NULL DEFAULT ''"),
    # Phase 8a: local inference or a hosted CLI session backend.
    ("sessions", "backend", "TEXT NOT NULL DEFAULT 'local'"),
]

JSON_COLUMNS = {"context", "run", "totals", "inbox", "args", "app_tools", "app_metadata"}


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        out[key] = json.loads(out[key])
    if "data" in out and isinstance(out["data"], str):
        out["data"] = json.loads(out["data"])
    return out


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.lock = threading.RLock()
        self._build_search_index()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # sessions
    def insert_session(self, s: dict) -> None:
        cols = list(s)
        values = [json.dumps(s[c]) if c in JSON_COLUMNS else s[c] for c in cols]
        with self.lock:
            self.conn.execute(
                f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", values
            )

    def update_session(self, sid: str, **fields) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
        with self.lock:
            self.conn.execute(f"UPDATE sessions SET {sets} WHERE id = ?", [*values, sid])

    def get_session(self, sid: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone())

    def find_session_ids(self, prefix: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute("SELECT id FROM sessions WHERE id LIKE ?", (prefix + "%",)).fetchall()
        return [r["id"] for r in rows]

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, project, target, model, backend, title, status, stop_reason, created_at, updated_at, totals, "
                "branch, review, workspace_removed, app_id, job_id, job_status FROM sessions "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row(r) for r in rows]

    def sessions_with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM sessions WHERE status IN ({marks}) ORDER BY updated_at", statuses
            ).fetchall()
        return [_row(r) for r in rows]

    # events
    def insert_event(self, sid: str, type_: str, data: dict) -> dict:
        ts = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events (session_id, ts, type, data) VALUES (?, ?, ?, ?)",
                (sid, ts, type_, json.dumps(data)),
            )
            self._index_event(sid, cur.lastrowid, ts, type_, data)
        return {"seq": cur.lastrowid, "session_id": sid, "ts": ts, "type": type_, "data": data}

    # session search (search.py)
    def _index_event(self, sid: str, seq: int, ts: float, type_: str, data: dict) -> None:
        from .search import event_text
        item = event_text(type_, data)
        if item and item[1].strip():
            self.conn.execute("INSERT INTO search_index (text, session_id, seq, kind, ts) VALUES (?, ?, ?, ?, ?)",
                              (item[1], sid, seq, item[0], ts))

    def _build_search_index(self) -> None:
        """Index events written before search existed (or by an older index version). Runs once."""
        from .search import INDEX_VERSION
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'search_index'").fetchone()
            if row and row["value"] == INDEX_VERSION:
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM search_index")
                for r in self.conn.execute("SELECT seq, session_id, ts, type, data FROM events ORDER BY seq").fetchall():
                    self._index_event(r["session_id"], r["seq"], r["ts"], r["type"], json.loads(r["data"]))
                self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('search_index', ?)", (INDEX_VERSION,))
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def search_events(self, fts_query: str, exclude: str = "", max_rows: int = 600) -> list[dict]:
        sql = ("SELECT session_id, seq, kind, ts, bm25(search_index) AS rank, "
               "snippet(search_index, 0, char(2), char(3), '…', 16) AS snippet "
               "FROM search_index WHERE search_index MATCH ?")
        params: list = [fts_query]
        if exclude:
            sql += " AND session_id != ?"
            params.append(exclude)
        with self.lock:
            try:
                rows = self.conn.execute(sql + " ORDER BY rank LIMIT ?", [*params, max_rows]).fetchall()
            except sqlite3.OperationalError:  # a query FTS5 can't parse despite the quoting: no results
                return []
        return [dict(r) for r in rows]

    def session_brief(self, sid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT id, project, target, title, status, created_at, updated_at, branch, "
                                    "review, substr(answer, 1, 400) AS answer FROM sessions WHERE id = ?",
                                    (sid,)).fetchone()
        return dict(row) if row else None

    def events(self, sid: str, after: int = 0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq", (sid, after)
            ).fetchall()
        return [_row(r) for r in rows]

    def last_event_seq(self, sid: str) -> int:
        with self.lock:
            row = self.conn.execute("SELECT MAX(seq) AS seq FROM events WHERE session_id = ?", (sid,)).fetchone()
        return row["seq"] or 0

    # approvals
    def insert_approval(self, a: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO approvals (id, session_id, tool_call_id, tool, args, reason, detail, status, created_at, "
                "token) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (a["id"], a["session_id"], a["tool_call_id"], a["tool"], json.dumps(a["args"]),
                 a["reason"], a.get("detail", ""), time.time(), secrets.token_urlsafe(24)),
            )

    def approval_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone())

    def get_approval(self, aid: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone())

    def approval_for_call(self, sid: str, call_id: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(
                "SELECT * FROM approvals WHERE session_id = ? AND tool_call_id = ? ORDER BY created_at DESC",
                (sid, call_id),
            ).fetchone())

    def pending_approvals(self, sid: str | None = None) -> list[dict]:
        query = "SELECT * FROM approvals WHERE status = 'pending'"
        params: tuple = ()
        if sid:
            query += " AND session_id = ?"
            params = (sid,)
        with self.lock:
            return [_row(r) for r in self.conn.execute(query + " ORDER BY created_at", params).fetchall()]

    def approvals(self, sid: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM approvals WHERE session_id = ? ORDER BY created_at", (sid,)).fetchall()
        return [_row(r) for r in rows]

    # scheduled jobs
    def list_jobs(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM jobs ORDER BY name").fetchall()]

    def get_job(self, jid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return dict(row) if row else None

    def insert_job(self, job: dict) -> None:
        now = time.time()
        row = {**job, "created_at": now, "updated_at": now}
        with self.lock:
            self.conn.execute(f"INSERT INTO jobs ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                              [int(v) if isinstance(v, bool) else v for v in row.values()])

    def update_job(self, jid: str, **fields) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                              [*(int(v) if isinstance(v, bool) else v for v in fields.values()), jid])

    def delete_job(self, jid: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM jobs WHERE id = ?", (jid,)).rowcount == 1

    def job_sessions(self, jid: str, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, title, status, stop_reason, job_status, created_at, updated_at, "
                                     "substr(answer, 1, 300) AS answer FROM sessions WHERE job_id = ? "
                                     "ORDER BY created_at DESC LIMIT ?", (jid, limit)).fetchall()
        return [dict(r) for r in rows]

    # templates
    def list_templates(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM templates ORDER BY name").fetchall()]

    def get_template(self, tid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM templates WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None

    def upsert_template(self, t: dict) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO templates (id, name, project, model, prompt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "project = excluded.project, model = excluded.model, prompt = excluded.prompt, "
                "updated_at = excluded.updated_at",
                (t["id"], t["name"], t["project"], t.get("model") or "", t["prompt"], now, now),
            )

    def delete_template(self, tid: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM templates WHERE id = ?", (tid,)).rowcount == 1

    # images
    def insert_image(self, job: dict) -> None:
        cols = ["id", "session_id", "source", "prompt", "model", "aspect_ratio", "width", "height", "seed"]
        with self.lock:
            self.conn.execute(f"INSERT INTO images ({','.join(cols)}, status, created_at) VALUES "
                              f"({','.join('?' * len(cols))}, 'queued', ?)", [job[c] for c in cols] + [time.time()])

    def update_image(self, iid: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE images SET {sets} WHERE id = ?", [*fields.values(), iid])

    def get_image(self, iid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM images WHERE id = ?", (iid,)).fetchone()
        return dict(row) if row else None

    def list_images(self, limit: int = 60, status: tuple = ()) -> list[dict]:
        query, params = "SELECT * FROM images", []
        if status:
            query += f" WHERE status IN ({','.join('?' * len(status))})"
            params = list(status)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY created_at DESC LIMIT ?", [*params, limit]).fetchall()
        return [dict(r) for r in rows]

    # inference endpoint keys and request log
    def create_api_key(self, name: str, scopes: str = "inference", kind: str = "device") -> tuple[dict, str]:
        import hashlib
        key = ("ha-" if kind == "app" else "hk-") + secrets.token_urlsafe(32)
        row = {"id": "k-" + secrets.token_hex(4), "name": name, "prefix": key[:10], "created_at": time.time(),
               "scopes": scopes, "kind": kind}
        with self.lock:
            self.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at, scopes, kind) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (row["id"], name, row["prefix"], hashlib.sha256(key.encode()).hexdigest(),
                               row["created_at"], scopes, kind))
        return row, key

    # app tool calls
    def insert_app_tool_call(self, sid: str, call_id: str, name: str, args: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO app_tool_calls (session_id, call_id, name, args, status, "
                              "created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                              (sid, call_id, name, json.dumps(args), time.time()))

    def get_app_tool_call(self, sid: str, call_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_tool_calls WHERE session_id = ? AND call_id = ?",
                                    (sid, call_id)).fetchone()
        return _row(row)

    def app_tool_calls(self, sid: str, status: str | None = None) -> list[dict]:
        query, params = "SELECT * FROM app_tool_calls WHERE session_id = ?", [sid]
        if status:
            query += " AND status = ?"
            params.append(status)
        with self.lock:
            return [_row(r) for r in self.conn.execute(query + " ORDER BY created_at", params).fetchall()]

    def finish_app_tool_call(self, sid: str, call_id: str, status: str, output: str, ok: bool) -> bool:
        with self.lock:
            return self.conn.execute(
                "UPDATE app_tool_calls SET status = ?, output = ?, ok = ?, finished_at = ? "
                "WHERE session_id = ? AND call_id = ? AND status = 'pending'",
                (status, output, int(ok), time.time(), sid, call_id)).rowcount == 1

    def api_key_by_secret(self, key: str) -> dict | None:
        import hashlib
        if not key:
            return None
        with self.lock:
            row = self.conn.execute("SELECT * FROM api_keys WHERE hash = ? AND revoked_at IS NULL",
                                    (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()
        return dict(row) if row else None

    def list_api_keys(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT k.id, k.name, k.prefix, k.kind, k.scopes, k.created_at, k.last_used_at, k.revoked_at, "
                "(SELECT COUNT(*) FROM endpoint_requests r WHERE r.key_id = k.id) AS requests "
                "FROM api_keys k ORDER BY k.created_at").fetchall()
        return [dict(r) for r in rows]

    def revoke_api_key(self, kid: str) -> bool:
        with self.lock:
            return self.conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                     (time.time(), kid)).rowcount == 1

    def log_endpoint_request(self, r: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO endpoint_requests (key_id, route, model, stream, status, prompt_tokens, "
                "completion_tokens, wait_ms, total_ms, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["key_id"], r["route"], r["model"], int(r["stream"]), r["status"], r.get("prompt_tokens", 0),
                 r.get("completion_tokens", 0), r.get("wait_ms", 0), r.get("total_ms", 0), time.time()))
            self.conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (time.time(), r["key_id"]))

    def decide_approval(self, aid: str, status: str, note: str = "") -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE approvals SET status = ?, note = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (status, note, time.time(), aid),
            )
        return cur.rowcount == 1
