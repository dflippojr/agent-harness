"""Index for the per-key request count (#669): `list_api_keys` counts each key's `endpoint_requests` rows, and the
table is never pruned. With only the `ts` index, every key scanned the whole table."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX endpoint_requests_key ON endpoint_requests(key_id)")
