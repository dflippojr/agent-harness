"""Stables' registry name (#334 stage d): the reserved App `app-web` (#330 decision 4) was registered as "Agent Harness
Web", the client's old name. Rename only that default; the row's id, kind, store and sessions stay as they are."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE api_keys SET name = 'Stables' "
                 "WHERE id = 'app-web' AND kind = 'web' AND name = 'Agent Harness Web'")
