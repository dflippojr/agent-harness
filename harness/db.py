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
]

JSON_COLUMNS = {"context", "run", "totals", "inbox", "args"}


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
                "SELECT id, project, target, model, title, status, stop_reason, created_at, updated_at, totals, "
                "branch, review, workspace_removed FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
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
        return {"seq": cur.lastrowid, "session_id": sid, "ts": ts, "type": type_, "data": data}

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
    def create_api_key(self, name: str) -> tuple[dict, str]:
        import hashlib
        key = "hk-" + secrets.token_urlsafe(32)
        row = {"id": "k-" + secrets.token_hex(4), "name": name, "prefix": key[:10], "created_at": time.time()}
        with self.lock:
            self.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at) VALUES (?, ?, ?, ?, ?)",
                              (row["id"], name, row["prefix"], hashlib.sha256(key.encode()).hexdigest(),
                               row["created_at"]))
        return row, key

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
                "SELECT k.id, k.name, k.prefix, k.created_at, k.last_used_at, k.revoked_at, "
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
