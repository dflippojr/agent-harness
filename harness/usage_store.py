"""The core's usage queries (#992 stage 6): the usage log (`usage`, one row per model call's tally) and each hosted
backend's last reported usage and limits (`backend_usage`).

`UsageStore` runs them on the main Database's writer queue, pooled readers and transactions. Both tables live in the
main store: an App's usage is metadata the main store keeps (#330), so the Database builds this as `db.usage`, and
every Database that `SessionStores` opens (each App's and Web's) shares the main store's. Its callers call it
directly: `Database` forwards none of these names.
"""

from __future__ import annotations

import json
import time

from .db import AND_APP, CoreStore, _reads, _row, _writes


class UsageStore(CoreStore):
    """The usage log's writes and tallies (per backend, App, end user, credential source and tool profile), and each
    hosted backend's reported usage."""

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
