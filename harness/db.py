"""SQLite persistence. Every state change is committed before the daemon acts on it, so a restart can resume."""

from __future__ import annotations

import json
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
"""

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
                "SELECT id, project, target, model, title, status, stop_reason, created_at, updated_at, totals "
                "FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
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
                "INSERT INTO approvals (id, session_id, tool_call_id, tool, args, reason, detail, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (a["id"], a["session_id"], a["tool_call_id"], a["tool"], json.dumps(a["args"]),
                 a["reason"], a.get("detail", ""), time.time()),
            )

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

    def decide_approval(self, aid: str, status: str, note: str = "") -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE approvals SET status = ?, note = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (status, note, time.time(), aid),
            )
        return cur.rowcount == 1
