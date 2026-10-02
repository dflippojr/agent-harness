"""Nightly canary eval results, one row per deployed commit (issue #265)."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canary_results (
            sha TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL,
            tries INTEGER NOT NULL DEFAULT 1,
            outcomes TEXT NOT NULL DEFAULT '[]',
            passes INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            pass_rate REAL,
            turns INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            wall_seconds REAL NOT NULL DEFAULT 0,
            baseline_sha TEXT NOT NULL DEFAULT '',
            baseline_rate REAL,
            alerted INTEGER NOT NULL DEFAULT 0
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_canary_results_started ON canary_results(started_at)")
