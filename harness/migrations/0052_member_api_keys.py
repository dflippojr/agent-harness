"""Household members' own provider API keys (#393): the encrypted key per (member, backend) in the main store.
Only `ciphertext` holds the secret, sealed under a key that lives outside the database and its backups."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS member_api_keys ("
                 "user_id TEXT NOT NULL, backend TEXT NOT NULL, ciphertext BLOB NOT NULL, last4 TEXT NOT NULL, "
                 "created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (user_id, backend))")
