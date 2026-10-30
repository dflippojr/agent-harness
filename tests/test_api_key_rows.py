"""The api_keys row each of the five minting paths writes (#843): every column, so folding them into one writer can't
drift. The random id, secret and timestamp are checked by shape."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

import pytest

from harness.db import Database

APP_ORIGIN = "https://app.example"
EMPTY = {"last_used_at": None, "revoked_at": None, "retention_days": None, "erase_after": None, "erased_at": None}


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "harness.sqlite3")


def _request(db: Database, rid: str, kind: str, origin: str, now: float, catalog_app_id: str = "") -> None:
    db.insert_pairing_request({"id": rid, "kind": kind, "name": f"{kind} request", "scopes": "sessions",
                               "catalog_app_id": catalog_app_id, "origin": origin, "state": "approved",
                               "created_at": now, "expires_at": now + 600, "key_id": ""})


def _create(kind: str, origins: list[str] | None, catalog_app_id: str):
    def mint(db: Database):
        return db.create_api_key("laptop", "inference sessions", kind, origins, catalog_app_id)
    return mint


def _pairing_code(catalog_app_id: str):
    def mint(db: Database):
        _row, code = db.create_pairing_code("paired app", APP_ORIGIN, "inference", 600, catalog_app_id)
        key, secret, error = db.redeem_pairing_code(code, APP_ORIGIN)
        assert error == ""
        return key, secret
    return mint


def _pairing_request(origin: str, catalog_app_id: str):
    def mint(db: Database):
        now = time.time()
        _request(db, "pr-app", "app", origin, now, catalog_app_id)
        return db.redeem_pairing_request("pr-app", now)
    return mint


def _hub_claim(origin: str):
    def mint(db: Database):
        now = time.time()
        _request(db, "pr-hub", "hub", origin, now, "hub-app")
        key, secret, error = db.redeem_hub_claim("pr-hub", now)
        assert error == ""
        return key, secret
    return mint


def _runner_code(db: Database):
    _row, code = db.create_runner_pairing_code("mac", "macrunner", 600)
    _pairing, key, secret, error = db.redeem_runner_pairing_code(code)
    assert error == ""
    return key, secret


# (path, mint, secret prefix, the row's fixed columns, the returned key's fixed fields)
CASES = [
    ("create device", _create("device", None, ""), "hk-",
     {"name": "laptop", "scopes": "inference sessions", "kind": "device", "origins": "[]", "catalog_app_id": "",
      "role": ""},
     {"name": "laptop", "scopes": "inference sessions", "kind": "device", "origins": [], "catalog_app_id": ""}),
    ("create app", _create("app", [APP_ORIGIN], "cat-1"), "ha-",
     {"name": "laptop", "scopes": "inference sessions", "kind": "app", "origins": '["https://app.example"]',
      "catalog_app_id": "cat-1", "role": ""},
     {"name": "laptop", "scopes": "inference sessions", "kind": "app", "origins": [APP_ORIGIN],
      "catalog_app_id": "cat-1"}),
    ("create owner", _create("owner", [], ""), "ho-",
     {"name": "laptop", "scopes": "inference sessions", "kind": "owner", "origins": "[]", "catalog_app_id": "",
      "role": ""},
     {"name": "laptop", "scopes": "inference sessions", "kind": "owner", "origins": [], "catalog_app_id": ""}),
    ("pairing code", _pairing_code("cat-2"), "ha-",
     {"name": "paired app", "scopes": "inference", "kind": "app", "origins": '["https://app.example"]',
      "catalog_app_id": "cat-2", "role": ""},
     {"name": "paired app", "scopes": "inference", "kind": "app", "origins": [APP_ORIGIN], "catalog_app_id": "cat-2"}),
    ("pairing code, no catalog app", _pairing_code(""), "ha-",
     {"name": "paired app", "scopes": "inference", "kind": "app", "origins": '["https://app.example"]',
      "catalog_app_id": "", "role": ""},
     {"name": "paired app", "scopes": "inference", "kind": "app", "origins": [APP_ORIGIN], "catalog_app_id": ""}),
    ("pairing request, browser", _pairing_request(APP_ORIGIN, "cat-3"), "ha-",
     {"name": "app request", "scopes": "sessions", "kind": "app", "origins": '["https://app.example"]',
      "catalog_app_id": "cat-3", "role": ""},
     {"name": "app request", "scopes": "sessions", "kind": "app", "origins": [APP_ORIGIN],
      "catalog_app_id": "cat-3"}),
    ("pairing request, native", _pairing_request("", ""), "ha-",
     {"name": "app request", "scopes": "sessions", "kind": "app", "origins": "[]", "catalog_app_id": "",
      "role": ""},
     {"name": "app request", "scopes": "sessions", "kind": "app", "origins": [], "catalog_app_id": ""}),
    ("hub claim, browser", _hub_claim(APP_ORIGIN), "ho-",
     {"name": "hub request", "scopes": "admin", "kind": "owner", "origins": '["https://app.example"]',
      "catalog_app_id": "hub-app", "role": "hub"},
     {"name": "hub request", "scopes": "admin", "kind": "owner", "role": "hub", "origins": [APP_ORIGIN],
      "catalog_app_id": "hub-app"}),
    ("hub claim, native", _hub_claim(""), "ho-",
     {"name": "hub request", "scopes": "admin", "kind": "owner", "origins": "[]", "catalog_app_id": "hub-app",
      "role": "hub"},
     {"name": "hub request", "scopes": "admin", "kind": "owner", "role": "hub", "origins": [],
      "catalog_app_id": "hub-app"}),
    ("runner pairing code", _runner_code, "ho-",
     {"name": "mac", "scopes": "admin", "kind": "owner", "origins": "[]", "catalog_app_id": None, "role": ""},
     {"name": "mac", "scopes": "admin", "kind": "owner", "origins": []}),
]


@pytest.mark.parametrize(("mint", "prefix", "row_fields", "key_fields"), [c[1:] for c in CASES],
                         ids=[c[0] for c in CASES])
def test_each_path_writes_the_same_api_keys_row(db: Database, mint, prefix: str, row_fields: dict,
                                                key_fields: dict):
    before = time.time()
    key, secret = mint(db)
    with db.lock:
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM api_keys")]
    assert len(rows) == 1
    row = rows[0]
    assert re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{43}", secret)
    assert re.fullmatch(r"k-[0-9a-f]{8}", row["id"])
    assert row["hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert before <= row["created_at"] <= time.time()
    assert row == {"id": row["id"], "prefix": secret[:10], "hash": row["hash"], "created_at": row["created_at"],
                   **EMPTY, **row_fields}
    # the returned key is what callers put in API responses: the same fields, no hash
    assert key == {"id": row["id"], "prefix": secret[:10], "created_at": row["created_at"], **key_fields}


def _fail_on_update(db: Database, table: str) -> None:
    with db.lock:
        db.conn.execute(f"CREATE TRIGGER fail_{table} BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'boom'); END")


def _fail_on_insert(db: Database, table: str) -> None:
    with db.lock:
        db.conn.execute(f"CREATE TRIGGER fail_{table} BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'boom'); END")


@pytest.mark.parametrize(("mint", "fail"), [
    (_pairing_code(""), lambda db: _fail_on_update(db, "pairing_codes")),
    (_pairing_request(APP_ORIGIN, ""), lambda db: _fail_on_update(db, "pairing_requests")),
    (_hub_claim(""), lambda db: _fail_on_insert(db, "hub_claim")),
    (_hub_claim(""), lambda db: _fail_on_update(db, "pairing_requests")),
    (_runner_code, lambda db: _fail_on_update(db, "runner_pairing_codes")),
], ids=["pairing code", "pairing request", "hub claim record", "hub claim request", "runner pairing code"])
def test_a_failed_neighbouring_write_leaves_no_key(db: Database, mint, fail):
    fail(db)
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        mint(db)
    with db.lock:
        assert db.conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0
