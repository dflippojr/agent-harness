"""Audit actor context (#467): who (credential, entry point) made each household/security audit change.
Legacy rows keep their data and read as `unknown`; no historical attribution is invented."""
import sqlite3

_COLUMNS = (
    ("actor_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("key_id", "TEXT NOT NULL DEFAULT ''"),
    ("source", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("target_kind", "TEXT NOT NULL DEFAULT ''"),
    ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
)


def up(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(account_audit)")}
    for name, definition in _COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE account_audit ADD COLUMN {name} {definition}")
