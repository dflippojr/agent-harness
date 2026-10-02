"""Untrusted sources a session has read (taint.py, issue #262): JSON list of {kind, origin, first_seen}."""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE sessions ADD COLUMN taint TEXT NOT NULL DEFAULT '[]'")
