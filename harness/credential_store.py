"""The core's credential queries (#992 stage 5): API keys, pairing codes, zero-touch pairing requests and the Hub
claim, EventSource stream tickets, and App provider credentials.

`CredentialStore` runs them on the main Database's writer queue, pooled readers and transactions; the Database
builds it as `db.credentials`. Every key-minting path writes its row through `_insert_api_key` (#843), so the
methods that mint a key live here with it. Until their call sites move, `Database` forwards each of these names to
the store (`harness.db._Forwarded`). The App registry's lifecycle (erasure, retention, limits, settings) stays on
`Database` for the App store.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import cast

from .db import AND_KIND, APP_ERASE_GRACE_SECONDS, CoreStore, _reads, _row, _writes
from .digests import sha256_hex

# An API key as `list_api_keys` and `get_api_key_listing` show it: no hash. `SessionStores` adds `requests`, the count
# of its logged requests, from the endpoint module (#992; 0 while that module is absent).
_API_KEY_LISTING = (
    "SELECT k.id, k.name, k.prefix, k.kind, k.role, k.scopes, k.origins, "
    "COALESCE(k.catalog_app_id, '') AS catalog_app_id, k.created_at, k.last_used_at, k.revoked_at, "
    "k.retention_days, k.erase_after, k.erased_at "
    "FROM api_keys k WHERE k.kind != 'web'")


def _rows(rows: list) -> list[dict]:
    """Each fetched row as `_row` decodes it (a fetched row is never None)."""
    return [cast(dict, _row(r)) for r in rows]


class CredentialStore(CoreStore):
    """API keys, pairing codes and requests, the Hub claim, stream tickets and App provider credentials."""

    # API keys: every App's, device's and owner token's, and Stables' registry row (the request log: the endpoint
    # module)
    @_writes
    def create_api_key(self, name: str, scopes: str = "inference", kind: str = "device",
                       origins: list[str] | None = None, catalog_app_id: str | None = "", *,
                       now: float | None = None) -> tuple[dict, str]:
        """Mint a key (see `_insert_api_key`); inside a caller's write it joins that transaction, stamped `now`."""
        with self.lock:
            return self._insert_api_key(name, scopes, kind, origins or [], time.time() if now is None else now,
                                        catalog_app_id)

    def _insert_api_key(self, name: str, scopes: str, kind: str, origins: list[str], now: float,
                        catalog_app_id: str | None = "", role: str = "") -> tuple[dict, str]:
        """Mint a `kind` key and write its row, the one api_keys insert for every minting path (#843). The caller
        holds the lock or its transaction. Returns (key row, secret); the row names `role` only when one is set and
        leaves out `catalog_app_id` when it is None, which a runner's owner key stores as NULL."""
        secret = {"app": "ha-", "owner": "ho-"}.get(kind, "hk-") + secrets.token_urlsafe(32)
        key = {"id": "k-" + secrets.token_hex(4), "name": name, "prefix": secret[:10], "created_at": now,
               "scopes": scopes, "kind": kind, "origins": origins}
        if catalog_app_id is not None:
            key["catalog_app_id"] = catalog_app_id
        if role:
            key["role"] = role
        self.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at, scopes, kind, origins, "
                          "catalog_app_id, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (key["id"], name, key["prefix"], sha256_hex(secret), now, scopes, kind,
                           json.dumps(origins), catalog_app_id, role))
        return key, secret

    @_reads
    def origin_allowed(self, origin: str, kind: str | None = None) -> bool:
        query = "SELECT origins FROM api_keys WHERE revoked_at IS NULL"
        params: tuple = ()
        if kind is not None:
            query += AND_KIND
            params = (kind,)
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return any(origin in (json.loads(r["origins"] or "[]")) for r in rows)

    @_reads
    def api_key_by_secret(self, key: str) -> dict | None:
        if not key:
            return None
        with self.lock:
            row = self.conn.execute("SELECT * FROM api_keys WHERE hash = ? AND revoked_at IS NULL AND kind != 'web'",
                                    (sha256_hex(key),)).fetchone()
        return _row(row)

    @_reads
    def get_api_key(self, kid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM api_keys WHERE id = ?", (kid,)).fetchone()
        return _row(row)

    @_writes
    def touch_api_key(self, kid: str) -> None:
        """Record successful credential authentication without opening its App store."""
        with self.lock:
            self.conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ? AND revoked_at IS NULL",
                              (time.time(), kid))

    @_reads
    def list_api_keys(self) -> list[dict]:
        """The keys: every App's, device's and owner token's. Stables' registry row has no key, so it is
        not listed (`get_api_key(WEB_APP_ID)` reads it)."""
        with self.lock:
            rows = self.conn.execute(_API_KEY_LISTING + " ORDER BY k.created_at").fetchall()
        return _rows(rows)

    @_reads
    def get_api_key_listing(self, kid: str) -> dict | None:
        """Key `kid` as `list_api_keys` lists it, read alone (#669): the same fields, without listing every key. None when `kid` is not a listed key (a missing or Stables')."""
        with self.lock:
            row = self.conn.execute(_API_KEY_LISTING + " AND k.id = ?", (kid,)).fetchone()
        return _row(row)

    @_writes
    def ensure_web_app(self, app_id: str) -> None:
        """Register Stables in the App registry (#330 decision 4) under its reserved id, once. It has no
        token: its hash is of a random secret nobody is given, and `api_key_by_secret` never matches its kind."""
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO api_keys (id, name, prefix, hash, created_at, scopes, kind, origins) "
                "VALUES (?, 'Stables', '', ?, ?, '', 'web', '[]')",
                (app_id, sha256_hex("web:" + secrets.token_urlsafe(32)), time.time()))

    @_writes
    def revoke_api_key(self, kid: str) -> bool:
        """Revoke a key at once. An App's or device's store and folder are scheduled for erasure after
        `APP_ERASE_GRACE_SECONDS` (#330); `restore_api_key` undoes that during the grace. The App's own settings stay
        until that erasure (unused while revoked), so an undone revoke gets them back."""
        now = time.time()
        with self.lock:
            # The Hub's key is revoked only by releasing the Hub (`release_hub`, #543).
            ok = self.conn.execute("UPDATE api_keys SET revoked_at = ?, erase_after = CASE WHEN kind = 'owner' "
                                   "THEN NULL ELSE ? END WHERE id = ? AND revoked_at IS NULL AND kind != 'web' "
                                   "AND role != 'hub'",
                                   (now, now + APP_ERASE_GRACE_SECONDS, kid)).rowcount == 1
            if ok:  # an owner key has no erasure to take its settings later
                self.conn.execute("DELETE FROM stream_tickets WHERE key_id = ?", (kid,))
                self.conn.execute("DELETE FROM app_settings WHERE app_id = ? AND app_id IN "
                                  "(SELECT id FROM api_keys WHERE kind = 'owner')", (kid,))
            return ok

    @_writes
    def restore_api_key(self, kid: str) -> tuple[dict, str] | None:
        """Undo a revoke during its erasure grace: the App keeps its id, store and folder, and gets a new token
        (shown once; the revoked one stays dead). None when `kid` has no pending erasure."""
        with self.lock:
            row = self.conn.execute("SELECT * FROM api_keys WHERE id = ? AND revoked_at IS NOT NULL AND erased_at "
                                    "IS NULL AND erase_after > ?", (kid, time.time())).fetchone()
            if row is None:
                return None
            prefix = {"app": "ha-", "owner": "ho-"}.get(row["kind"], "hk-")
            key = prefix + secrets.token_urlsafe(32)
            self.conn.execute("UPDATE api_keys SET hash = ?, prefix = ?, revoked_at = NULL, erase_after = NULL "
                              "WHERE id = ?", (sha256_hex(key), key[:10], kid))
        return cast(dict, self.get_api_key_listing(kid)), key  # the key just restored: a listed one

    # browser pairing codes
    @_writes
    def create_pairing_code(self, name: str, origin: str, scopes: str, ttl_seconds: int,
                            catalog_app_id: str = "") -> tuple[dict, str]:
        now = time.time()
        code = "hp-" + secrets.token_urlsafe(18)
        row = {"id": "p-" + secrets.token_hex(4), "name": name, "origin": origin, "scopes": scopes,
               "catalog_app_id": catalog_app_id, "created_at": now, "expires_at": now + ttl_seconds,
               "used_at": None, "key_id": ""}
        with self.lock:
            self.conn.execute("INSERT INTO pairing_codes (id, hash, name, origin, scopes, catalog_app_id, created_at, "
                              "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (row["id"], sha256_hex(code), name, origin, scopes,
                               catalog_app_id, now, row["expires_at"]))
        return row, code

    @_reads
    def list_pairing_codes(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, name, origin, scopes, COALESCE(catalog_app_id, '') AS catalog_app_id, "
                                     "created_at, expires_at, used_at, key_id "
                                     "FROM pairing_codes ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    @_writes
    def revoke_pairing_code(self, pid: str) -> bool:
        with self.lock:
            return self.conn.execute("UPDATE pairing_codes SET expires_at = ? WHERE id = ? AND used_at IS NULL "
                                     "AND expires_at > ?", (time.time(), pid, time.time())).rowcount == 1

    @_reads
    def pairing_id_for_key(self, key_id: str) -> str:
        with self.lock:
            row = self.conn.execute("SELECT id FROM pairing_codes WHERE key_id = ?", (key_id,)).fetchone()
        return row["id"] if row else ""

    @_reads
    def pairing_origin_active(self, origin: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM pairing_codes WHERE origin = ? AND used_at IS NULL "
                                    "AND expires_at > ? LIMIT 1", (origin, time.time())).fetchone()
        return row is not None

    @_writes
    def redeem_pairing_code(self, code: str, origin: str) -> tuple[dict | None, str, str]:
        """Atomically redeem a bootstrap code. Returns (key row, secret, error)."""
        digest, now = sha256_hex(code), time.time()
        with self._tx():
            pairing = self.conn.execute("SELECT * FROM pairing_codes WHERE hash = ?", (digest,)).fetchone()
            if pairing is None:
                return None, "", "invalid pairing code"
            if pairing["used_at"] is not None:
                return None, "", "pairing code already used"
            if pairing["expires_at"] <= now:
                return None, "", "pairing code expired"
            if pairing["origin"] != origin:
                return None, "", "pairing code is not approved for this origin"
            key, secret = self._insert_api_key(pairing["name"], pairing["scopes"], "app", [origin], now,
                                               pairing["catalog_app_id"] or "")
            self.conn.execute("UPDATE pairing_codes SET used_at = ?, key_id = ? WHERE id = ?",
                              (now, key["id"], pairing["id"]))
        return key, secret, ""

    # zero-touch App pairing requests (#519, harness/pairing_requests.py): the row holds the hash of the App's
    # code_challenge, never a token or the verifier
    _PAIRING_REQUEST_FIELDS = ("id", "kind", "name", "scopes", "catalog_app_id", "origin", "source", "challenge_hash",
                               "match_code", "state", "armed", "created_at", "expires_at", "approved_at",
                               "finished_at", "key_id")

    @_writes
    def insert_pairing_request(self, row: dict) -> None:
        fields = [f for f in self._PAIRING_REQUEST_FIELDS if f in row]
        with self.lock:
            self.conn.execute(f"INSERT INTO pairing_requests ({', '.join(fields)}) "
                              f"VALUES ({', '.join('?' * len(fields))})", [row[f] for f in fields])

    @_reads
    def get_pairing_request(self, rid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM pairing_requests WHERE id = ?", (rid,)).fetchone()
        return dict(row) if row else None

    @_reads
    def list_pairing_requests(self, kind: str | None = None) -> list[dict]:
        """The newest 100 requests, or the newest 100 of one `kind` (`app` or `hub`)."""
        where, args = ("WHERE kind = ? ", (kind,)) if kind else ("", ())
        with self.lock:
            rows = self.conn.execute(f"SELECT * FROM pairing_requests {where}ORDER BY created_at DESC LIMIT 100",
                                     args).fetchall()
        return [dict(r) for r in rows]

    @_reads
    def open_pairing_match_codes(self) -> set[str]:
        """Codes waiting for owner approval or confirmation, across all kinds and without a row limit."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT match_code FROM pairing_requests WHERE state IN ('pending', 'claimed')").fetchall()
        return {row[0] for row in rows}

    @_reads
    def pending_pairing_catalog_origins(self, now: float) -> list[tuple[str, str]]:
        """Labels and origins of unexpired open App requests; no secrets or 100-row list limit."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT catalog_app_id, origin FROM pairing_requests WHERE kind = 'app' "
                "AND state IN ('pending', 'armed', 'claimed', 'approved') AND expires_at > ?",
                (now,)).fetchall()
        return [(row[0], row[1]) for row in rows]

    @_reads
    def pairing_request_load(self, source: str, since: float, active: tuple[str, ...]) -> tuple[int, int, int]:
        """(requests `source` made since `since`, its unapproved active requests, every unapproved active request)."""
        marks = ", ".join("?" * len(active))
        with self.lock:
            recent = self.conn.execute("SELECT COUNT(*) FROM pairing_requests WHERE source = ? AND created_at >= ?",
                                       (source, since)).fetchone()[0]
            mine, total = self.conn.execute(
                f"SELECT COALESCE(SUM(source = ?), 0), COUNT(*) FROM pairing_requests WHERE state IN ({marks})",
                (source, *active)).fetchone()
        return recent, mine, total

    @_writes
    def update_pairing_request(self, rid: str, states: tuple[str, ...], **fields) -> bool:
        """Set `fields` on the request if it is still in one of `states` (a compare-and-set)."""
        if not fields or not set(fields) <= set(self._PAIRING_REQUEST_FIELDS):
            raise ValueError(f"unknown pairing request fields: {sorted(fields)}")
        assigns = ", ".join(f"{name} = ?" for name in fields)
        marks = ", ".join("?" * len(states))
        with self.lock:
            return self.conn.execute(f"UPDATE pairing_requests SET {assigns} WHERE id = ? AND state IN ({marks})",
                                     (*fields.values(), rid, *states)).rowcount == 1

    @_writes
    def expire_pairing_requests(self, now: float, active: tuple[str, ...], keep_seconds: float) -> list[dict]:
        """Mark active requests past their deadline expired and return them; forget finished ones older than
        `keep_seconds`."""
        marks = ", ".join("?" * len(active))
        with self.lock:
            rows = [dict(r) for r in self.conn.execute(
                f"SELECT * FROM pairing_requests WHERE state IN ({marks}) AND expires_at <= ?", (*active, now))]
            for row in rows:
                self.conn.execute("UPDATE pairing_requests SET state = 'expired', finished_at = ? WHERE id = ?",
                                  (now, row["id"]))
            self.conn.execute(f"DELETE FROM pairing_requests WHERE state NOT IN ({marks}) AND created_at < ?",
                              (*active, now - keep_seconds))
        return rows

    @_reads
    def pairing_request_origin_known(self, origin: str) -> bool:
        """Whether a pairing request (in any state, until it is forgotten) belongs to this browser origin."""
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM pairing_requests WHERE origin = ? LIMIT 1", (origin,)).fetchone()
        return row is not None

    @_writes
    def redeem_pairing_request(self, rid: str, now: float) -> tuple[dict | None, str]:
        """Mint the request's `ha-` App key and mark it redeemed, if it is still approved. Returns (key row, secret)."""
        with self._tx():
            req = self.conn.execute("SELECT * FROM pairing_requests WHERE id = ? AND kind = 'app' "
                                    "AND state = 'approved' AND expires_at > ?", (rid, now)).fetchone()
            if req is None:
                return None, ""
            key, secret = self._insert_api_key(req["name"], req["scopes"], "app",
                                               [req["origin"]] if req["origin"] else [], now,
                                               req["catalog_app_id"] or "")
            self.conn.execute("UPDATE pairing_requests SET state = 'redeemed', finished_at = ?, key_id = ? "
                              "WHERE id = ?", (now, key["id"], rid))
        return key, secret

    # the exclusive Hub claim (#543, harness/hub_claim.py): one row at most, never a token or hash
    @_reads
    def hub_claim(self) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT key_id, name, kind, origin, request_id, claimed_at FROM hub_claim "
                                    "WHERE slot = 1").fetchone()
        return dict(row) if row else None

    @_writes
    def redeem_hub_claim(self, rid: str, now: float) -> tuple[dict | None, str, str]:
        """Mint the Hub's key (owner kind, admin scope, role `hub`), record it as the one Hub and mark the request
        redeemed, if the request is still approved and no Hub is recorded. Returns (key row, secret, error)."""
        with self._tx():
            req = self.conn.execute("SELECT * FROM pairing_requests WHERE id = ? AND kind = 'hub' "
                                    "AND state = 'approved' AND expires_at > ?", (rid, now)).fetchone()
            if req is None:
                return None, "", "used"
            if self.conn.execute("SELECT 1 FROM hub_claim WHERE slot = 1").fetchone():
                return None, "", "claimed"
            key, secret = self._insert_api_key(req["name"], "admin", "owner",
                                               [req["origin"]] if req["origin"] else [], now,
                                               req["catalog_app_id"] or "", role="hub")
            self.conn.execute("INSERT INTO hub_claim (slot, key_id, name, kind, origin, request_id, claimed_at) "
                              "VALUES (1, ?, ?, ?, ?, ?, ?)",
                              (key["id"], key["name"], "browser" if req["origin"] else "native", req["origin"], rid,
                               now))
            self.conn.execute("UPDATE pairing_requests SET state = 'redeemed', finished_at = ?, key_id = ? "
                              "WHERE id = ?", (now, key["id"], rid))
        return key, secret, ""

    @_writes
    def release_hub(self, now: float) -> dict | None:
        """Revoke the Hub's key and clear the record, so a new Hub may claim. Returns the released record, or None
        when no Hub is recorded."""
        with self._tx():
            row = self.conn.execute("SELECT key_id, name, kind, origin, request_id, claimed_at FROM hub_claim "
                                    "WHERE slot = 1").fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE api_keys SET revoked_at = ?, erase_after = NULL WHERE id = ? "
                              "AND revoked_at IS NULL", (now, row["key_id"]))
            self.conn.execute("DELETE FROM app_settings WHERE app_id = ?", (row["key_id"],))
            self.conn.execute("DELETE FROM hub_claim WHERE slot = 1")
        return dict(row)

    @_reads
    def hub_minted_owner_keys(self) -> list[dict]:
        """Live owner keys a Hub key minted through `POST /keys` (their `key.create` audit row is attributed `hub`):
        release cannot revoke them, so it names them for the owner to revoke."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, name, prefix, created_at FROM api_keys WHERE kind = 'owner' AND role != 'hub' "
                "AND revoked_at IS NULL AND id IN (SELECT target_id FROM account_audit WHERE action = 'key.create' "
                "AND outcome = 'ok' AND actor_kind = 'hub') ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # EventSource stream tickets
    @_writes
    def create_stream_ticket(self, key_id: str, session_id: str, origin: str,
                             ttl_seconds: int = 60) -> tuple[str, float]:
        now = time.time()
        ticket = "hs-" + secrets.token_urlsafe(24)
        with self.lock:
            self.conn.execute("DELETE FROM stream_tickets WHERE expires_at <= ?", (now,))
            self.conn.execute("INSERT INTO stream_tickets "
                              "(hash, key_id, session_id, origin, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (sha256_hex(ticket), key_id, session_id, origin, now,
                               now + ttl_seconds))
        return ticket, now + ttl_seconds

    @_reads
    def stream_ticket_key(self, ticket: str, session_id: str, origin: str) -> dict | None:
        if not ticket or not origin:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT k.* FROM stream_tickets t JOIN api_keys k ON k.id = t.key_id "
                "WHERE t.hash = ? AND t.session_id = ? AND t.origin = ? AND t.expires_at > ? "
                "AND k.revoked_at IS NULL",
                (sha256_hex(ticket), session_id, origin, time.time())).fetchone()
        return _row(row)

    @_reads
    def stream_ticket_origin_active(self, ticket: str, origin: str) -> bool:
        if not ticket or not origin:
            return False
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM stream_tickets t JOIN api_keys k ON k.id = t.key_id "
                "WHERE t.hash = ? AND t.origin = ? AND t.expires_at > ? AND k.revoked_at IS NULL LIMIT 1",
                (sha256_hex(ticket), origin, time.time())).fetchone()
        return row is not None

    # Owner-managed per-app provider policy. Secret values and file paths never enter this database.
    @_writes
    def set_app_provider_credential(self, app_id: str, backend: str, secret_ref: str, policy: str,
                                    models: list[str]) -> dict:
        now, cid = time.time(), "pc-" + secrets.token_hex(5)
        with self._tx():
            self.conn.execute("UPDATE app_provider_credentials SET revoked_at = ? "
                              "WHERE app_id = ? AND backend = ? AND revoked_at IS NULL", (now, app_id, backend))
            self.conn.execute("INSERT INTO app_provider_credentials "
                              "(id, app_id, backend, secret_ref, policy, models, created_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (cid, app_id, backend, secret_ref, policy, json.dumps(models), now))
        return cast(dict, self.app_provider_credential(app_id, backend))  # the row just inserted

    @_reads
    def app_provider_credential(self, app_id: str, backend: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_provider_credentials WHERE app_id = ? AND backend = ? "
                                    "AND revoked_at IS NULL", (app_id, backend)).fetchone()
        return _row(row)

    @_reads
    def app_provider_credential_by_id(self, cid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_provider_credentials WHERE id = ?", (cid,)).fetchone()
        return _row(row)

    @_reads
    def app_provider_managed(self, app_id: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM app_provider_credentials WHERE app_id = ? LIMIT 1",
                                     (app_id,)).fetchone() is not None

    @_reads
    def list_app_provider_credentials(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT c.*, k.name AS app_name FROM app_provider_credentials c "
                "LEFT JOIN api_keys k ON k.id = c.app_id ORDER BY c.created_at DESC").fetchall()
        return _rows(rows)

    @_writes
    def revoke_app_provider_credential(self, cid: str) -> bool:
        with self.lock:
            return self.conn.execute("UPDATE app_provider_credentials SET revoked_at = ? "
                                     "WHERE id = ? AND revoked_at IS NULL", (time.time(), cid)).rowcount == 1
