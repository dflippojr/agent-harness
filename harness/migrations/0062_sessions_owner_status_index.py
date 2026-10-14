"""Session counts by owner (#668): `count_sessions` filters `owner_id = ? AND status IN (...)` for member account views
and admission. `sessions_owner_status` answers it from the index alone instead of walking each row past its large
`context` value. The list query's `(kind, owner_id, created_at)` index is 0061's `sessions_list`."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX sessions_owner_status ON sessions(owner_id, status)")
