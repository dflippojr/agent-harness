"""Idempotent App session creation (#462): one row per `Idempotency-Key` an App sent to POST /api/v1/sessions, in
that App's own store. It holds the hash of the key, a digest of the request body and the session it created, never
the prompt, context or tools. Erasing the session clears `session_id`, leaving a tombstone until `expires_at`."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE idempotency_keys (
        key_hash TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX idempotency_keys_session ON idempotency_keys(session_id)")
    conn.execute("CREATE INDEX idempotency_keys_expiry ON idempotency_keys(expires_at)")
