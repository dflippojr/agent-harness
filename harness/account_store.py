"""The core's account queries (#992 stage 5): household accounts and their audit log, member projects, GitHub
connections, members' own provider API keys, Google identities and invitations, and Stables sign-in sessions.

`AccountStore` runs them on the main Database's writer queue, pooled readers and transactions. The Database builds it
as `db.accounts`, and every Database that `SessionStores` opens (each App's and Web's) shares the main store's, so
any route reaches the one household registry. Its callers call it directly: `Database` forwards none of these names.
An App end user's namespace audit stays on `Database` until stage 6: its rows live in each App's own store.
"""

from __future__ import annotations

import json
import sqlite3
import time

from .db import CoreStore, _reads, _writes


class AccountStore(CoreStore):
    """Accounts, the account audit, member projects and keys, GitHub connections, Google identities and web sessions."""

    # household accounts (non-secret metadata only: never tokens, credentials, or session material)
    @_reads
    def member_count(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE role = 'member'").fetchone()
        return int(row["n"] if row else 0)

    @_reads
    def account_by_login(self, login: str) -> dict | None:
        if not login:
            return None
        with self.lock:
            row = self.conn.execute("SELECT * FROM accounts WHERE login = ?", (login,)).fetchone()
        return dict(row) if row else None

    @_reads
    def account_by_id(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @_reads
    def list_accounts(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM accounts WHERE role = 'member' ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    @_writes
    def insert_account(self, row: dict) -> None:
        cols = ("user_id", "role", "login", "display_name", "enabled", "disk_quota_bytes",
                "max_running", "max_queued", "created_at", "updated_at", "last_activity_at")
        with self.lock:
            self.conn.execute(
                f"INSERT INTO accounts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )

    @_writes
    def update_account(self, user_id: str, **fields) -> bool:
        if not fields:
            return False
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            return self.conn.execute(
                f"UPDATE accounts SET {sets} WHERE user_id = ?", [*fields.values(), user_id]
            ).rowcount == 1

    @_writes
    def touch_account(self, user_id: str) -> None:
        if not user_id or user_id == "owner":
            return
        with self.lock:
            self.conn.execute("UPDATE accounts SET last_activity_at = ? WHERE user_id = ?",
                              (time.time(), user_id))

    @_writes
    def insert_audit(self, actor_id: str, target_id: str, action: str, outcome: str, detail: str = "", *,
                     context=None, target_kind: str = "", metadata: dict | None = None) -> None:
        """Append one audit row (joins the caller's transaction). `context` is an AuditContext built after
        authentication; without one the row carries no credential/source attribution."""
        from . import audit_context
        ctx = context if context is not None else audit_context.AuditContext(actor_id)
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO account_audit (ts, actor_id, target_id, action, outcome, detail, actor_kind, key_id, "
                "source, target_kind, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, ctx.actor_id or actor_id, target_id, action, outcome, detail, ctx.actor_kind, ctx.key_id,
                 ctx.source, target_kind, audit_context.dump_metadata(action, metadata)),
            )
            # Retention (365 days) is applied here, on insertion; nothing else deletes audit rows.
            self.conn.execute("DELETE FROM account_audit WHERE ts < ?", (now - 365 * 86400,))

    @_reads
    def unknown_denials_since(self, action: str, since: float) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM account_audit WHERE action = ? AND outcome = 'denied' "
                "AND actor_kind = 'unknown' AND ts >= ?", (action, since)).fetchone()[0]

    @_reads
    def list_audit(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, actor_id, target_id, action, outcome, detail FROM account_audit "
                "ORDER BY ts DESC, id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [dict(r) for r in rows]

    @_reads
    def audit_page(self, limit: int = 200, before_id: int | None = None, *, actor_id: str | None = None,
                   key_id: str | None = None, target_id: str | None = None, action: str | None = None,
                   outcome: str | None = None, since: float | None = None, until: float | None = None) -> dict:
        """Newest-first page by immutable id; `next_before_id` is the last id of a full page that has more rows."""
        limit = max(1, min(int(limit), 500))
        where, args = [], []
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        for column, value in (("actor_id", actor_id), ("key_id", key_id), ("target_id", target_id),
                              ("action", action), ("outcome", outcome)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts < ?")
            args.append(until)
        sql = ("SELECT id, ts, actor_id, actor_kind, key_id, source, target_id, target_kind, action, outcome, "
               "detail, metadata FROM account_audit")
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.lock:
            rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit + 1]).fetchall()
        more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item["metadata"] or "{}")
            except ValueError:
                item["metadata"] = {}
            items.append(item)
        return {"items": items, "next_before_id": items[-1]["id"] if more and items else None}

    @_reads
    def get_member_project(self, user_id: str, slug: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM member_projects WHERE user_id = ? AND slug = ?", (user_id, slug)
            ).fetchone()
        return dict(row) if row else None

    @_reads
    def list_member_projects(self, user_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM member_projects WHERE user_id = ? ORDER BY slug", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    @_writes
    def insert_member_project(self, row: dict) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO member_projects (user_id, slug, description, repo, source_url, source_auth, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["user_id"], row["slug"], row.get("description") or "", row.get("repo") or "",
                 row.get("source_url") or "", row.get("source_auth") or "", now, now),
            )

    @_reads
    def get_github_connection(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM github_connections WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @_reads
    def list_github_connections(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM github_connections ORDER BY user_id").fetchall()
        return [dict(r) for r in rows]

    @_writes
    def set_github_connection(self, user_id: str, **fields) -> None:
        """Upsert non-secret connection state: status, timestamps, and a sanitized error class only."""
        allowed = {"status", "namespace_version", "connected_at", "last_used_at", "last_error"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown github connection fields: {sorted(bad)}")
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO github_connections (user_id, updated_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO NOTHING", (user_id, now))
            if fields:
                cols = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE github_connections SET {cols}, updated_at = ? WHERE user_id = ?",
                                  (*fields.values(), now, user_id))

    @_writes
    def delete_member_project(self, user_id: str, slug: str) -> bool:
        with self.lock:
            return self.conn.execute(
                "DELETE FROM member_projects WHERE user_id = ? AND slug = ?", (user_id, slug)
            ).rowcount == 1

    # Issue #393: a member's own provider API key. `ciphertext` is sealed by `member_keys`; `last4` is all that shows.
    @_reads
    def member_api_key(self, user_id: str, backend: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM member_api_keys WHERE user_id = ? AND backend = ?",
                                    (user_id, backend)).fetchone()
        return dict(row) if row else None

    @_reads
    def member_api_keys(self, user_id: str) -> list[dict]:
        """The member's keys without their ciphertext."""
        with self.lock:
            rows = self.conn.execute("SELECT user_id, backend, last4, created_at, updated_at FROM member_api_keys "
                                     "WHERE user_id = ? ORDER BY backend", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    @_writes
    def set_member_api_key(self, user_id: str, backend: str, ciphertext: bytes, last4: str) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO member_api_keys (user_id, backend, ciphertext, last4, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, backend) DO UPDATE SET "
                "ciphertext = excluded.ciphertext, last4 = excluded.last4, updated_at = excluded.updated_at",
                (user_id, backend, ciphertext, last4, now, now))

    @_writes
    def delete_member_api_keys(self, user_id: str, backend: str | None = None) -> int:
        """Delete one backend's key, or every key of the member."""
        with self.lock:
            if backend is None:
                return self.conn.execute("DELETE FROM member_api_keys WHERE user_id = ?", (user_id,)).rowcount
            return self.conn.execute("DELETE FROM member_api_keys WHERE user_id = ? AND backend = ?",
                                     (user_id, backend)).rowcount

    # Issue #64: Google identities, link invitations, and Stables sessions (hashes only).
    @_reads
    def google_identity(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM google_identities WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @_reads
    def google_identity_by_sub(self, sub: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM google_identities WHERE sub = ?", (sub,)).fetchone()
        return dict(row) if row else None

    @_writes
    def link_google_identity(self, user_id: str, sub: str, email: str, now: float) -> bool:
        """Insert the link unless the member or the `sub` is already linked. Callers hold `tx()` when combining."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO google_identities (user_id, sub, email, linked_at, last_sign_in_at) "
                    "VALUES (?, ?, ?, ?, ?)", (user_id, sub, email, now, now))
            except sqlite3.IntegrityError:
                return False
        return True

    @_writes
    def unlink_google_identity(self, user_id: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM google_identities WHERE user_id = ?", (user_id,)).rowcount == 1

    @_writes
    def touch_google_sign_in(self, user_id: str, email: str, now: float) -> None:
        with self.lock:
            self.conn.execute("UPDATE google_identities SET email = ?, last_sign_in_at = ? WHERE user_id = ?",
                              (email, now, user_id))

    @_writes
    def put_google_invitation(self, user_id: str, code_hash: str, now: float, expires_at: float) -> None:
        """Replace any pending invitation for this member."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO google_link_invitations (user_id, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET code_hash = excluded.code_hash, "
                "created_at = excluded.created_at, expires_at = excluded.expires_at",
                (user_id, code_hash, now, expires_at))

    @_reads
    def google_invitation(self, user_id: str, now: float) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id, created_at, expires_at FROM google_link_invitations "
                "WHERE user_id = ? AND expires_at > ?", (user_id, now)).fetchone()
        return dict(row) if row else None

    @_reads
    def google_invitation_user(self, code_hash: str, now: float) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id FROM google_link_invitations WHERE code_hash = ? AND expires_at > ?",
                (code_hash, now)).fetchone()
        return row["user_id"] if row else None

    @_writes
    def consume_google_invitation(self, user_id: str, code_hash: str, now: float) -> bool:
        with self.lock:
            return self.conn.execute(
                "DELETE FROM google_link_invitations WHERE user_id = ? AND code_hash = ? AND expires_at > ?",
                (user_id, code_hash, now)).rowcount == 1

    @_writes
    def cancel_google_invitation(self, user_id: str) -> bool:
        with self.lock:
            return self.conn.execute(
                "DELETE FROM google_link_invitations WHERE user_id = ?", (user_id,)).rowcount == 1

    @_writes
    def insert_web_session(self, id_hash: str, user_id: str, now: float, expires_at: float) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO web_sessions (id_hash, user_id, created_at, last_seen_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)", (id_hash, user_id, now, now, expires_at))

    @_reads
    def web_session(self, id_hash: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM web_sessions WHERE id_hash = ?", (id_hash,)).fetchone()
        return dict(row) if row else None

    @_writes
    def touch_web_session(self, id_hash: str, now: float) -> None:
        with self.lock:
            self.conn.execute("UPDATE web_sessions SET last_seen_at = ? WHERE id_hash = ? AND revoked_at IS NULL",
                              (now, id_hash))

    @_writes
    def revoke_web_session(self, id_hash: str, now: float) -> str | None:
        """Revoke one session; returns its member `user_id` when it was active."""
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id FROM web_sessions WHERE id_hash = ? AND revoked_at IS NULL", (id_hash,)).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE web_sessions SET revoked_at = ? WHERE id_hash = ?", (now, id_hash))
        return row["user_id"]

    @_writes
    def revoke_web_sessions(self, user_id: str, now: float) -> int:
        with self.lock:
            return self.conn.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id)).rowcount

    @_reads
    def count_web_sessions(self, user_id: str, now: float, idle_seconds: float) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM web_sessions WHERE user_id = ? AND revoked_at IS NULL "
                "AND expires_at > ? AND last_seen_at > ?", (user_id, now, now - idle_seconds)).fetchone()
        return int(row["n"])

    @_writes
    def purge_web_sessions(self, before: float) -> int:
        """Drop rows that expired or were revoked before `before`."""
        with self.lock:
            return self.conn.execute(
                "DELETE FROM web_sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
                (before, before)).rowcount
