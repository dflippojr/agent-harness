"""The core's App queries (#992 stage 5): the main store's per-App records beside the App registry. That is each
App's settings (`app_settings`), the session caps the owner set (`app_limits:` meta keys), its retention, and the
erasure of a revoked App after its grace.

`AppStore` runs them on the main Database's writer queue, pooled readers and transactions. The Database builds it as
`db.apps`, and every Database that `SessionStores` opens (each App's and Web's) shares the main store's, so any route
reaches the one registry's records. Its callers call it directly: `Database` forwards none of these names.

The rows each App keeps in its own store (its end users, its tool calls and its namespace audit) are not here: they
are reached through `for_app` and by-session routing, so they move in stage 6 with routed store access.
"""

from __future__ import annotations

import json
import time

from .db import CoreStore, _reads, _writes

APP_LIMITS_META = "app_limits:"           # meta key prefix: an App's owner-set session caps (#524)
APP_LIMIT_KEYS = ("max_running", "max_queued")


class AppStore(CoreStore):
    """An App's settings, session caps, retention and erasure: the main store's records beside its registry row."""

    @_reads
    def pending_erasures(self, due_by: float | None = None) -> list[dict]:
        """Revoked Apps (and devices) whose store and folder are still to be erased, soonest first; with `due_by`,
        only those due by then."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, name, kind, revoked_at, erase_after FROM api_keys WHERE erase_after IS NOT NULL AND "
                "erased_at IS NULL AND erase_after <= ? ORDER BY erase_after",
                (float("inf") if due_by is None else due_by,)).fetchall()
        return [dict(r) for r in rows]

    @_writes
    def mark_app_erased(self, kid: str) -> None:
        """Leave only a tombstone of an erased App in the registry: its id, name, kind and dates. Its settings,
        provider credentials and error counts go with its store.

        One transaction across stores, on purpose (#992 decision 4): the tombstone (`api_keys`, CredentialStore's), the
        provider credentials (`app_provider_credentials`, CredentialStore's), the settings (this store's) and the
        `app_error:`/`app_limits:` meta keys commit together or not at all, so a failed erase never leaves an App
        half-erased. `tests/test_app_retention.py` checks that."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM api_keys WHERE id = ? AND kind = 'web'", (kid,)).fetchone():
                raise ValueError("Stables is never erased")
            self.conn.execute("UPDATE api_keys SET erased_at = ?, erase_after = NULL, scopes = '', origins = '[]', "
                              "retention_days = NULL WHERE id = ?", (time.time(), kid))
            self.conn.execute("DELETE FROM app_settings WHERE app_id = ?", (kid,))
            self.conn.execute("DELETE FROM app_provider_credentials WHERE app_id = ?", (kid,))
            self.conn.execute("DELETE FROM meta WHERE key IN (?, ?)", ("app_error:" + kid, APP_LIMITS_META + kid))

    @_writes
    def set_app_retention(self, kid: str, days: float | None) -> bool:
        """An App's default retention for its sessions, in days (None: keep them until deleted)."""
        with self.lock:
            return self.conn.execute("UPDATE api_keys SET retention_days = ? WHERE id = ? "
                                     "AND kind NOT IN ('owner', 'web') "
                                     "AND erased_at IS NULL", (days, kid)).rowcount == 1

    @_reads
    def app_limits(self, kid: str) -> dict:
        """The session caps the owner set for App `kid` (#524): `max_running` and `max_queued`, each None when unset."""
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (APP_LIMITS_META + kid,)).fetchone()
        stored = json.loads(row["value"]) if row else {}
        return {k: (int(stored[k]) if stored.get(k) is not None else None) for k in APP_LIMIT_KEYS}

    @_writes
    def set_app_limits(self, kid: str, limits: dict) -> bool:
        """Replace App `kid`'s session caps (None: the daemon default). False when no live App has that id."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM api_keys WHERE id = ? AND kind = 'app' AND erased_at IS NULL",
                                 (kid,)).fetchone() is None:
                return False
            values = {k: limits.get(k) for k in APP_LIMIT_KEYS if limits.get(k) is not None}
            if values:
                self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                  (APP_LIMITS_META + kid, json.dumps(values)))
            else:
                self.conn.execute("DELETE FROM meta WHERE key = ?", (APP_LIMITS_META + kid,))
            return True

    @_reads
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

    @_writes
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

    @_writes
    def delete_app_settings(self, app_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM app_settings WHERE app_id = ?", (app_id,))
