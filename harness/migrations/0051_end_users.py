"""App end users (#365): the registry kept in each App's own store, the end user a session ran for, and the per-end-user
usage tally."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS end_users (id TEXT PRIMARY KEY, created_at REAL NOT NULL)")
    conn.execute("ALTER TABLE sessions ADD COLUMN end_user TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE usage ADD COLUMN end_user TEXT NOT NULL DEFAULT ''")
