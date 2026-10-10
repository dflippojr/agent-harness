"""Optional catalog app id (#518): a label saying which listing or Hub entry a key or pairing code belongs to.
It grants nothing and is never part of a token secret. Older rows read back empty."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE api_keys ADD COLUMN catalog_app_id TEXT")
    conn.execute("ALTER TABLE pairing_codes ADD COLUMN catalog_app_id TEXT")
