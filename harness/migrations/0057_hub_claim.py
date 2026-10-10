"""Exclusive Hub claim (#543): the one Hub that administers this daemon, and the role of its key.

`hub_claim` holds at most one row (`slot` is always 1), so a second Hub can never be recorded beside the first. It
keeps the Hub key's id, display name, kind (browser or native), origin and claim time; never a token or hash.
`api_keys.role` is `hub` on that key (an owner-kind key with the admin scope) and empty on every other key."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE api_keys ADD COLUMN role TEXT NOT NULL DEFAULT ''")
    conn.execute("""CREATE TABLE hub_claim (
        slot INTEGER PRIMARY KEY CHECK (slot = 1),
        key_id TEXT NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL DEFAULT '',
        claimed_at REAL NOT NULL
    )""")
