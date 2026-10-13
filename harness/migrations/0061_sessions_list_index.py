"""Agents list paging (#663): `sessions_list` serves each page of one owner's sessions of one kind, newest first by
`(created_at, id)`, and `sessions_status` finds the active ones (running, queued or parked) whatever their age."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX sessions_list ON sessions(kind, owner_id, created_at, id)")
    conn.execute("CREATE INDEX sessions_status ON sessions(status)")
