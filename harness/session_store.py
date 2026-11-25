"""The core's session queries (#992 stage 5): sessions and their checkpoints, artifacts, draft review comments and
secret-scan dismissals, events and the session search index, approvals and smart approval reviews.

`SessionStore` runs them on its Database's writer queue, pooled readers and transactions; each Database (the main
store, Web's and every App's, #330) builds its own as `db.sessions`, so a session's rows stay in the store
`SessionStores` routes it to. The rule for what lives here: the `sessions` table and the rows keyed by one session
(`app_stores.SESSION_TABLES`), and the other rows each App keeps in its own store: its tool calls, end users,
idempotency keys and namespace audit (#992 stage 6, decisions 1 and 3).

Behind `SessionStores`, `db.sessions` (and `db.for_app(app_id).sessions`, `db.scope(app_id).sessions`) is a routing
proxy with this store's methods: each call runs on the store that holds the session, or the App's rows
(`app_stores._RoutedSessions`). `Database` forwards none of them: every caller calls the store or a proxy (#992 stage
6).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import cast

from .db import (AND_APP, AND_JOIN, AND_KIND, AND_OWNER, JSON_COLUMNS, CoreStore, _reads, _row, _rows, _writes,
                 merge_newest_first)
from .digests import sha256_hex
from .policy import canonical_args


def _like_prefix(prefix: str) -> str:
    """`LIKE ? ESCAPE '\\'` pattern for ids that start with `prefix`, its own `%`, `_` and `\\` taken literally."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


_SESSION_LIST_COLUMNS = (
    "SELECT id, project, target, model, backend, title, status, stop_reason, created_at, updated_at, totals, "
    "branch, review, workspace_removed, app_id, job_id, job_status, owner_id FROM sessions")


def _session_filter(owner_id: str | None, kind: str, target: str | None) -> tuple[str, list]:
    where, params = " WHERE kind = ?", [kind]
    if owner_id is not None:
        where += AND_OWNER
        params.append(owner_id)
    if target is not None:
        where += " AND target = ?"
        params.append(target)
    return where, params


class SessionStore(CoreStore):
    """Sessions, checkpoints, artifacts, review comments, secret dismissals, events, search, approvals and smart reviews;
    and each App's tool calls, end users, idempotency keys and namespace audit."""

    # sessions
    @_writes
    def insert_session(self, s: dict) -> None:
        cols = list(s)
        values = [json.dumps(s[c]) if c in JSON_COLUMNS else s[c] for c in cols]
        with self.lock:
            self.conn.execute(
                f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", values
            )

    @_writes
    def update_session(self, sid: str, **fields) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
        with self.lock:
            self.conn.execute(f"UPDATE sessions SET {sets} WHERE id = ?", [*values, sid])

    @_writes
    def patch_run(self, sid: str, /, **fields: object) -> dict | None:
        """Merge `fields` into session `sid`'s `run` and return the new `run` (None for a session that is gone). The
        read and the write are one writer transaction, so a field another write set meanwhile is never lost (#991):
        write `run` through here, not with a copy read earlier."""
        current = self.get_session(sid)
        if current is None:
            return None
        run = dict(current.get("run") or {})
        run.update(fields)
        self.update_session(sid, run=run)
        return run

    @_writes
    def unset_run(self, sid: str, /, *keys: str) -> dict | None:
        """Remove `keys` from session `sid`'s `run`, as `patch_run` sets them: in one writer transaction."""
        current = self.get_session(sid)
        if current is None:
            return None
        run = {k: v for k, v in (current.get("run") or {}).items() if k not in keys}
        self.update_session(sid, run=run)
        return run

    @_reads
    def get_session(self, sid: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone())

    @_reads
    def get_session_for_user(self, sid: str, user_id: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND owner_id = ?", (sid, user_id)).fetchone())

    @_reads
    def find_session_ids(self, prefix: str, user_id: str | None = None, app_id: str | None = None,
                         kind: str | None = None) -> list[str]:
        sql = "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\'"
        params: list = [_like_prefix(prefix)]  # the prefix is literal: `%` and `_` in it never match others (#785)
        if user_id is not None:
            sql += AND_OWNER
            params.append(user_id)
        if app_id is not None:
            sql += AND_APP
            params.append(app_id)
        if kind is not None:
            sql += AND_KIND
            params.append(kind)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [r["id"] for r in rows]

    @_reads
    def list_sessions(self, limit: int = 50, owner_id: str | None = None, kind: str = "agent", *,
                      before: tuple[float, str] | None = None, target: str | None = None,
                      include_active: bool = False) -> list[dict]:
        """Sessions of one conversation kind, newest first. Agent lists never include Chat and vice versa.
        `before` is a `(created_at, id)` keyset cursor; `include_active` adds every active session or one with a
        pending approval, whatever its age (#663)."""
        return self.session_page(limit, owner_id, kind, before=before, target=target,
                                 include_active=include_active)[0]

    @_reads
    def session_page(self, limit: int = 50, owner_id: str | None = None, kind: str = "agent", *,
                     before: tuple[float, str] | None = None, target: str | None = None,
                     include_active: bool = False) -> tuple[list[dict], tuple[float, str] | None]:
        """`list_sessions` and the cursor of the next older page, None when this page is the last (#663)."""
        where, params = _session_filter(owner_id, kind, target)
        page_where, page_params = where, list(params)
        if before is not None:
            page_where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            page_params += [before[0], before[0], before[1]]
        with self.lock:
            rows = self.conn.execute(
                _SESSION_LIST_COLUMNS + page_where + " ORDER BY created_at DESC, id DESC LIMIT ?",
                (*page_params, limit + 1)).fetchall()
            active = self._active_sessions(where, params) if include_active else []
        page = _rows(rows[:limit])
        cursor = (page[-1]["created_at"], page[-1]["id"]) if page and len(rows) > limit else None
        return merge_newest_first(page, active), cursor

    @_reads
    def active_sessions(self, owner_id: str | None = None, kind: str = "agent",
                        target: str | None = None) -> list[dict]:
        """Every session that is running, queued or parked, or has a pending approval, newest first (#663)."""
        where, params = _session_filter(owner_id, kind, target)
        with self.lock:
            return self._active_sessions(where, params)

    def _active_sessions(self, where: str, params: list) -> list[dict]:
        from .runner import ACTIVE
        marks = ",".join("?" * len(ACTIVE))
        rows = self.conn.execute(
            _SESSION_LIST_COLUMNS + where + f" AND status IN ({marks}) UNION "
            + _SESSION_LIST_COLUMNS + where
            + " AND id IN (SELECT session_id FROM approvals WHERE status = 'pending')"
            " ORDER BY created_at DESC, id DESC", (*params, *ACTIVE, *params)).fetchall()
        return _rows(rows)

    @_reads
    def group_sessions(self, group: str, owner_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM sessions WHERE compare_group = ? AND owner_id = ? "
                                     "ORDER BY created_at, id", (group, owner_id)).fetchall()
        return _rows(rows)

    @_writes
    def add_checkpoint(self, sid: str, turn: int, sha: str, head: str, branch: str,
                       event_seq: int | None = None) -> None:
        """`event_seq` is the last event the checkpoint covers; None means the log's end right now."""
        with self.lock:
            seq = event_seq if event_seq is not None else self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (sid,)).fetchone()[0]
            self.conn.execute("INSERT OR REPLACE INTO checkpoints (session_id, turn, sha, head, branch, event_seq, "
                              "hidden, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                              (sid, turn, sha, head, branch, seq, time.time()))

    @_reads
    def checkpoints(self, sid: str, hidden: bool | None = False) -> list[dict]:
        """Checkpoints oldest first; `hidden` None returns both visible and rewound-past ones."""
        sql = "SELECT * FROM checkpoints WHERE session_id = ?"
        params: list[object] = [sid]
        if hidden is not None:
            sql += " AND hidden = ?"
            params.append(int(hidden))
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql + " ORDER BY turn", params).fetchall()]

    @_writes
    def show_checkpoints_through(self, sid: str, turn: int) -> None:
        """After a rewind to `turn`: the checkpoints up to it are visible, the later ones hidden."""
        with self.lock:
            self.conn.execute("UPDATE checkpoints SET hidden = (turn > ?) WHERE session_id = ?", (turn, sid))

    @_writes
    def delete_checkpoints(self, sid: str, turns: list[int]) -> None:
        with self.lock:
            self.conn.executemany("DELETE FROM checkpoints WHERE session_id = ? AND turn = ?",
                                  [(sid, t) for t in turns])

    @_writes
    def delete_session(self, sid: str) -> None:
        with self._tx():
            self.conn.execute("DELETE FROM namespace_audit WHERE session_id = ?", (sid,))
            for table in ("events", "approvals", "review_comments", "secret_dismissals", "artifacts", "checkpoints",
                          "app_tool_calls", "smart_reviews"):
                self.conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))
            self.conn.execute("DELETE FROM search_index WHERE session_id = ?", (sid,))
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            # a retried create with the key that made it gets 410 until the key expires (#462)
            self.conn.execute("UPDATE idempotency_keys SET session_id = '' WHERE session_id = ?", (sid,))

    @_writes
    def put_artifact(self, sid: str, hash_: str, content: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO artifacts (session_id, hash, content) VALUES (?, ?, ?)",
                              (sid, hash_, content))

    def read_artifact(self, sid: str, hash_: str, start: int = 0, end: int | None = None) -> tuple[str, bool] | None:
        """Read a bounded character range from a session's full artifact string."""
        # Slice in Python: SQLite length()/substr() stop at the first embedded NUL.
        text = self.full_artifact(sid, hash_)
        if text is None:
            return None
        stop = len(text) if end is None else min(end, len(text))
        chunk = text[start:min(stop, start + 20000)]
        return chunk, stop > start + len(chunk)

    @_reads
    def full_artifact(self, sid: str, hash_: str) -> str | None:
        """The whole stored string, uncapped. Only for the daemon's own masking; the model-facing read is bounded."""
        with self.lock:
            row = self.conn.execute("SELECT content FROM artifacts WHERE session_id = ? AND hash = ?",
                                    (sid, hash_)).fetchone()
        return row["content"] if row else None

    # draft review comments
    @_writes
    def add_review_comment(self, sid: str, c: dict) -> dict:
        row = {"id": "rc-" + secrets.token_hex(5), "session_id": sid, "created_at": time.time(), **c}
        row["quoted"] = json.dumps(c["quoted"])
        with self.lock:
            self.conn.execute(
                "INSERT INTO review_comments (id, session_id, repo, path, side, start_line, end_line, quoted, "
                "comment, base, head, created_at) VALUES (:id, :session_id, :repo, :path, :side, :start_line, "
                ":end_line, :quoted, :comment, :base, :head, :created_at)", row)
        return {**row, "quoted": c["quoted"]}

    @_reads
    def list_review_comments(self, sid: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM review_comments WHERE session_id = ? ORDER BY created_at, id", (sid,)).fetchall()
        return [{**dict(r), "quoted": json.loads(r["quoted"])} for r in rows]

    # secret-scan dismissals: by fingerprint, so they follow the same value to later heads
    @_writes
    def add_secret_dismissal(self, sid: str, d: dict) -> dict:
        row = {"session_id": sid, "created_at": time.time(), **d}
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO secret_dismissals (session_id, fingerprint, rule, repo, path, line, reason, "
                "actor_id, created_at) VALUES (:session_id, :fingerprint, :rule, :repo, :path, :line, :reason, "
                ":actor_id, :created_at)", row)
        return row

    @_reads
    def secret_dismissals(self, sid: str) -> dict[str, dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM secret_dismissals WHERE session_id = ?", (sid,)).fetchall()
        return {r["fingerprint"]: dict(r) for r in rows}

    @_writes
    def delete_review_comments(self, sid: str, ids: list[str] | None = None) -> int:
        """Delete one session's drafts: the given ids, or all of them."""
        with self.lock:
            if ids is None:
                cur = self.conn.execute("DELETE FROM review_comments WHERE session_id = ?", (sid,))
            else:
                marks = ",".join("?" * len(ids))
                cur = self.conn.execute(
                    f"DELETE FROM review_comments WHERE session_id = ? AND id IN ({marks})", (sid, *ids))
            return cur.rowcount

    @_reads
    def sessions_with_status(self, *statuses: str, user_id: str | None = None) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        query = f"SELECT * FROM sessions WHERE status IN ({marks})"
        params: list = list(statuses)
        if user_id is not None:
            query += AND_OWNER
            params.append(user_id)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY updated_at", params).fetchall()
        return _rows(rows)

    @_reads
    def sessions_with_run_flag(self, flag: str) -> list[dict]:
        """Sessions whose current run carries `flag` (a key of the `run` JSON)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM sessions WHERE json_extract(run, ?) IS NOT NULL ORDER BY updated_at",
                (f"$.{flag}",)).fetchall()
        return _rows(rows)

    @_reads
    def count_sessions(self, user_id: str, *statuses: str, holding: bool = False) -> int:
        """`holding`: only those running or holding a place under their running cap (`run.holds_place`, #524)."""
        return self._count_sessions("owner_id", user_id, statuses, holding)

    @_reads
    def count_app_sessions(self, app_id: str, *statuses: str, holding: bool = False) -> int:
        """App `app_id`'s sessions in any of `statuses` (#524), `holding` as in count_sessions."""
        return self._count_sessions("app_id", app_id, statuses, holding)

    def _count_sessions(self, column: str, value: str, statuses: tuple, holding: bool) -> int:
        marks = ",".join("?" * len(statuses))
        held = " AND (status = 'running' OR json_extract(run, '$.holds_place') = 1)" if holding else ""
        with self.lock:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM sessions WHERE {column} = ? AND status IN ({marks}){held}",
                (value, *statuses),
            ).fetchone()
        return int(row["n"] if row else 0)

    # A lone store has no index: its rows are what SessionStores' stored counts read (#524).
    count_stored_sessions = count_sessions
    count_stored_app_sessions = count_app_sessions

    # events
    @_writes
    def insert_event(self, sid: str, type_: str, data: dict) -> dict:
        ts = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events (session_id, ts, type, data) VALUES (?, ?, ?, ?)",
                (sid, ts, type_, json.dumps(data)),
            )
            seq = cast(int, cur.lastrowid)  # the row just inserted
            self._index_event(sid, seq, ts, type_, data)
        return {"seq": seq, "session_id": sid, "ts": ts, "type": type_, "data": data}

    def _session_user_id(self, sid: str) -> str:
        row = self.conn.execute("SELECT owner_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        return (row["owner_id"] if row and row["owner_id"] else "owner")

    # session search (the index: search_index.py; queries: harness_modules/search)
    def _index_event(self, sid: str, seq: int, ts: float, type_: str, data: dict, user_id: str | None = None) -> None:
        from .search_index import event_text
        item = event_text(type_, data)
        if item and item[1].strip():
            uid = user_id if user_id is not None else self._session_user_id(sid)
            self.conn.execute(
                "INSERT INTO search_index (text, session_id, seq, kind, ts, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                (item[1], sid, seq, item[0], ts, uid),
            )

    @_writes
    def build_search_index(self) -> None:
        """Index events written before search existed (or by an older index version). Runs once."""
        from .search_index import INDEX_VERSION
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'search_index'").fetchone()
            if row and row["value"] == INDEX_VERSION:
                return
            self.conn.execute("DELETE FROM search_index")
            owners = {r["id"]: (r["owner_id"] or "owner")
                      for r in self.conn.execute("SELECT id, owner_id FROM sessions")}
            for r in self.conn.execute("SELECT seq, session_id, ts, type, data FROM events ORDER BY seq").fetchall():
                self._index_event(r["session_id"], r["seq"], r["ts"], r["type"], json.loads(r["data"]),
                                  user_id=owners.get(r["session_id"], "owner"))
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('search_index', ?)", (INDEX_VERSION,))

    @_reads
    def search_events(self, fts_query: str, exclude: str = "", max_rows: int = 600,
                      user_id: str | None = None, app_id: str | None = None,
                      session_kind: str | None = "agent") -> list[dict]:
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
        session_filters, session_params = [], []
        if session_kind is not None:
            session_filters.append("kind = ?")
            session_params.append(session_kind)
        if app_id is not None:
            session_filters.append("app_id = ?")
            session_params.append(app_id)
        if session_filters:
            sql += " AND session_id IN (SELECT id FROM sessions WHERE " + AND_JOIN.join(session_filters) + ")"
            params.extend(session_params)
        with self.lock:
            try:
                rows = self.conn.execute(sql + " ORDER BY rank LIMIT ?", [*params, max_rows]).fetchall()
            except sqlite3.OperationalError:  # a query FTS5 can't parse despite the quoting: no results
                return []
        return [dict(r) for r in rows]

    @_reads
    def session_brief(self, sid: str, user_id: str | None = None, kind: str | None = "agent",
                      with_taint: bool = False) -> dict | None:
        """A search hit's session fields; `with_taint` adds its taint list, read in the same row (#795)."""
        query = ("SELECT id, project, target, title, status, created_at, updated_at, branch, "
                 "review, owner_id, substr(answer, 1, 400) AS answer" + (", taint" if with_taint else "")
                 + " FROM sessions WHERE id = ?")
        params: list = [sid]
        if kind is not None:
            query += AND_KIND
            params.append(kind)
        if user_id is not None:
            query += AND_OWNER
            params.append(user_id)
        with self.lock:
            row = self.conn.execute(query, params).fetchone()
        if row is None:
            return None
        return {**dict(row), "taint": json.loads(row["taint"] or "[]")} if with_taint else dict(row)

    @_reads
    def events(self, sid: str, after: int = 0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq", (sid, after)
            ).fetchall()
        return _rows(rows)

    @_reads
    def has_tool_result(self, sid: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM events WHERE session_id = ? AND type = 'tool_result' LIMIT 1",
                                     (sid,)).fetchone() is not None

    @_reads
    def pushed_heads(self, sid: str) -> list[str]:
        """The short heads a session's branch was pushed at, from its `review` events (issue #263)."""
        with self.lock:
            rows = self.conn.execute("SELECT data FROM events WHERE session_id = ? AND type = 'review'",
                                     (sid,)).fetchall()
        return [d["head"] for d in (json.loads(r["data"]) for r in rows) if d.get("action") == "push" and d.get("head")]

    @_reads
    def snippet_events(self) -> list[dict]:
        """Every chat's snippet run events, oldest first (snippets.py restart recovery)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE session_id IN (SELECT id FROM sessions WHERE kind = 'chat') "
                "AND type IN ('snippet_started', 'snippet_result') ORDER BY seq").fetchall()
        return _rows(rows)

    @_reads
    def session_card_bits(self, sid: str, answer_chars: int = 400) -> dict:
        """What a session-list card needs (issue #667): the answer's first `answer_chars` characters and its full
        length, and the contents of the session's user messages, oldest first. It reads no other event's data.
        SQLite's text functions stop at a NUL character, so a value holding one is read whole and cut here instead."""
        with self.lock:
            row = self.conn.execute(
                "SELECT substr(answer, 1, ?) AS answer, length(answer) AS answer_length, "
                "CASE WHEN instr(CAST(answer AS BLOB), x'00') THEN answer END AS whole FROM sessions WHERE id = ?",
                (answer_chars, sid)).fetchone()
            rows = self.conn.execute(
                "SELECT json_extract(data, '$.content') AS content, "
                "CASE WHEN instr(data, '\\u0000') THEN data END AS whole FROM events "
                "WHERE session_id = ? AND type = 'user_message' ORDER BY seq", (sid,)).fetchall()
        if row is None:
            answer, length = "", 0
        elif row["whole"] is not None:
            answer, length = row["whole"][:answer_chars], len(row["whole"])
        else:
            answer, length = row["answer"] or "", row["answer_length"] or 0
        asks = [json.loads(r["whole"]).get("content") if r["whole"] is not None else r["content"] for r in rows]
        return {"answer": answer, "answer_length": length, "asks": [a if isinstance(a, str) else "" for a in asks]}

    @_reads
    def last_event_seq(self, sid: str) -> int:
        with self.lock:
            row = self.conn.execute("SELECT MAX(seq) AS seq FROM events WHERE session_id = ?", (sid,)).fetchone()
        return row["seq"] or 0

    # approvals
    @_writes
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

    @_reads
    def approval_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone())

    @_reads
    def get_approval(self, aid: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone())

    @_reads
    def approval_for_exact_call(self, sid: str, call_id: str, tool: str | tuple[str, ...], args: dict) -> dict | None:
        """The newest approval recorded for this exact call: the same tool call id, tool and arguments (#1063). A
        backend whose call ids come from model output can reuse an id; a reused id with another tool or other
        arguments is a different call, so it finds nothing here and is asked anew. `tool` may name each form the
        same tool is recorded under (a harness tool asked natively or over MCP)."""
        tools = (tool,) if isinstance(tool, str) else tuple(tool)
        wanted = canonical_args(args)
        with self.lock:
            rows = _rows(self.conn.execute(
                f"SELECT * FROM approvals WHERE session_id = ? AND tool_call_id = ? "
                f"AND tool IN ({', '.join('?' * len(tools))}) ORDER BY created_at DESC, rowid DESC",
                (sid, call_id, *tools),
            ).fetchall())
        return next((row for row in rows if canonical_args(row["args"]) == wanted), None)

    @_reads
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
        query += " WHERE " + AND_JOIN.join(where)
        with self.lock:
            return _rows(self.conn.execute(query + " ORDER BY a.created_at", params).fetchall())

    @_reads
    def approvals(self, sid: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM approvals WHERE session_id = ? ORDER BY created_at", (sid,)).fetchall()
        return _rows(rows)

    # a scheduled job's sessions (the job rows themselves: harness_modules/jobs/store.py) and the retention sweep's
    # view of every session
    @_reads
    def job_sessions(self, jid: str, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, title, status, stop_reason, job_status, created_at, updated_at, "
                                     "substr(answer, 1, 300) AS answer FROM sessions WHERE job_id = ? "
                                     "ORDER BY created_at DESC LIMIT ?", (jid, limit)).fetchall()
        return [dict(r) for r in rows]

    @_reads
    def session_activity(self) -> list[dict]:
        """Every session's own retention and when it was last active (its newest event, or its creation), for the
        retention sweep."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT s.id, s.app_id, s.status, s.retention_days, MAX(s.created_at, COALESCE("
                "(SELECT MAX(e.ts) FROM events e WHERE e.session_id = s.id), 0)) AS active_at FROM sessions s"
            ).fetchall()
        return [dict(r) for r in rows]

    # approval decisions and smart approval reviews
    @_writes
    def decide_approval(self, aid: str, status: str, note: str = "") -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE approvals SET status = ?, note = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (status, note, time.time(), aid),
            )
        return cur.rowcount == 1

    @_writes
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

    @_reads
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

    @_reads
    def smart_reviews(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM smart_reviews ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return _rows(rows)

    # Rows each App keeps in its own store beside its sessions (#992 stage 6, decisions 1 and 3). The tool calls are
    # keyed by session, so `db.sessions` routes them by session id; the others are the App's: call them on
    # `db.for_app(app_id).sessions`.

    # an App's end users (#365)
    @_writes
    def register_end_user(self, end_user: str) -> None:
        """Record an App's end user in the store it is called on (the App's own, `db.for_app(app_id).sessions`)."""
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
            return _rows(self.conn.execute(query + " ORDER BY created_at", params).fetchall())

    @_writes
    def finish_app_tool_call(self, sid: str, call_id: str, status: str, output: str, ok: bool) -> bool:
        with self.lock:
            return self.conn.execute(
                "UPDATE app_tool_calls SET status = ?, output = ?, ok = ?, finished_at = ? "
                "WHERE session_id = ? AND call_id = ? AND status = 'pending'",
                (status, output, int(ok), time.time(), sid, call_id)).rowcount == 1

    # an App end user's namespace audit (#470)
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
