"""Why a canary run ended the way it did, e.g. the config problem that skipped it (issue #316)."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE canary_results ADD COLUMN note TEXT NOT NULL DEFAULT ''")
