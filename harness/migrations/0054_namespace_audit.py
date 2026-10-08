"""Private App/member operational trails; never queried through owner account audit."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE namespace_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        namespace TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
        actor_id TEXT NOT NULL, actor_kind TEXT NOT NULL, key_id TEXT NOT NULL,
        source TEXT NOT NULL, target_id TEXT NOT NULL, target_kind TEXT NOT NULL,
        action TEXT NOT NULL, outcome TEXT NOT NULL, metadata TEXT NOT NULL,
        expires_at REAL, checksum TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX namespace_audit_scope ON namespace_audit(namespace, id)")
    conn.execute("CREATE INDEX namespace_audit_session ON namespace_audit(session_id)")
