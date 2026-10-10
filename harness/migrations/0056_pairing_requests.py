"""Zero-touch App pairing requests (#519): a short-lived record of an App asking to pair (or a slot the owner armed
for one). It stores the hash of the App's PKCE code_challenge and never a token or the verifier; the `ha-` key is
minted only when the App redeems. `source` is a hash of who asked (origin, tailnet login or address), for the cap."""
import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE pairing_requests (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'app',
        name TEXT NOT NULL,
        scopes TEXT NOT NULL,
        catalog_app_id TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        challenge_hash TEXT NOT NULL DEFAULT '',
        match_code TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL,
        armed INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        approved_at REAL,
        finished_at REAL,
        key_id TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("CREATE INDEX pairing_requests_state ON pairing_requests(state, expires_at)")
    conn.execute("CREATE INDEX pairing_requests_source ON pairing_requests(source, created_at)")
