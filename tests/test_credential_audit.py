"""Audit of credential, pairing and provider-grant lifecycle changes (#468)."""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from harness import audit_context, credential_audit
from harness.admin import PREFIX
from harness.api import create_app
from harness.config import BackendConfig, RunnerConfig
from harness.db import Database
from harness.manager import Manager

from test_household import H, OWNER, ALICE, BOB, create_member, household
from test_daemon import make_cfg

ORIGIN = "https://control.example"
SENTINEL_KEY = "sk-ant-api03-SENTINELkey0a1b2c3d4e5f6a7b8c"
SECRET_FILE_SENTINEL = "never-in-audit-secret-file-body"
REF = "billing-sentinel-ref"


def _boom(*_args, **_kwargs):
    raise sqlite3.OperationalError("disk full")


@pytest.fixture
def hh(tmp_path):
    client, m = household(tmp_path)
    secret = tmp_path / "grant.key"
    secret.write_text(SECRET_FILE_SENTINEL, encoding="utf-8")
    m.cfg.provider_secret_files = {REF: str(secret)}
    m.cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    m.cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.6-sol")
    with client:
        yield client, m


def _bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _rows(client, **params) -> list[dict]:
    r = client.get(f"{PREFIX}/audit", params={"limit": 500, **params}, headers=H(OWNER))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _dump(client) -> str:
    return client.get(f"{PREFIX}/audit?limit=500", headers=H(OWNER)).text


def _owner_key(client, name="k") -> dict:
    r = client.post(f"{PREFIX}/keys", json={"name": name, "kind": "owner", "scopes": ["admin"]}, headers=H(OWNER))
    assert r.status_code == 201, r.text
    return r.json()


def _by_action(client, action, **params) -> list[dict]:
    return _rows(client, action=action, **params)


def test_two_owner_tokens_chain_survives_restart_and_erasure(tmp_path):
    client, m = household(tmp_path)
    with client:
        k1, k2 = _owner_key(client, "one"), _owner_key(client, "two")
        app = client.post("/keys", json={"name": "app", "kind": "app", "scopes": ["sessions"]},
                          headers=_bearer(k1["key"])).json()
        assert client.delete(f"{PREFIX}/keys/{app['id']}", headers=_bearer(k2["key"])).status_code == 204
        restored = client.post(f"{PREFIX}/apps/{app['id']}/restore", headers=_bearer(k1["key"]))
        assert restored.status_code == 200
        m.db.mark_app_erased(app["id"])  # the App's payload is gone; the grant history must stay
        db_path = m.cfg.db_path
    db = Database(db_path)
    rows = db.audit_page(500, target_id=app["id"])["items"][::-1]
    assert [(r["action"], r["key_id"], r["outcome"]) for r in rows] == [
        ("key.create", k1["id"], "ok"), ("key.revoke", k2["id"], "ok"), ("app.restore", k1["id"], "ok")]
    assert rows[0]["metadata"] == {"key_id": app["id"], "kind": "app", "scopes": ["sessions"]}
    assert [r["actor_kind"] for r in rows] == ["owner_key", "owner_key", "owner_key"]
    db.close()


def test_key_rows_alias_dedup_noop_and_denial(hh):
    client, _ = hh
    created = client.post(f"{PREFIX}/keys", json={"name": "d", "kind": "device", "scopes": ["sessions"]},
                          headers=H(OWNER)).json()
    rows = _by_action(client, "key.create", target_id=created["id"])
    assert len(rows) == 1  # the alias and the core route are one change
    row = rows[0]
    assert (row["actor_id"], row["source"], row["target_kind"], row["outcome"]) == (
        "owner", "admin_api", "api_key", "ok")
    assert row["ts"] > 0
    assert client.delete(f"/keys/{created['id']}").status_code == 204
    assert client.delete(f"/keys/{created['id']}").status_code == 404           # idempotent retry
    revokes = _by_action(client, "key.revoke")
    assert [(r["outcome"], r["target_id"]) for r in revokes] == [("noop", created["id"]), ("ok", created["id"])]
    assert revokes[0]["metadata"]["reason"] == "already_revoked"
    assert revokes[1]["source"] == "legacy_api"
    assert client.delete("/keys/k-attacker-guess").status_code == 404
    unknown = _by_action(client, "key.revoke")[0]
    assert unknown["target_id"] == "" and "attacker" not in str(unknown)
    assert client.post("/keys", json={"name": "", "kind": "bogus"}).status_code == 400
    denied = _by_action(client, "key.create", outcome="denied")
    assert denied and denied[0]["target_id"] == "" and denied[0]["metadata"] == {"reason": "invalid_request"}


def test_pairing_lifecycle_race_and_safe_refusals(hh):
    client, m = hh
    approved = client.post("/pairing-codes", json={"name": "Sentinel Name", "origin": ORIGIN,
                                                   "scopes": ["sessions"]}).json()
    code, pid = approved["code"], approved["id"]
    # the origin guard only lets an origin with an active approved code reach the handler at all
    client.post("/pairing-codes", json={"name": "e", "origin": "https://evil.example", "scopes": ["sessions"]})
    assert client.post("/api/v1/pair", headers={"Origin": "https://evil.example"},
                       json={"code": code}).status_code == 400
    assert client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": "hp-guessed-code"}).status_code == 400
    client.post("/pairing-codes", json={"name": "keep", "origin": ORIGIN, "scopes": ["sessions"]})  # origin stays open
    results = []

    def redeem():
        results.append(client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": code}).status_code)
    threads = [threading.Thread(target=redeem) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201, 400, 400, 400]
    ok = _by_action(client, "pairing.redeem", outcome="ok")
    assert len(ok) == 1 and ok[0]["actor_kind"] == "app"
    assert ok[0]["actor_id"] == ok[0]["key_id"] == ok[0]["target_id"]
    assert ok[0]["metadata"]["pairing_id"] == pid and ok[0]["source"] == "app_api"
    denied = _by_action(client, "pairing.redeem", outcome="denied")
    assert {"origin_mismatch", "unknown_code", "code_used"} <= {r["metadata"]["reason"] for r in denied}
    assert all(r["actor_kind"] == "unknown" and r["target_id"] == "" for r in denied)
    other = client.post("/pairing-codes", json={"name": "x", "origin": ORIGIN, "scopes": ["sessions"]}).json()
    assert client.delete(f"/pairing-codes/{other['id']}").status_code == 204
    assert client.delete(f"/pairing-codes/{other['id']}").status_code == 404
    assert [r["outcome"] for r in _by_action(client, "pairing.revoke")] == ["noop", "ok"]
    dump = _dump(client)
    for sentinel in (code, "Sentinel Name", ORIGIN, "evil.example", "hp-guessed-code"):
        assert sentinel not in dump


def test_expired_pairing_code_is_a_safe_refusal(hh):
    client, m = hh
    approved = client.post("/pairing-codes", json={"name": "n", "origin": ORIGIN, "scopes": ["sessions"]}).json()
    client.post("/pairing-codes", json={"name": "n2", "origin": ORIGIN, "scopes": ["sessions"]})  # keeps the origin open
    with m.db.main.lock:
        m.db.main.conn.execute("UPDATE pairing_codes SET expires_at = 1 WHERE id = ?", (approved["id"],))
    assert client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": approved["code"]}).status_code == 400
    assert _by_action(client, "pairing.redeem")[0]["metadata"] == {"reason": "code_expired"}


def test_audit_failure_rolls_back_everything(hh, monkeypatch):
    client, m = hh
    approved = client.post("/pairing-codes", json={"name": "n", "origin": ORIGIN, "scopes": ["sessions"]}).json()
    app = client.post("/keys", json={"name": "a", "kind": "app", "scopes": ["sessions"]}).json()
    keys_before = len(m.db.list_api_keys())
    monkeypatch.setattr(Database, "insert_audit", _boom)
    assert client.post("/keys", json={"name": "b", "kind": "app", "scopes": ["sessions"]}).status_code == 503
    assert len(m.db.list_api_keys()) == keys_before
    r = client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": approved["code"]})
    assert r.status_code == 503 and "token" not in r.text
    assert len(m.db.list_api_keys()) == keys_before
    assert client.delete(f"/keys/{app['id']}").status_code == 503
    assert m.db.get_api_key(app["id"])["revoked_at"] is None
    grant = {"app_id": app["id"], "backend": "claude", "secret_ref": REF, "policy": "api_key", "models": []}
    assert client.post(f"{PREFIX}/provider-credentials", json=grant, headers=H(OWNER)).status_code == 503
    assert m.db.list_app_provider_credentials() == []
    assert client.put(f"{PREFIX}/apps/{app['id']}/retention", json={"retention_days": 5},
                      headers=H(OWNER)).status_code == 503
    assert m.db.get_api_key(app["id"]).get("retention_days") is None
    monkeypatch.undo()
    assert client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": approved["code"]}).status_code == 201


def test_restore_rotation_and_retention_rows(hh):
    client, m = hh
    app = client.post("/keys", json={"name": "a", "kind": "app", "scopes": ["sessions"]}).json()
    client.delete(f"/keys/{app['id']}")
    restored = client.post(f"{PREFIX}/apps/{app['id']}/restore", headers=H(OWNER)).json()
    assert restored["key"] != app["key"]
    assert client.post(f"{PREFIX}/apps/nope/restore", headers=H(OWNER)).status_code == 404
    rows = _by_action(client, "app.restore")
    assert [r["outcome"] for r in rows] == ["noop", "ok"] and rows[1]["metadata"]["kind"] == "app"
    assert rows[0]["target_id"] == "" and rows[0]["metadata"] == {"reason": "grace_over"}
    client.put(f"{PREFIX}/apps/{app['id']}/retention", json={"retention_days": 7}, headers=H(OWNER))
    client.put(f"{PREFIX}/apps/{app['id']}/retention", json={"retention_days": None}, headers=H(OWNER))
    ret = _by_action(client, "app.retention")
    assert ret[1]["metadata"] == {"app_id": app["id"], "old_retention_days": None, "new_retention_days": 7}
    assert ret[0]["metadata"]["old_retention_days"] == 7 and ret[0]["metadata"]["new_retention_days"] is None
    assert client.put(f"{PREFIX}/apps/nope/retention", json={"retention_days": 1},
                      headers=H(OWNER)).status_code == 404
    assert _by_action(client, "app.retention")[0]["target_id"] == ""
    text = _dump(client)
    assert restored["key"] not in text and app["key"] not in text


def test_provider_grant_set_replace_revoke_and_sentinels(hh):
    client, m = hh
    app = client.post("/keys", json={"name": "a", "kind": "app", "scopes": ["sessions"]}).json()
    body = {"app_id": app["id"], "backend": "claude", "secret_ref": REF, "policy": "api_key",
            "models": ["model-sentinel-1"]}
    first = client.post(f"{PREFIX}/provider-credentials", json=body, headers=H(OWNER)).json()
    second = client.post(f"{PREFIX}/provider-credentials", json={**body, "policy": "subscription", "secret_ref": "",
                                                                  "models": []}, headers=H(OWNER)).json()
    assert client.post(f"{PREFIX}/provider-credentials", json={**body, "backend": "nope"},
                       headers=H(OWNER)).status_code == 400
    assert client.delete(f"{PREFIX}/provider-credentials/{second['id']}", headers=H(OWNER)).status_code == 204
    assert client.delete(f"{PREFIX}/provider-credentials/{second['id']}", headers=H(OWNER)).status_code == 404
    sets = _by_action(client, "provider_grant.set", outcome="ok")[::-1]
    assert sets[0]["metadata"] == {"grant_id": first["id"], "app_id": app["id"], "backend": "claude",
                                   "policy": "api_key", "fields": ["backend", "models", "policy", "secret_ref"],
                                   "replaced": False}
    assert sets[1]["metadata"]["previous_grant_id"] == first["id"] and sets[1]["metadata"]["replaced"] is True
    assert sets[1]["metadata"]["fields"] == ["models", "policy", "secret_ref"]
    assert client.post(f"{PREFIX}/provider-credentials", json={**body, "app_id": "k-missing"},
                       headers=H(OWNER)).status_code == 404
    denied = _by_action(client, "provider_grant.set", outcome="denied")  # newest first
    assert [r["metadata"]["reason"] for r in denied] == ["not_found", "invalid_request"]
    assert all(r["target_id"] == "" for r in denied) and "k-missing" not in str(denied)
    assert [r["outcome"] for r in _by_action(client, "provider_grant.revoke")] == ["noop", "ok"]
    dump = _dump(client)
    for sentinel in (REF, SECRET_FILE_SENTINEL, "model-sentinel-1", "grant.key"):
        assert sentinel not in dump
    m.db.mark_app_erased(app["id"])  # erasing the payload never touches the grant history
    assert _by_action(client, "provider_grant.set", outcome="ok")


def test_member_key_set_replace_delete_test_and_isolation(hh):
    client, m = hh
    a, b = create_member(client, ALICE, "Alice")["user_id"], create_member(client, BOB, "Bob")["user_id"]
    outcomes = iter([(True, ""), (False, "provider body sentinel"), (None, "down")])
    m.member_keys._probe = lambda _b, _k: next(outcomes)
    url = "/api/v1/me/api-keys/claude"
    assert client.put(url, json={"key": SENTINEL_KEY}, headers=H(ALICE)).status_code == 200
    assert client.put(url, json={"key": SENTINEL_KEY[:-1] + "Z"}, headers=H(ALICE)).status_code == 200
    assert client.put(url, json={"key": "bad"}, headers=H(ALICE)).status_code == 400
    for _ in range(3):
        assert client.post(url + "/test", headers=H(ALICE)).status_code == 200
    assert client.delete(url, headers=H(ALICE)).status_code == 200
    assert client.delete(url, headers=H(ALICE)).status_code == 200
    assert client.post(url + "/test", headers=H(ALICE)).status_code == 409
    assert client.post(url + "/test", headers=H(BOB)).status_code == 409           # Alice's key is not Bob's
    sets = _by_action(client, "member_key.set", target_id=a)[::-1]
    assert [(r["outcome"], r["metadata"].get("replaced")) for r in sets] == [
        ("ok", False), ("ok", True), ("denied", None)]
    assert sets[0]["actor_kind"] == "member" and sets[0]["actor_id"] == a
    assert sets[0]["metadata"]["backend"] == "claude"
    tests = _by_action(client, "member_key.test", target_id=a)[::-1]
    assert [r["outcome"] for r in tests] == ["ok", "rejected", "unavailable", "noop"]
    assert [r["outcome"] for r in _by_action(client, "member_key.delete", target_id=a)[::-1]] == ["ok", "noop"]
    assert _by_action(client, "member_key.test", target_id=b)[0]["outcome"] == "noop"
    dump = _dump(client)
    for sentinel in (SENTINEL_KEY, "provider body sentinel", "ciphertext", "last4"):
        assert sentinel not in dump


def test_member_key_audit_failure_keeps_old_ciphertext(hh, monkeypatch):
    client, m = hh
    a = create_member(client, ALICE, "Alice")["user_id"]
    url = "/api/v1/me/api-keys/claude"
    assert client.put(url, json={"key": SENTINEL_KEY}, headers=H(ALICE)).status_code == 200
    before = bytes(m.db.member_api_key(a, "claude")["ciphertext"])
    monkeypatch.setattr(Database, "insert_audit", _boom)
    assert client.put(url, json={"key": SENTINEL_KEY[:-1] + "Z"}, headers=H(ALICE)).status_code == 503
    assert bytes(m.db.member_api_key(a, "claude")["ciphertext"]) == before


def test_runner_pairing_lifecycle(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.public_url = "https://tower.example.ts.net"
    cfg.runners["macbook"] = RunnerConfig(name="macbook", token_file=str(tmp_path / "s" / "runner.token"),
                                          min_free_gb=7)
    m = Manager(cfg)
    with TestClient(create_app(m)) as client:
        gone = client.post("/runner-pairing-codes", json={"name": "Secret Mac", "runner": "macbook"}).json()
        assert client.delete(f"/api/admin/v1/runner-pairing-codes/{gone['id']}").status_code == 204
        assert client.delete(f"/runner-pairing-codes/{gone['id']}").status_code == 404
        approved = client.post("/runner-pairing-codes", json={"name": "Secret Mac", "runner": "macbook"}).json()
        assert client.post("/api/v1/runner-pair", json={"code": "hrp-guess"}).status_code == 400
        paired = client.post("/api/v1/runner-pair", json={"code": approved["code"]})
        assert paired.status_code == 201
        assert client.post("/api/v1/runner-pair", json={"code": approved["code"]}).status_code == 400
        rows = m.db.audit_page(500)["items"]
        assert len([r for r in rows if r["action"] == "runner_pairing.create"]) == 2
        assert sorted(r["outcome"] for r in rows if r["action"] == "runner_pairing.revoke") == ["noop", "ok"]
        ok = [r for r in rows if r["action"] == "runner_pairing.redeem" and r["outcome"] == "ok"]
        assert len(ok) == 1 and ok[0]["actor_id"] == ok[0]["key_id"] == paired.json()["owner_key"]["id"]
        assert ok[0]["metadata"]["pairing_id"] == approved["id"]
        reasons = sorted(r["metadata"]["reason"] for r in rows
                         if r["action"] == "runner_pairing.redeem" and r["outcome"] == "denied")
        assert reasons == ["code_used", "unknown_code"]
        dump = str(rows)
        for sentinel in (approved["code"], paired.json()["owner_token"], paired.json()["runner"]["token"],
                         "Secret Mac", "hrp-guess", "tower.example"):
            assert sentinel not in dump


def test_unknown_denials_are_capped(hh, monkeypatch):
    client, _ = hh
    monkeypatch.setattr(credential_audit, "UNKNOWN_DENIAL_HOURLY_CAP", 3)
    client.post("/pairing-codes", json={"name": "n", "origin": ORIGIN, "scopes": ["sessions"]})
    for _ in range(6):
        client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": "hp-nope-nope-nope"})
    assert len(_by_action(client, "pairing.redeem", outcome="denied")) == 3


def test_metadata_allowlist_drops_everything_else():
    cleaned = audit_context.clean_metadata("provider_grant.set", {
        "grant_id": "pc-abc12", "secret_ref": REF, "path": "C:/x", "backend": "claude", "policy": "api_key; drop",
        "fields": ["policy", "/etc/passwd"], "models": ["m"], "replaced": "yes", "reason": "free text"})
    assert cleaned == {"grant_id": "pc-abc12", "backend": "claude", "fields": ["policy"]}
    assert audit_context.clean_metadata("key.create", {"scopes": ["sessions", "bad scope!"], "kind": "x"}) == {
        "scopes": ["sessions"]}
