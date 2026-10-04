"""Retention and erasure of App data (#330 decision 5): a session's own `retention_days`, an App's default one, and a
revoked App's scheduled erasure (`erase_after`) and its tombstone (`erased_at`) in the registry."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE sessions ADD COLUMN retention_days REAL")
    conn.execute("ALTER TABLE api_keys ADD COLUMN retention_days REAL")
    conn.execute("ALTER TABLE api_keys ADD COLUMN erase_after REAL")
    conn.execute("ALTER TABLE api_keys ADD COLUMN erased_at REAL")
