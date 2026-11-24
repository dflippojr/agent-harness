"""Agent Harness for Mac pairing codes on the core's shared, transactional SQLite database (#992).

Mac pairing is separate from browser-origin pairing. A code authorizes one owner CLI token; the runner token stays in
its configured owner file and never enters SQLite. The runner_pairing_codes table stays in the core's frozen
baseline, so existing installs and backups keep their schema while this add-on is absent (#334); its queries live
here, on the core's writer queue and pooled readers.
"""

from __future__ import annotations

import secrets
import time

from harness.modules import ModuleStore, database_reads, database_writes, sha256_hex


class RunnerPairingStore(ModuleStore):
    """One-time Mac pairing codes: created, listed, revoked and redeemed for an owner key."""

    @database_writes
    def create_runner_pairing_code(self, name: str, runner: str, ttl_seconds: int) -> tuple[dict, str]:
        now = time.time()
        code = "hrp-" + secrets.token_urlsafe(18)
        row = {"id": "rp-" + secrets.token_hex(4), "name": name, "runner": runner,
               "created_at": now, "expires_at": now + ttl_seconds, "used_at": None, "key_id": ""}
        with self.lock:
            self.conn.execute("INSERT INTO runner_pairing_codes "
                              "(id, hash, name, runner, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (row["id"], sha256_hex(code), name, runner, now,
                               row["expires_at"]))
        return row, code

    @database_reads
    def list_runner_pairing_codes(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, name, runner, created_at, expires_at, used_at, key_id "
                "FROM runner_pairing_codes ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    @database_writes
    def revoke_runner_pairing_code(self, pid: str) -> bool:
        now = time.time()
        with self.lock:
            return self.conn.execute("UPDATE runner_pairing_codes SET expires_at = ? WHERE id = ? "
                                     "AND used_at IS NULL AND expires_at > ?", (now, pid, now)).rowcount == 1

    @database_writes
    def redeem_runner_pairing_code(self, code: str) -> tuple[dict | None, dict | None, str, str]:
        """Atomically redeem a native-client code. Returns (pairing, owner key, secret, error)."""
        digest, now = sha256_hex(code), time.time()
        with self.lock:
            pairing = self.conn.execute("SELECT * FROM runner_pairing_codes WHERE hash = ?", (digest,)).fetchone()
            if pairing is None:
                return None, None, "", "invalid runner pairing code"
            if pairing["used_at"] is not None:
                return None, None, "", "runner pairing code already used"
            if pairing["expires_at"] <= now:
                return None, None, "", "runner pairing code expired"
            # The core's one api_keys insert (#843), in this transaction; a runner's owner key has a NULL catalog id.
            key, secret = self.core.credentials.create_api_key(pairing["name"], "admin", "owner", [], catalog_app_id=None, now=now)
            self.conn.execute("UPDATE runner_pairing_codes SET used_at = ?, key_id = ? WHERE id = ?",
                              (now, key["id"], pairing["id"]))
        return dict(pairing), key, secret, ""
