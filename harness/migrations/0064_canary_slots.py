"""One row per nightly canary slot and how it ended, so a canary that stops running is noticed (issue #732)."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canary_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL,
            sha TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            alerted_at REAL  -- when the silence alarm was sent on this slot; NULL if it wasn't
        )""")
