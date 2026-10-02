"""Per-turn workspace checkpoints (checkpoints.py, issue #261): one row per snapshot, a session-level turn counter
(run turns reset every run) and fork lineage."""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            session_id TEXT NOT NULL,
            turn INTEGER NOT NULL,
            sha TEXT NOT NULL,
            head TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '',
            event_seq INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            PRIMARY KEY (session_id, turn)
        )""")
    conn.execute("ALTER TABLE sessions ADD COLUMN turn_seq INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE sessions ADD COLUMN parent_id TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE sessions ADD COLUMN fork_turn INTEGER NOT NULL DEFAULT 0")
