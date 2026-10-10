"""Optional catalog app id on keys and pairing codes (#518): a label only, never part of a token secret."""
from __future__ import annotations

import re

import pytest

from harness import audit_context, catalog_ids, cli, migrations
from harness.admin import PREFIX
from harness.db import Database

from test_household import H, OWNER, household

ORIGIN = "https://control.example"
CATALOG_ID = "com.example.shopping-list"
TOKEN_SHAPE = re.compile(r"(ha|hk|ho)-[A-Za-z0-9_-]{43}")
INVALID = ["Com.Example.App", "com.example app", "com." + "a" * 60 + "." + "b" * 60, "ha-shop.example",
           "ho-x.example", "hp-x.example", "single", "com..example", 7]


@pytest.fixture
def hh(tmp_path):
    client, m = household(tmp_path)
    with client:
        yield client, m


def _audit(client, action: str, **params) -> list[dict]:
    r = client.get(f"{PREFIX}/audit", params={"limit": 500, "action": action, **params}, headers=H(OWNER))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _pairing(client, **extra):
    return client.post("/pairing-codes", json={"name": "shop", "origin": ORIGIN, "scopes": ["sessions"], **extra})


def test_pairing_code_carries_the_id_to_the_key_it_mints(hh):
    client, _ = hh
    approved = _pairing(client, catalog_app_id=CATALOG_ID)
    assert approved.status_code == 201, approved.text
    assert approved.json()["catalog_app_id"] == CATALOG_ID
    listed = next(p for p in client.get("/pairing-codes").json() if p["id"] == approved.json()["id"])
    assert listed["catalog_app_id"] == CATALOG_ID and "code" not in listed

    paired = client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": approved.json()["code"]})
    assert paired.status_code == 201
    app = paired.json()["app"]
    assert app["catalog_app_id"] == CATALOG_ID
    key = next(k for k in client.get(f"{PREFIX}/keys", headers=H(OWNER)).json() if k["id"] == app["id"])
    assert key["catalog_app_id"] == CATALOG_ID

    create = _audit(client, "pairing.create", outcome="ok")[0]
    redeem = _audit(client, "pairing.redeem", outcome="ok")[0]
    assert create["metadata"]["catalog_app_id"] == redeem["metadata"]["catalog_app_id"] == CATALOG_ID
    # Only the one-time create responses carry a secret; the listings and the audit never do.
    for text in (client.get("/pairing-codes").text, client.get("/keys").text,
                 client.get(f"{PREFIX}/audit?limit=500", headers=H(OWNER)).text):
        assert approved.json()["code"] not in text and paired.json()["token"] not in text


def test_key_create_reports_and_audits_the_id(hh):
    client, _ = hh
    created = client.post(f"{PREFIX}/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions"],
                                                  "catalog_app_id": CATALOG_ID}, headers=H(OWNER))
    assert created.status_code == 201, created.text
    assert created.json()["catalog_app_id"] == CATALOG_ID
    listed = next(k for k in client.get("/keys").json() if k["id"] == created.json()["id"])
    assert listed["catalog_app_id"] == CATALOG_ID and "key" not in listed and "hash" not in listed
    row = _audit(client, "key.create", target_id=created.json()["id"])[0]
    assert row["metadata"]["catalog_app_id"] == CATALOG_ID


def test_absent_id_works_as_before(hh):
    client, _ = hh
    approved = _pairing(client).json()
    assert approved["catalog_app_id"] == ""
    app = client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": approved["code"]}).json()["app"]
    assert app["catalog_app_id"] == ""
    device = client.post("/keys", json={"name": "d", "scopes": ["inference"]}).json()
    assert {k["id"]: k["catalog_app_id"] for k in client.get("/keys").json()} == {app["id"]: "", device["id"]: ""}
    assert "catalog_app_id" not in _audit(client, "pairing.redeem", outcome="ok")[0]["metadata"]
    assert "catalog_app_id" not in _audit(client, "key.create", outcome="ok")[0]["metadata"]


@pytest.mark.parametrize("value", INVALID)
def test_invalid_ids_are_refused_with_a_denied_row(hh, value):
    client, _ = hh
    for path, headers in (("/keys", {}), (f"{PREFIX}/keys", H(OWNER))):
        r = client.post(path, json={"name": "k", "kind": "app", "scopes": ["sessions"], "catalog_app_id": value},
                        headers=headers)
        assert r.status_code == 400
    if isinstance(value, str):
        assert _pairing(client, catalog_app_id=value).status_code == 400
        pairing_denied = _audit(client, "pairing.create", outcome="denied")
        assert pairing_denied and pairing_denied[0]["metadata"] == {"reason": "invalid_request"}
        assert value not in client.get(f"{PREFIX}/audit?limit=500", headers=H(OWNER)).text
    else:  # a non-string fails request validation before the handler, as any mistyped field does
        assert _pairing(client, catalog_app_id=value).status_code == 422
    key_denied = _audit(client, "key.create", outcome="denied")
    assert len(key_denied) == 2 and all(r["metadata"] == {"reason": "invalid_request"} for r in key_denied)
    assert client.get("/keys").json() == [] and client.get("/pairing-codes").json() == []


def test_validator_accepts_the_design_pattern_and_the_length_limit():
    assert catalog_ids.normalize(None) == catalog_ids.normalize("") == ""
    longest = ".".join(["a" * 23] * 5)
    assert len(longest) == 119 and catalog_ids.normalize(longest) == longest
    for ok in ("com.example.app", "dev.agent-harness.web", "io.x9.y-z"):
        assert catalog_ids.normalize(ok) == ok
    for bad in INVALID:
        with pytest.raises(ValueError):
            catalog_ids.normalize(bad)
    assert audit_context.clean_metadata("key.create", {"catalog_app_id": "HA-Bad Value"}) == {}


def test_minted_secret_has_the_same_shape_with_or_without_an_id(hh):
    client, _ = hh
    plain = client.post("/keys", json={"name": "a", "kind": "app", "scopes": ["sessions"]}).json()["key"]
    tagged = client.post("/keys", json={"name": "b", "kind": "app", "scopes": ["sessions"],
                                        "catalog_app_id": CATALOG_ID}).json()["key"]
    code = _pairing(client, catalog_app_id=CATALOG_ID).json()["code"]
    paired = client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": code}).json()["token"]
    for secret in (plain, tagged, paired):
        assert TOKEN_SHAPE.fullmatch(secret) and secret.startswith("ha-")
        assert CATALOG_ID not in secret and "shopping" not in secret
    assert len(plain) == len(tagged) == len(paired)


def test_migration_adds_an_empty_column_to_existing_rows(tmp_path):
    shipped = migrations.discover()
    assert shipped[-1][0] == 55
    path = tmp_path / "harness.db"
    old = Database(path, migrations=shipped[:-1])
    old.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at, scopes, kind, origins) "
                     "VALUES ('k-old', 'legacy', 'ha-xxxxxxx', 'h', 1, 'sessions', 'app', '[]')")
    old.conn.execute("INSERT INTO pairing_codes (id, hash, name, origin, scopes, created_at, expires_at) "
                     "VALUES ('p-old', 'h2', 'legacy', ?, 'sessions', 1, 9e12)", (ORIGIN,))
    old.conn.commit()
    assert "catalog_app_id" not in {r["name"] for r in old.conn.execute("PRAGMA table_info(api_keys)")}
    old.close()
    db = Database(path, migrations=shipped)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 55
    assert next(k for k in db.list_api_keys() if k["id"] == "k-old")["catalog_app_id"] == ""
    assert next(p for p in db.list_pairing_codes() if p["id"] == "p-old")["catalog_app_id"] == ""
    assert list((tmp_path / "pre-migration").glob("harness-v54-*.sqlite3"))
    db.close()


@pytest.mark.parametrize("argv,field", [
    (["pairing-codes", "create", "shop", ORIGIN, "--catalog-app-id", CATALOG_ID], "catalog_app_id"),
    (["keys", "create", "shop", "--kind", "app", "--catalog-app-id", CATALOG_ID], "catalog_app_id"),
])
def test_cli_flags_send_the_id(argv, field):
    method, _, kwargs = cli.admin_request(cli._build_parser().parse_args(argv))
    assert method == "POST" and kwargs["json"][field] == CATALOG_ID
