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
"""

# Column definitions reused across the migration table below.
TEXT_EMPTY = "TEXT NOT NULL DEFAULT ''"
TEXT_LOCAL = "TEXT NOT NULL DEFAULT 'local'"

# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = [
    # Secret for deciding one approval from a notification button, without a session cookie or JSON body.
    ("approvals", "token", TEXT_EMPTY),
    # Phase 3: git-backed projects. `review` is '' | merged | pushed | discarded.
    ("sessions", "branch", TEXT_EMPTY),
    ("sessions", "base_branch", TEXT_EMPTY),
    ("sessions", "base_commit", TEXT_EMPTY),
    ("sessions", "review", TEXT_EMPTY),
    ("sessions", "review_detail", TEXT_EMPTY),
    # Set when cleanup deleted the workspace (or the user discarded it).
    ("sessions", "workspace_removed", "INTEGER NOT NULL DEFAULT 0"),
    # Phase 6e: sessions created through the app API, their registered tools and metadata; key scopes.
    ("sessions", "app_id", TEXT_EMPTY),
    ("sessions", "app_tools", "TEXT NOT NULL DEFAULT '[]'"),
    ("sessions", "app_metadata", "TEXT NOT NULL DEFAULT '{}'"),
    # Issue #57: human-owned Agent Harness Web data. v1 has one stable owner; guests own nothing.
    ("sessions", "owner_id", "TEXT NOT NULL DEFAULT 'owner'"),
    ("api_keys", "scopes", "TEXT NOT NULL DEFAULT 'inference'"),
    ("api_keys", "kind", "TEXT NOT NULL DEFAULT 'device'"),
    ("api_keys", "origins", "TEXT NOT NULL DEFAULT '[]'"),
    # Phase 7d: sessions started by a scheduled job, and the STATUS the job's answer ended with (ok | attention).
    ("sessions", "job_id", TEXT_EMPTY),
    ("sessions", "job_status", TEXT_EMPTY),
    # Phase 8a: local inference or a hosted CLI session backend.
    ("sessions", "backend", TEXT_LOCAL),
    ("jobs", "backend", TEXT_LOCAL),
    ("templates", "backend", TEXT_LOCAL),
    # UI refresh: explicit image resolution while preserving model-native defaults for old callers.
    ("images", "resolution", "TEXT NOT NULL DEFAULT 'auto'"),
    ("images", "base_model", "TEXT NOT NULL DEFAULT ''"),
    ("images", "lora", "TEXT NOT NULL DEFAULT ''"),
    ("images", "lora_revision", "TEXT NOT NULL DEFAULT ''"),
    ("images", "lora_sha256", "TEXT NOT NULL DEFAULT ''"),
    # Issue #86: durable image archive state. The canonical digest detects later source corruption.
    ("images", "sha256", "TEXT NOT NULL DEFAULT ''"),
    ("images", "archive_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("images", "archived_at", "REAL"),
    ("images", "archive_error", "TEXT NOT NULL DEFAULT ''"),
    ("images", "archive_deleted_at", "REAL"),
    # Issue #87: opt-in Real-ESRGAN derived images keep the original PNG unchanged.
    ("images", "parent_id", TEXT_EMPTY),
    ("images", "operation", "TEXT NOT NULL DEFAULT 'generate'"),
    ("images", "scale", "INTEGER NOT NULL DEFAULT 1"),
    ("images", "upscale_model", TEXT_EMPTY),
    ("images", "requested_upscale", "TEXT NOT NULL DEFAULT 'none'"),
    # Issue #29: usage attribution names the credential class, never the key or its file reference.
    ("usage", "credential_source", "TEXT NOT NULL DEFAULT 'subscription'"),
    # Issue #17: frozen owner-approved instruction skills for a session.
    ("sessions", "skills", "TEXT NOT NULL DEFAULT '[]'"),
    # Issue #18: sanitized smart-review recommendation on the ordinary approval row.
    ("approvals", "smart", "TEXT NOT NULL DEFAULT '{}'"),
    # Issue #66: freeze hosted effort at session start; app-scoped settings live beside the token.
    ("sessions", "effort", "TEXT NOT NULL DEFAULT ''"),
    # Issue #66: in-flight app sessions keep the defaults they started with if the app is revoked.
    ("sessions", "app_defaults", "TEXT NOT NULL DEFAULT '{}'"),
]


APP_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    app_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

JSON_COLUMNS = {"context", "run", "totals", "inbox", "args", "app_tools", "app_metadata", "app_defaults", "data",
                "origins", "models", "smart", "risk_flags", "skills", "references", "examples", "manifest",
                "static_findings", "findings"}
# skill_proposals.review is JSON; sessions.review is a plain merge/push/discard string.
SKILL_JSON_COLUMNS = JSON_COLUMNS | {"review"}


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in SKILL_JSON_COLUMNS & out.keys():
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


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.executescript(APP_SETTINGS_SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self._ensure_search_index_columns()
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

    def get_session_for_user(self, sid: str, user_id: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND owner_id = ?", (sid, user_id)).fetchone())

    def find_session_ids(self, prefix: str, user_id: str | None = None, app_id: str | None = None) -> list[str]:
        sql = "SELECT id FROM sessions WHERE id LIKE ?"
        params: list = [prefix + "%"]
        if user_id is not None:
            sql += " AND owner_id = ?"
            params.append(user_id)
        if app_id is not None:
            sql += " AND app_id = ?"
            params.append(app_id)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
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

    def sessions_with_status(self, *statuses: str, user_id: str | None = None) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        query = f"SELECT * FROM sessions WHERE status IN ({marks})"
        params: list = list(statuses)
        if user_id is not None:
            query += " AND owner_id = ?"
            params.append(user_id)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY updated_at", params).fetchall()
        return [_row(r) for r in rows]

    def count_sessions(self, user_id: str, *statuses: str) -> int:
        marks = ",".join("?" * len(statuses))
        with self.lock:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM sessions WHERE owner_id = ? AND status IN ({marks})",
                (user_id, *statuses),
            ).fetchone()
        return int(row["n"] if row else 0)

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

    def _session_user_id(self, sid: str) -> str:
        row = self.conn.execute("SELECT owner_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        return (row["owner_id"] if row and row["owner_id"] else "owner")

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

    # session search (search.py)
    def _index_event(self, sid: str, seq: int, ts: float, type_: str, data: dict, user_id: str | None = None) -> None:
        from .search import event_text
        item = event_text(type_, data)
        if item and item[1].strip():
            uid = user_id if user_id is not None else self._session_user_id(sid)
            self.conn.execute(
                "INSERT INTO search_index (text, session_id, seq, kind, ts, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                (item[1], sid, seq, item[0], ts, uid),
            )

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
                owners = {r["id"]: (r["owner_id"] or "owner")
                          for r in self.conn.execute("SELECT id, owner_id FROM sessions")}
                for r in self.conn.execute("SELECT seq, session_id, ts, type, data FROM events ORDER BY seq").fetchall():
                    self._index_event(r["session_id"], r["seq"], r["ts"], r["type"], json.loads(r["data"]),
                                      user_id=owners.get(r["session_id"], "owner"))
                self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('search_index', ?)", (INDEX_VERSION,))
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def search_events(self, fts_query: str, exclude: str = "", max_rows: int = 600,
                      user_id: str | None = None, app_id: str | None = None) -> list[dict]:
        sql = ("SELECT session_id, seq, kind, ts, bm25(search_index) AS rank, "
               "snippet(search_index, 0, char(2), char(3), '…', 16) AS snippet "
               "FROM search_index WHERE search_index MATCH ?")
        params: list = [fts_query]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if exclude:
            sql += " AND session_id != ?"
            params.append(exclude)
        if app_id is not None:
            sql += " AND session_id IN (SELECT id FROM sessions WHERE app_id = ?)"
            params.append(app_id)
        with self.lock:
            try:
                rows = self.conn.execute(sql + " ORDER BY rank LIMIT ?", [*params, max_rows]).fetchall()
            except sqlite3.OperationalError:  # a query FTS5 can't parse despite the quoting: no results
                return []
        return [dict(r) for r in rows]

    def session_brief(self, sid: str, user_id: str | None = None) -> dict | None:
        query = ("SELECT id, project, target, title, status, created_at, updated_at, branch, "
                 "review, owner_id, substr(answer, 1, 400) AS answer FROM sessions WHERE id = ?")
        params: tuple = (sid,)
        if user_id is not None:
            query += " AND owner_id = ?"
            params = (sid, user_id)
        with self.lock:
            row = self.conn.execute(query, params).fetchone()
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
        status = a.get("status") or "pending"
        decided_at = time.time() if status != "pending" else None
        smart = a.get("smart") if isinstance(a.get("smart"), dict) else {}
        with self.lock:
            self.conn.execute(
                "INSERT INTO approvals (id, session_id, tool_call_id, tool, args, reason, detail, status, note, "
                "created_at, token, smart, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (a["id"], a["session_id"], a["tool_call_id"], a["tool"], json.dumps(a["args"]),
                 a["reason"], a.get("detail", ""), status, a.get("note") or "", time.time(),
                 secrets.token_urlsafe(24), json.dumps(smart), decided_at),
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

    def pending_approvals(self, sid: str | None = None, user_id: str | None = None) -> list[dict]:
        query = "SELECT a.* FROM approvals a"
        params: list = []
        where = ["a.status = 'pending'"]
        if sid:
            where.append("a.session_id = ?")
            params.append(sid)
        if user_id is not None:
            query += " JOIN sessions s ON s.id = a.session_id"
            where.append("s.owner_id = ?")
            params.append(user_id)
        query += " WHERE " + " AND ".join(where)
        with self.lock:
            return [_row(r) for r in self.conn.execute(query + " ORDER BY a.created_at", params).fetchall()]

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
        cols = ["id", "session_id", "source", "prompt", "model", "aspect_ratio", "resolution", "width", "height",
                "seed", "base_model", "lora", "lora_revision", "lora_sha256",
                "parent_id", "operation", "scale", "upscale_model", "requested_upscale"]
        defaults = {"session_id": "", "resolution": "auto", "base_model": "", "lora": "", "lora_revision": "",
                    "lora_sha256": "", "parent_id": "", "operation": "generate", "scale": 1,
                    "upscale_model": "", "requested_upscale": "none"}
        values = [job[c] if c in job else defaults[c] for c in cols]
        with self.lock:
            self.conn.execute(f"INSERT INTO images ({','.join(cols)}, status, created_at) VALUES "
                              f"({','.join('?' * len(cols))}, 'queued', ?)", values + [time.time()])

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

    def find_image_upscale(self, parent_id: str, upscale: str) -> dict | None:
        """Return the existing derived upscale for this parent and scale, if any (including failed)."""
        choice = str(upscale or "").strip().lower().replace("×", "x")
        scale = 2 if choice in ("2x", "2") else 4 if choice in ("4x", "4") else 0
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM images WHERE parent_id = ? AND operation = 'upscale' AND "
                "(requested_upscale = ? OR scale = ?) ORDER BY created_at DESC LIMIT 1",
                (parent_id, choice, scale)).fetchone()
        return dict(row) if row else None

    def image_children(self, parent_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM images WHERE parent_id = ? ORDER BY created_at DESC", (parent_id,)).fetchall()
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

    # Agent Harness for Mac pairing is separate from browser-origin pairing. It authorizes one owner CLI token;
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
            ok = self.conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                   (time.time(), kid)).rowcount == 1
            if ok:
                self.conn.execute("DELETE FROM app_settings WHERE app_id = ?", (kid,))
            return ok

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

    def delete_app_settings(self, app_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM app_settings WHERE app_id = ?", (app_id,))

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

    def insert_smart_review(self, rid: str, sid: str, approval_id: str, record: dict) -> None:
        flags = record.get("risk_flags") or []
        with self.lock:
            self.conn.execute(
                "INSERT INTO smart_reviews (id, session_id, approval_id, tool, policy_fingerprint, provider, model, "
                "mode, recommendation, confidence, risk_flags, reason, latency_ms, outcome, escalate_reason, "
                "prompt_tokens, completion_tokens, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, sid, approval_id or "", record.get("tool") or "", record.get("policy_fingerprint") or "",
                 record.get("provider") or "", record.get("model") or "", record.get("mode") or "",
                 record.get("recommendation") or "", record.get("confidence"), json.dumps(flags),
                 str(record.get("reason") or "")[:140], int(record.get("latency_ms") or 0),
                 record.get("outcome") or "", record.get("escalate_reason") or "",
                 int(record.get("prompt_tokens") or 0), int(record.get("completion_tokens") or 0),
                 float(record.get("cost_usd") or 0), time.time()),
            )

    def smart_review_stats(self, since: float | None = None) -> dict:
        since = time.time() - 7 * 86400 if since is None else since
        with self.lock:
            rows = self.conn.execute(
                "SELECT outcome, COUNT(*), COALESCE(AVG(latency_ms), 0), COALESCE(SUM(cost_usd), 0), "
                "COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0) "
                "FROM smart_reviews WHERE created_at >= ? GROUP BY outcome", (since,)).fetchall()
            reasons = self.conn.execute(
                "SELECT COALESCE(NULLIF(escalate_reason, ''), recommendation), COUNT(*) FROM smart_reviews "
                "WHERE created_at >= ? AND outcome IN ('escalated', 'failed') GROUP BY 1",
                (since,)).fetchall()
        by_outcome = {row[0]: {"count": row[1], "latency_ms": row[2], "cost_usd": row[3],
                               "prompt_tokens": row[4], "completion_tokens": row[5]} for row in rows}
        attempts = sum(v["count"] for v in by_outcome.values())
        latency = 0.0
        if attempts:
            latency = sum(v["latency_ms"] * v["count"] for v in by_outcome.values()) / attempts
        return {
            "attempts": attempts,
            "auto_approvals": by_outcome.get("auto_approved", {}).get("count", 0),
            "escalations": by_outcome.get("escalated", {}).get("count", 0) + by_outcome.get("failed", {}).get("count", 0),
            "human_asked": by_outcome.get("human_asked", {}).get("count", 0),
            "failures": by_outcome.get("failed", {}).get("count", 0),
            "latency_ms": round(latency, 1),
            "cost_usd": round(sum(v["cost_usd"] for v in by_outcome.values()), 6),
            "prompt_tokens": int(sum(v["prompt_tokens"] for v in by_outcome.values())),
            "completion_tokens": int(sum(v["completion_tokens"] for v in by_outcome.values())),
            "escalations_by_reason": {str(reason or "unknown"): n for reason, n in reasons},
        }

    def smart_reviews(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM smart_reviews ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    # instruction skills (issue #17)
    def insert_skill_proposal(self, row: dict) -> None:
        cols = ("id", "slug", "title", "purpose", "activation_suggestion", "content_hash", "status",
                "source_session_id", "skill_md", "references", "examples", "manifest", "static_findings",
                "review", "review_status", "target_slug", "diff", "created_at", "updated_at")
        values = [json.dumps(row[c]) if c in SKILL_JSON_COLUMNS else row[c] for c in cols]
        sql_cols = [f'"{c}"' if c == "references" else c for c in cols]
        with self.lock:
            self.conn.execute(
                f"INSERT INTO skill_proposals ({','.join(sql_cols)}) VALUES ({','.join('?' * len(cols))})", values)

    def update_skill_proposal(self, pid: str, *, expected_status: str | tuple[str, ...] | None = None,
                              **fields) -> bool:
        if not fields:
            return False
        if "status" in fields and expected_status is None:
            raise ValueError("skill proposal status changes require expected_status")
        fields["updated_at"] = time.time()
        sets = ", ".join(f'"{k}" = ?' if k == "references" else f"{k} = ?" for k in fields)
        values = [json.dumps(v) if k in SKILL_JSON_COLUMNS else v for k, v in fields.items()]
        query = f"UPDATE skill_proposals SET {sets} WHERE id = ?"
        params = [*values, pid]
        if expected_status is not None:
            statuses = (expected_status,) if isinstance(expected_status, str) else tuple(expected_status)
            query += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        with self.lock:
            return self.conn.execute(query, params).rowcount == 1

    def skill_proposal(self, pid: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_proposals WHERE id = ?", (pid,)).fetchone())

    def skill_proposal_by_hash(self, content_hash: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(
                "SELECT * FROM skill_proposals WHERE content_hash = ? ORDER BY created_at DESC LIMIT 1",
                (content_hash,)).fetchone())

    def list_skill_proposals(self, slug: str | None = None) -> list[dict]:
        query, params = "SELECT * FROM skill_proposals", []
        if slug:
            query += " WHERE slug = ?"
            params.append(slug)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY created_at DESC", params).fetchall()
        return [_row(r) for r in rows]

    def skill_proposal_count(self, since: float, session_id: str | None = None) -> int:
        query, params = "SELECT COUNT(*) FROM skill_proposals WHERE created_at >= ?", [since]
        if session_id:
            query += " AND source_session_id = ?"
            params.append(session_id)
        with self.lock:
            return int(self.conn.execute(query, params).fetchone()[0])

    def delete_skill_proposal(self, pid: str, *, not_status: str | tuple[str, ...] | None = None) -> bool:
        query = "DELETE FROM skill_proposals WHERE id = ?"
        params: list = [pid]
        if not_status is not None:
            statuses = (not_status,) if isinstance(not_status, str) else tuple(not_status)
            query += f" AND status NOT IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        with self.lock:
            return self.conn.execute(query, params).rowcount == 1

    def reject_skill_hash(self, content_hash: str, proposal_id: str, reason: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO skill_rejected (content_hash, proposal_id, reason, rejected_at) "
                "VALUES (?, ?, ?, ?)", (content_hash, proposal_id, reason, time.time()))

    def clear_rejected_skill_hash(self, content_hash: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_rejected WHERE content_hash = ?", (content_hash,))

    def skill_hash_rejected(self, content_hash: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM skill_rejected WHERE content_hash = ?",
                                    (content_hash,)).fetchone()
        return row is not None

    def skill_installed(self, slug: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_installed WHERE slug = ?", (slug,)).fetchone())

    def list_skill_installed(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM skill_installed ORDER BY slug").fetchall()
        return [_row(r) for r in rows]

    def upsert_skill_installed(self, row: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO skill_installed (slug, title, purpose, current_version, current_hash, enabled, "
                "installed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET title=excluded.title, purpose=excluded.purpose, "
                "current_version=excluded.current_version, current_hash=excluded.current_hash, "
                "enabled=excluded.enabled, updated_at=excluded.updated_at",
                (row["slug"], row["title"], row.get("purpose") or "", row["current_version"], row["current_hash"],
                 int(row.get("enabled") or 0), row["installed_at"], row["updated_at"]))

    def delete_skill_installed(self, slug: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_installed WHERE slug = ?", (slug,))

    def insert_skill_version(self, row: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO skill_versions (slug, version, content_hash, title, purpose, skill_md, \"references\", "
                "examples, manifest, installed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["slug"], row["version"], row["content_hash"], row["title"], row.get("purpose") or "",
                 row["skill_md"], json.dumps(row.get("references") or []), json.dumps(row.get("examples") or []),
                 json.dumps(row.get("manifest") or {}), row["installed_at"]))

    def skill_version(self, slug: str, version: int) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_versions WHERE slug = ? AND version = ?",
                                          (slug, version)).fetchone())

    def skill_version_by_hash(self, content_hash: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_versions WHERE content_hash = ?",
                                          (content_hash,)).fetchone())

    def list_skill_versions(self, slug: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM skill_versions WHERE slug = ? ORDER BY version", (slug,)).fetchall()
        return [_row(r) for r in rows]

    def skill_allowlist(self, slug: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT project FROM skill_project_allowlist WHERE slug = ? ORDER BY project", (slug,)).fetchall()
        return [r[0] for r in rows]

    def skill_allowlisted_slugs(self, project: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT slug FROM skill_project_allowlist WHERE project = ? ORDER BY slug", (project,)).fetchall()
        return [r[0] for r in rows]

    def set_skill_allowlist(self, slug: str, projects: list[str]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_project_allowlist WHERE slug = ?", (slug,))
            self.conn.executemany("INSERT INTO skill_project_allowlist (project, slug) VALUES (?, ?)",
                                  [(p, slug) for p in projects])

    def clear_skill_allowlist(self, slug: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_project_allowlist WHERE slug = ?", (slug,))

    def insert_skill_review_job(self, row: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO skill_review_jobs (id, proposal_id, content_hash, status, mode, findings, error, "
                "created_at, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["proposal_id"], row["content_hash"], row["status"], row["mode"],
                 json.dumps(row.get("findings") or {}), row.get("error") or "", row["created_at"],
                 row.get("started_at"), row.get("finished_at")))

    def update_skill_review_job(self, jid: str, **fields) -> None:
        values = [json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE skill_review_jobs SET {sets} WHERE id = ?", [*values, jid])

    def list_skill_review_jobs(self, status: tuple[str, ...] | None = None) -> list[dict]:
        query, params = "SELECT * FROM skill_review_jobs", []
        if status:
            query += f" WHERE status IN ({','.join('?' * len(status))})"
            params.extend(status)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY created_at", params).fetchall()
        return [_row(r) for r in rows]

    # household accounts (non-secret metadata only: never tokens, credentials, or session material)
    def member_count(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE role = 'member'").fetchone()
        return int(row["n"] if row else 0)

    def account_by_login(self, login: str) -> dict | None:
        if not login:
            return None
        with self.lock:
            row = self.conn.execute("SELECT * FROM accounts WHERE login = ?", (login,)).fetchone()
        return dict(row) if row else None

    def account_by_id(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM accounts WHERE role = 'member' ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def insert_account(self, row: dict) -> None:
        cols = ("user_id", "role", "login", "display_name", "enabled", "disk_quota_bytes",
                "max_running", "max_queued", "created_at", "updated_at", "last_activity_at")
        with self.lock:
            self.conn.execute(
                f"INSERT INTO accounts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )

    def update_account(self, user_id: str, **fields) -> bool:
        if not fields:
            return False
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            return self.conn.execute(
                f"UPDATE accounts SET {sets} WHERE user_id = ?", [*fields.values(), user_id]
            ).rowcount == 1

    def touch_account(self, user_id: str) -> None:
        if not user_id or user_id == "owner":
            return
        with self.lock:
            self.conn.execute("UPDATE accounts SET last_activity_at = ? WHERE user_id = ?",
                              (time.time(), user_id))

    def insert_audit(self, actor_id: str, target_id: str, action: str, outcome: str, detail: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO account_audit (ts, actor_id, target_id, action, outcome, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), actor_id, target_id, action, outcome, detail),
            )
            cutoff = time.time() - 365 * 86400
            self.conn.execute("DELETE FROM account_audit WHERE ts < ?", (cutoff,))

    def list_audit(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, actor_id, target_id, action, outcome, detail FROM account_audit "
                "ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_member_project(self, user_id: str, slug: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM member_projects WHERE user_id = ? AND slug = ?", (user_id, slug)
            ).fetchone()
        return dict(row) if row else None

    def list_member_projects(self, user_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM member_projects WHERE user_id = ? ORDER BY slug", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_member_project(self, row: dict) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO member_projects (user_id, slug, description, repo, source_url, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["user_id"], row["slug"], row.get("description") or "", row.get("repo") or "",
                 row.get("source_url") or "", now, now),
            )

    def delete_member_project(self, user_id: str, slug: str) -> bool:
        with self.lock:
            return self.conn.execute(
                "DELETE FROM member_projects WHERE user_id = ? AND slug = ?", (user_id, slug)
            ).rowcount == 1
