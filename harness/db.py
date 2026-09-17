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
CREATE TABLE IF NOT EXISTS runner_pairing_codes ( -- owner-approved Mac client + runner bootstrap codes
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
    # Issue #57: human-owned Control Center data. v1 has one stable owner; guests own nothing.
    ("sessions", "owner_id", "TEXT NOT NULL DEFAULT 'owner'"),
    ("api_keys", "scopes", "TEXT NOT NULL DEFAULT 'inference'"),
    ("api_keys", "kind", "TEXT NOT NULL DEFAULT 'device'"),
    ("api_keys", "origins", "TEXT NOT NULL DEFAULT '[]'"),
    # Phase 7d: sessions started by a scheduled job, and the STATUS the job's answer ended with (ok | attention).
    ("sessions", "job_id", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "job_status", "TEXT NOT NULL DEFAULT ''"),
    # Phase 8a: local inference or a hosted CLI session backend.
    ("sessions", "backend", "TEXT NOT NULL DEFAULT 'local'"),
    ("jobs", "backend", "TEXT NOT NULL DEFAULT 'local'"),
    ("templates", "backend", "TEXT NOT NULL DEFAULT 'local'"),
    # UI refresh: explicit image resolution while preserving model-native defaults for old callers.
    ("images", "resolution", "TEXT NOT NULL DEFAULT 'auto'"),
    # Issue #86: durable image archive state. The canonical digest detects later source corruption.
    ("images", "sha256", "TEXT NOT NULL DEFAULT ''"),
    ("images", "archive_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("images", "archived_at", "REAL"),
    ("images", "archive_error", "TEXT NOT NULL DEFAULT ''"),
    ("images", "archive_deleted_at", "REAL"),
    # Issue #29: usage attribution names the credential class, never the key or its file reference.
    ("usage", "credential_source", "TEXT NOT NULL DEFAULT 'subscription'"),
]

JSON_COLUMNS = {"context", "run", "totals", "inbox", "args", "app_tools", "app_metadata", "data", "origins",
                "models"}


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

    def list_sessions(self, limit: int = 50, owner_id: str | None = None) -> list[dict]:
        where = " WHERE owner_id = ?" if owner_id is not None else ""
        params = (owner_id, limit) if owner_id is not None else (limit,)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, project, target, model, backend, title, status, stop_reason, created_at, updated_at, totals, "
                "branch, review, workspace_removed, app_id, job_id, job_status, owner_id FROM sessions" + where +
                " ORDER BY created_at DESC LIMIT ?", params
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

    # small user-facing preferences (profile emoji, later display choices)
    def get_meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # hosted backend usage and limits
    def set_backend_usage(self, backend: str, data: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO backend_usage (backend, data, updated_at) VALUES (?, ?, ?) "
                              "ON CONFLICT(backend) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                              (backend, json.dumps(data), time.time()))

    def get_backend_usage(self, backend: str) -> dict:
        with self.lock:
            row = self.conn.execute("SELECT * FROM backend_usage WHERE backend = ?", (backend,)).fetchone()
        return _row(row) or {"backend": backend, "data": {}, "updated_at": None}

    def record_usage(self, backend: str, sid: str, app_id: str, prompt_tokens: int,
                     completion_tokens: int, cost_usd: float, billing: str,
                     credential_source: str = "subscription") -> None:
        with self.lock:
            self.conn.execute("INSERT INTO usage (backend, session_id, app_id, prompt_tokens, completion_tokens, "
                              "cost_usd, billing, credential_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (backend, sid, app_id, prompt_tokens, completion_tokens, cost_usd, billing,
                               credential_source, time.time()))

    def usage_tally(self, backend: str, since: float, app_id: str | None = None) -> dict:
        app_clause, params = (" AND app_id = ?", [backend, since, app_id]) if app_id is not None else ("", [backend, since])
        with self.lock:
            row = self.conn.execute("SELECT COALESCE(SUM(requests),0) requests, "
                                    "COALESCE(SUM(prompt_tokens),0) prompt_tokens, "
                                    "COALESCE(SUM(completion_tokens),0) completion_tokens, "
                                    "COALESCE(SUM(cost_usd),0) cost_usd FROM usage "
                                    f"WHERE backend = ? AND created_at >= ?{app_clause}", params).fetchone()
        return dict(row)

    def usage_by_source(self, backend: str, since: float, app_id: str | None = None) -> dict[str, dict]:
        app_clause, params = (" AND app_id = ?", [backend, since, app_id]) if app_id is not None else ("", [backend, since])
        with self.lock:
            rows = self.conn.execute(
                "SELECT credential_source, COALESCE(SUM(requests),0) requests, "
                "COALESCE(SUM(prompt_tokens),0) prompt_tokens, COALESCE(SUM(completion_tokens),0) completion_tokens, "
                "COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE backend = ? AND created_at >= ?"
                f"{app_clause} GROUP BY credential_source", params).fetchall()
        return {row["credential_source"]: {k: row[k] for k in
                                            ("requests", "prompt_tokens", "completion_tokens", "cost_usd")}
                for row in rows}

    # Owner-managed per-app provider policy. Secret values and file paths never enter this database.
    def set_app_provider_credential(self, app_id: str, backend: str, secret_ref: str, policy: str,
                                    models: list[str]) -> dict:
        now, cid = time.time(), "pc-" + secrets.token_hex(5)
        with self.tx():
            self.conn.execute("UPDATE app_provider_credentials SET revoked_at = ? "
                              "WHERE app_id = ? AND backend = ? AND revoked_at IS NULL", (now, app_id, backend))
            self.conn.execute("INSERT INTO app_provider_credentials "
                              "(id, app_id, backend, secret_ref, policy, models, created_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (cid, app_id, backend, secret_ref, policy, json.dumps(models), now))
        return self.app_provider_credential(app_id, backend)

    def app_provider_credential(self, app_id: str, backend: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_provider_credentials WHERE app_id = ? AND backend = ? "
                                    "AND revoked_at IS NULL", (app_id, backend)).fetchone()
        return _row(row)

    def app_provider_credential_by_id(self, cid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_provider_credentials WHERE id = ?", (cid,)).fetchone()
        return _row(row)

    def app_provider_managed(self, app_id: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM app_provider_credentials WHERE app_id = ? LIMIT 1",
                                     (app_id,)).fetchone() is not None

    def list_app_provider_credentials(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT c.*, k.name AS app_name FROM app_provider_credentials c "
                "LEFT JOIN api_keys k ON k.id = c.app_id ORDER BY c.created_at DESC").fetchall()
        return [_row(row) for row in rows]

    def revoke_app_provider_credential(self, cid: str) -> bool:
        with self.lock:
            return self.conn.execute("UPDATE app_provider_credentials SET revoked_at = ? "
                                     "WHERE id = ? AND revoked_at IS NULL", (time.time(), cid)).rowcount == 1

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
                "INSERT INTO templates (id, name, project, backend, model, prompt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "project = excluded.project, backend = excluded.backend, model = excluded.model, prompt = excluded.prompt, "
                "updated_at = excluded.updated_at",
                (t["id"], t["name"], t["project"], t.get("backend") or "local", t.get("model") or "",
                 t["prompt"], now, now),
            )

    def delete_template(self, tid: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM templates WHERE id = ?", (tid,)).rowcount == 1

    # images
    def insert_image(self, job: dict) -> None:
        cols = ["id", "session_id", "source", "prompt", "model", "aspect_ratio", "resolution", "width", "height", "seed"]
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

    def images_for_archive(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM images WHERE status = 'done' ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    # inference endpoint keys and request log
    def create_api_key(self, name: str, scopes: str = "inference", kind: str = "device",
                       origins: list[str] | None = None) -> tuple[dict, str]:
        import hashlib
        prefix = {"app": "ha-", "owner": "ho-"}.get(kind, "hk-")
        key = prefix + secrets.token_urlsafe(32)
        row = {"id": "k-" + secrets.token_hex(4), "name": name, "prefix": key[:10], "created_at": time.time(),
               "scopes": scopes, "kind": kind, "origins": origins or []}
        with self.lock:
            self.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at, scopes, kind, origins) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (row["id"], name, row["prefix"], hashlib.sha256(key.encode()).hexdigest(),
                               row["created_at"], scopes, kind, json.dumps(row["origins"])))
        return row, key

    # browser pairing and EventSource tickets
    def create_pairing_code(self, name: str, origin: str, scopes: str, ttl_seconds: int) -> tuple[dict, str]:
        import hashlib
        now = time.time()
        code = "hp-" + secrets.token_urlsafe(18)
        row = {"id": "p-" + secrets.token_hex(4), "name": name, "origin": origin, "scopes": scopes,
               "created_at": now, "expires_at": now + ttl_seconds, "used_at": None, "key_id": ""}
        with self.lock:
            self.conn.execute("INSERT INTO pairing_codes (id, hash, name, origin, scopes, created_at, expires_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (row["id"], hashlib.sha256(code.encode()).hexdigest(), name, origin, scopes, now,
                               row["expires_at"]))
        return row, code

    def list_pairing_codes(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, name, origin, scopes, created_at, expires_at, used_at, key_id "
                                     "FROM pairing_codes ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def revoke_pairing_code(self, pid: str) -> bool:
        with self.lock:
            return self.conn.execute("UPDATE pairing_codes SET expires_at = ? WHERE id = ? AND used_at IS NULL "
                                     "AND expires_at > ?", (time.time(), pid, time.time())).rowcount == 1

    def pairing_origin_active(self, origin: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM pairing_codes WHERE origin = ? AND used_at IS NULL "
                                    "AND expires_at > ? LIMIT 1", (origin, time.time())).fetchone()
        return row is not None

    def redeem_pairing_code(self, code: str, origin: str) -> tuple[dict | None, str, str]:
        """Atomically redeem a bootstrap code. Returns (key row, secret, error)."""
        import hashlib
        digest, now = hashlib.sha256(code.encode()).hexdigest(), time.time()
        with self.tx():
            pairing = self.conn.execute("SELECT * FROM pairing_codes WHERE hash = ?", (digest,)).fetchone()
            if pairing is None:
                return None, "", "invalid pairing code"
            if pairing["used_at"] is not None:
                return None, "", "pairing code already used"
            if pairing["expires_at"] <= now:
                return None, "", "pairing code expired"
            if pairing["origin"] != origin:
                return None, "", "pairing code is not approved for this origin"
            secret = "ha-" + secrets.token_urlsafe(32)
            key = {"id": "k-" + secrets.token_hex(4), "name": pairing["name"], "prefix": secret[:10],
                   "created_at": now, "scopes": pairing["scopes"], "kind": "app", "origins": [origin]}
            self.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at, scopes, kind, origins) "
                              "VALUES (?, ?, ?, ?, ?, ?, 'app', ?)",
                              (key["id"], key["name"], key["prefix"], hashlib.sha256(secret.encode()).hexdigest(),
                               now, key["scopes"], json.dumps(key["origins"])))
            self.conn.execute("UPDATE pairing_codes SET used_at = ?, key_id = ? WHERE id = ?",
                              (now, key["id"], pairing["id"]))
        return key, secret, ""

    # Native Mac client pairing is separate from browser origin pairing. The code authorizes one owner CLI token;
    # the runner token remains in its configured owner file and never enters SQLite.
    def create_runner_pairing_code(self, name: str, runner: str, ttl_seconds: int) -> tuple[dict, str]:
        import hashlib
        now = time.time()
        code = "hrp-" + secrets.token_urlsafe(18)
        row = {"id": "rp-" + secrets.token_hex(4), "name": name, "runner": runner,
               "created_at": now, "expires_at": now + ttl_seconds, "used_at": None, "key_id": ""}
        with self.lock:
            self.conn.execute("INSERT INTO runner_pairing_codes "
                              "(id, hash, name, runner, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (row["id"], hashlib.sha256(code.encode()).hexdigest(), name, runner, now,
                               row["expires_at"]))
        return row, code

    def list_runner_pairing_codes(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, name, runner, created_at, expires_at, used_at, key_id "
                "FROM runner_pairing_codes ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    def revoke_runner_pairing_code(self, pid: str) -> bool:
        now = time.time()
        with self.lock:
            return self.conn.execute("UPDATE runner_pairing_codes SET expires_at = ? WHERE id = ? "
                                     "AND used_at IS NULL AND expires_at > ?", (now, pid, now)).rowcount == 1

    def redeem_runner_pairing_code(self, code: str) -> tuple[dict | None, dict | None, str, str]:
        """Atomically redeem a native-client code. Returns (pairing, owner key, secret, error)."""
        import hashlib
        digest, now = hashlib.sha256(code.encode()).hexdigest(), time.time()
        with self.tx():
            pairing = self.conn.execute("SELECT * FROM runner_pairing_codes WHERE hash = ?", (digest,)).fetchone()
            if pairing is None:
                return None, None, "", "invalid runner pairing code"
            if pairing["used_at"] is not None:
                return None, None, "", "runner pairing code already used"
            if pairing["expires_at"] <= now:
                return None, None, "", "runner pairing code expired"
            secret = "ho-" + secrets.token_urlsafe(32)
            key = {"id": "k-" + secrets.token_hex(4), "name": pairing["name"], "prefix": secret[:10],
                   "created_at": now, "scopes": "admin", "kind": "owner", "origins": []}
            self.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at, scopes, kind, origins) "
                              "VALUES (?, ?, ?, ?, ?, 'admin', 'owner', '[]')",
                              (key["id"], key["name"], key["prefix"], hashlib.sha256(secret.encode()).hexdigest(),
                               now))
            self.conn.execute("UPDATE runner_pairing_codes SET used_at = ?, key_id = ? WHERE id = ?",
                              (now, key["id"], pairing["id"]))
        return dict(pairing), key, secret, ""

    def origin_allowed(self, origin: str, kind: str | None = None) -> bool:
        query = "SELECT origins FROM api_keys WHERE revoked_at IS NULL"
        params: tuple = ()
        if kind is not None:
            query += " AND kind = ?"
            params = (kind,)
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return any(origin in (json.loads(r["origins"] or "[]")) for r in rows)

    def create_stream_ticket(self, key_id: str, session_id: str, origin: str,
                             ttl_seconds: int = 60) -> tuple[str, float]:
        import hashlib
        now = time.time()
        ticket = "hs-" + secrets.token_urlsafe(24)
        with self.lock:
            self.conn.execute("DELETE FROM stream_tickets WHERE expires_at <= ?", (now,))
            self.conn.execute("INSERT INTO stream_tickets "
                              "(hash, key_id, session_id, origin, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (hashlib.sha256(ticket.encode()).hexdigest(), key_id, session_id, origin, now,
                               now + ttl_seconds))
        return ticket, now + ttl_seconds

    def stream_ticket_key(self, ticket: str, session_id: str, origin: str) -> dict | None:
        import hashlib
        if not ticket or not origin:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT k.* FROM stream_tickets t JOIN api_keys k ON k.id = t.key_id "
                "WHERE t.hash = ? AND t.session_id = ? AND t.origin = ? AND t.expires_at > ? "
                "AND k.revoked_at IS NULL",
                (hashlib.sha256(ticket.encode()).hexdigest(), session_id, origin, time.time())).fetchone()
        return _row(row)

    def stream_ticket_origin_active(self, ticket: str, origin: str) -> bool:
        import hashlib
        if not ticket or not origin:
            return False
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM stream_tickets t JOIN api_keys k ON k.id = t.key_id "
                "WHERE t.hash = ? AND t.origin = ? AND t.expires_at > ? AND k.revoked_at IS NULL LIMIT 1",
                (hashlib.sha256(ticket.encode()).hexdigest(), origin, time.time())).fetchone()
        return row is not None

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
        return _row(row)

    def get_api_key(self, kid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM api_keys WHERE id = ?", (kid,)).fetchone()
        return _row(row)

    def list_api_keys(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT k.id, k.name, k.prefix, k.kind, k.scopes, k.origins, k.created_at, k.last_used_at, k.revoked_at, "
                "(SELECT COUNT(*) FROM endpoint_requests r WHERE r.key_id = k.id) AS requests "
                "FROM api_keys k ORDER BY k.created_at").fetchall()
        return [_row(r) for r in rows]

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
