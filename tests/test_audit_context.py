"""Audit actor context and owner review (#467)."""
from __future__ import annotations

import json
import sqlite3
import time

from harness import audit_context, migrations, operation_audit
from harness.admin import PREFIX
from harness.db import Database
import pytest

from test_household import H, OWNER, ALICE, create_member, household


@pytest.fixture
def hh(tmp_path):
    client, m = household(tmp_path)
    with client:
        yield client, m


def _key(client, name: str, kind: str = "owner", scopes=("admin",)) -> dict:
    r = client.post(f"{PREFIX}/keys", json={"name": name, "kind": kind, "scopes": list(scopes)}, headers=H(OWNER))
    assert r.status_code in (200, 201), r.text
    return r.json()


def _bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _rows(client, **params) -> dict:
    r = client.get(f"{PREFIX}/audit", params=params, headers=H(OWNER))
    assert r.status_code == 200, r.text
    return r.json()


def test_legacy_rows_survive_migration(tmp_path):
    path = tmp_path / "x.sqlite3"
    shipped = [step for step in migrations.discover() if step[0] <= 52]
    assert shipped[-1][0] == 52
    db = Database(path, migrations=shipped)  # an actual v52 schema, before the audit context columns
    db.conn.execute("INSERT INTO account_audit (ts, actor_id, target_id, action, outcome, detail) "
                    "VALUES (?, 'owner', 'u', 'create', 'ok', 'old')", (time.time(),))
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 52
    db.close()
    db = Database(path)
    row = db.audit_page()["items"][0]
    assert (row["actor_id"], row["detail"], row["actor_kind"], row["source"], row["key_id"], row["metadata"]) == (
        "owner", "old", "unknown", "unknown", "", {})
    db.insert_audit("owner", "u2", "create", "ok", context=audit_context.owner_context({"id": "k1"}))
    db.close()
    db = Database(path)
    assert [r["key_id"] for r in db.audit_page()["items"]] == ["k1", ""]
    db.close()


def test_pagination_same_timestamp_inserts_and_clock_step(tmp_path, monkeypatch):
    db = Database(tmp_path / "x.sqlite3")
    base = time.time()
    monkeypatch.setattr(time, "time", lambda: base)
    for i in range(13):  # the cursor logic is the same at any size; a page of 5 takes three pages
        db.insert_audit("owner", f"t{i}", "quota", "ok")
    first = db.audit_page(5)
    seen = [r["id"] for r in first["items"]]
    monkeypatch.setattr(time, "time", lambda: base - 3600)  # the clock steps backwards
    db.insert_audit("owner", "late", "quota", "ok")
    page = first
    while page["next_before_id"] is not None:
        page = db.audit_page(5, page["next_before_id"])
        seen += [r["id"] for r in page["items"]]
    assert len(seen) == len(set(seen)) == 13
    assert seen == sorted(seen, reverse=True)
    assert db.audit_page(5, actor_id="owner", target_id="t7")["items"][0]["target_id"] == "t7"
    assert db.audit_page(5, since=base, until=base + 1)["items"]
    assert db.audit_page(5, until=base)["items"][0]["target_id"] == "late"
    db.close()


def test_api_filters_bounds_and_cursor_errors(hh):
    client, _ = hh
    create_member(client, ALICE, "Alice")
    for bad in ({"limit": 0}, {"limit": 501}, {"before_id": 0}, {"since": 5, "until": 5}, {"since": "nan"}):
        assert client.get(f"{PREFIX}/audit", params=bad, headers=H(OWNER)).status_code in (400, 422), bad
    page = _rows(client, action="create", outcome="ok", limit=1)
    assert page["items"] and page["next_before_id"] is None
    assert _rows(client, action="nope")["items"] == []


def test_actor_context_for_ambient_and_two_owner_keys(hh):
    client, _ = hh
    k1, k2 = _key(client, "one"), _key(client, "two")
    a = create_member(client, ALICE, "Alice")
    r = client.patch(f"{PREFIX}/accounts/{a['user_id']}", json={"max_running": 2},
                     headers={**_bearer(k1["key"]), "X-Actor": "mallory", "X-Source": "local_cli"})
    assert r.status_code == 200, r.text
    client.patch(f"{PREFIX}/accounts/{a['user_id']}", json={"max_queued": 3}, headers=_bearer(k2["key"]))
    rows = {r["action"] + r["key_id"]: r for r in _rows(client)["items"]}
    ambient = rows["create"]
    assert (ambient["actor_id"], ambient["actor_kind"], ambient["key_id"], ambient["source"]) == (
        "owner", "owner", "", "admin_api")
    assert ambient["target_kind"] == "account"
    r1, r2 = rows["concurrency" + k1["id"]], rows["concurrency" + k2["id"]]
    assert r1["actor_kind"] == "owner_key" and r1["actor_id"] == "owner" and r1["source"] == "admin_api"
    assert r1["metadata"]["new_max_running"] == 2 and r2["metadata"]["new_max_queued"] == 3
    assert _rows(client, key_id=k2["id"])["items"] == [r2]


def test_metadata_allowlist_and_no_values(hh):
    client, _ = hh
    a = create_member(client, ALICE, "Alice Secret")
    client.patch(f"{PREFIX}/accounts/{a['user_id']}",
                 json={"display_name": "Renamed Sentinel", "disk_quota_bytes": 5_000_000}, headers=H(OWNER))
    text = client.get(f"{PREFIX}/audit", headers=H(OWNER)).text
    assert "Renamed Sentinel" not in text and ALICE not in text
    rows = {r["action"]: r for r in _rows(client)["items"]}
    assert rows["rename"]["metadata"] == {"fields": ["display_name"]}
    assert rows["quota"]["metadata"]["new_disk_quota_bytes"] == 5_000_000


SENTINEL = "SECRET/path/prompt/note/financial/health/command"
# (id, action, raw metadata, what survives). Only allowlisted, safely typed values are kept; nothing else is stored.
CLEAN_METADATA = [
    ("account-fields-and-types", "quota", {
        "fields": ["login", "/etc/passwd"], "path": "C:/secret", "new_disk_quota_bytes": "x", "token": "sk-1",
        "old_disk_quota_bytes": True, "reason": "free text secret"}, {"fields": ["login"]}),
    ("credential-grant", "provider_grant.set", {
        "grant_id": "pc-abc12", "secret_ref": "billing-sentinel-ref", "path": "C:/x", "backend": "claude",
        "policy": "api_key; drop", "fields": ["policy", "/etc/passwd"], "models": ["m"], "replaced": "yes",
        "reason": "free text"}, {"grant_id": "pc-abc12", "backend": "claude", "fields": ["policy"]}),
    ("key-scopes", "key.create", {"scopes": ["sessions", "bad scope!"], "kind": "x"}, {"scopes": ["sessions"]}),
    ("job-fields-must-be-a-list", "job.update", {"fields": SENTINEL, "enabled": 1}, {}),
    ("checkpoint-bounds", "checkpoint.rewind", {"turn": 2**53, "session_id": 42}, {}),
    ("maintenance-count", "maintenance.cleanup", {"removed": 3}, {"removed": 3}),
    ("every-operation-action", None, {
        "prompt": SENTINEL, "note": SENTINEL, "path": SENTINEL, "fields": [SENTINEL, "enabled"],
        "session_id": SENTINEL, "turn": True, "decision": SENTINEL, "reviewer_mode": SENTINEL, "expired": -1}, None),
]


@pytest.mark.parametrize("action, raw, expected", [row[1:] for row in CLEAN_METADATA],
                         ids=[row[0] for row in CLEAN_METADATA])
def test_clean_metadata_keeps_only_allowlisted_safe_values(action, raw, expected):
    if action is None:  # no operation action keeps a free-text value
        for operation in operation_audit.ACTIONS:
            assert SENTINEL not in json.dumps(audit_context.clean_metadata(operation, raw)), operation
        return
    assert audit_context.clean_metadata(action, raw) == expected


def test_audit_failure_rolls_back_account_change(hh, monkeypatch):
    client, m = hh
    a = create_member(client, ALICE, "Alice")

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(Database, "insert_audit", boom)
    r = client.patch(f"{PREFIX}/accounts/{a['user_id']}", json={"disk_quota_bytes": 123}, headers=H(OWNER))
    assert r.status_code == 503
    assert m.db.account_by_id(a["user_id"])["disk_quota_bytes"] != 123
    r = client.post(f"{PREFIX}/accounts", json={"login": "carol@example.com", "display_name": "C"},
                    headers=H(OWNER))
    assert r.status_code == 503 and m.db.account_by_login("carol@example.com") is None


def test_denial_rows_carry_context_and_reason(hh):
    client, _ = hh
    key = _key(client, "k")
    r = client.post(f"{PREFIX}/accounts", json={"login": OWNER, "display_name": "x"}, headers=_bearer(key["key"]))
    assert r.status_code == 400
    row = next(r for r in _rows(client)["items"] if r["outcome"] == "denied")
    assert row["key_id"] == key["id"] and row["metadata"] == {"reason": "login_is_owner"}
    assert row["detail"] == "login is an owner"


def test_retention_prunes_on_insert(tmp_path, monkeypatch):
    db = Database(tmp_path / "x.sqlite3")
    monkeypatch.setattr(time, "time", lambda: 1.0)
    db.insert_audit("owner", "old", "quota", "ok")
    monkeypatch.setattr(time, "time", lambda: 400 * 86400.0)
    db.insert_audit("owner", "new", "quota", "ok")
    assert [r["target_id"] for r in db.audit_page()["items"]] == ["new"]
    db.close()


def test_failed_create_leaves_no_directories(hh, monkeypatch):
    client, m = hh
    before = set(p.name for p in m.cfg.data_dir.rglob("u-*"))
    monkeypatch.setattr(Database, "insert_audit", lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    r = client.post(f"{PREFIX}/accounts", json={"login": "dave@example.com", "display_name": "D"}, headers=H(OWNER))
    assert r.status_code == 503
    assert set(p.name for p in m.cfg.data_dir.rglob("u-*")) == before
