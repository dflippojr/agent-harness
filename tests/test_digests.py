"""#841: token, code and ticket hashes are SHA-256 hex digests of the UTF-8 text. These pin the stored values to
literal digests, so the mint and lookup paths stay byte-identical whatever computes them."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.db import Database
from harness.delegate_edit import text_digest
from harness.digests import sha256_hex

# sha256("<prefix>-fixed"), computed once outside the harness
HK_FIXED = "18caa7c9597bf905a3499d5f955198be8e4a112e536dc715ea87236cd88a1df8"
HA_FIXED = "3d70a7e10f5df3f0e79c22890824563197636194c47f8843748045b0f3d1dbd6"
HO_FIXED = "a5ff6ef216dc8c3ff0a46dd6d90be4e526825cbdd154873da688a58790f2d9ae"
HP_FIXED = "fa59c8b270586f4fac7e55dd8a1fbbeb1750819f462067beb6ff71fff28c0d98"
HRP_FIXED = "8a276afaae55a87beef532352e63773f71f310824922f3344dbb0fcb987ce1f8"
HS_FIXED = "6af71b3e49f9bc377ea02fce459dbbdb940d22fb1764aadb86fa327b9cc86078"


@pytest.mark.parametrize(("text", "expected"), [
    ("", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    ("h\u00e9llo \u2713", "5657cdef8a85a584e0e961e6f8247cf5d3f8ed21496ed6fdbcfd43a761e94245"),  # UTF-8, not Latin-1
])
def test_sha256_hex_matches_fixed_vectors(text: str, expected: str):
    assert sha256_hex(text) == expected
    assert text_digest(text) == expected  # #155 receipts keep the same convention


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    monkeypatch.setattr("harness.db.secrets.token_urlsafe", lambda n=None: "fixed")
    return Database(tmp_path / "harness.sqlite3")


def _hash(db: Database, table: str, column: str, value: str) -> str:
    with db.lock:
        return db.conn.execute(f"SELECT hash FROM {table} WHERE {column} = ?", (value,)).fetchone()["hash"]


def test_api_key_hash_is_the_literal_digest(db: Database):
    row, key = db.create_api_key("laptop")
    assert key == "hk-fixed"
    assert _hash(db, "api_keys", "id", row["id"]) == HK_FIXED
    found = db.api_key_by_secret(key)
    assert found is not None and found["id"] == row["id"]


def test_pairing_code_and_its_key_hash_to_the_literal_digests(db: Database):
    row, code = db.create_pairing_code("app", "https://app.example", "inference", 600)
    assert code == "hp-fixed"
    assert _hash(db, "pairing_codes", "id", row["id"]) == HP_FIXED
    key, secret, error = db.redeem_pairing_code(code, "https://app.example")
    assert error == "" and key is not None and secret == "ha-fixed"
    assert _hash(db, "api_keys", "id", key["id"]) == HA_FIXED


def test_runner_pairing_code_and_its_key_hash_to_the_literal_digests(db: Database):
    row, code = db.create_runner_pairing_code("mac", "macrunner", 600)
    assert code == "hrp-fixed"
    assert _hash(db, "runner_pairing_codes", "id", row["id"]) == HRP_FIXED
    _pairing, key, secret, error = db.redeem_runner_pairing_code(code)
    assert error == "" and key is not None and secret == "ho-fixed"
    assert _hash(db, "api_keys", "id", key["id"]) == HO_FIXED


def test_stream_ticket_hash_is_the_literal_digest(db: Database):
    row, _key = db.create_api_key("laptop")
    ticket, _expires = db.create_stream_ticket(row["id"], "s1", "https://app.example")
    assert ticket == "hs-fixed"
    with db.lock:
        stored = db.conn.execute("SELECT hash FROM stream_tickets").fetchone()["hash"]
    assert stored == HS_FIXED
    found = db.stream_ticket_key(ticket, "s1", "https://app.example")
    assert found is not None and found["id"] == row["id"]
    assert db.stream_ticket_origin_active(ticket, "https://app.example")
