"""Per-session tool profile (#638): `default` or `bash-only`, frozen on the session at creation and copied onto each
usage row, so tokens and cost can be grouped by profile. Older rows read back `default`."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE sessions ADD COLUMN tool_profile TEXT NOT NULL DEFAULT 'default'")
    conn.execute("ALTER TABLE usage ADD COLUMN tool_profile TEXT NOT NULL DEFAULT 'default'")
