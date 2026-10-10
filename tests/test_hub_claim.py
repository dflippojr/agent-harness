"""Exclusive Hub claim (#543): the Hub pairs by itself, the owner approves on the daemon host only, and one Hub at a
time administers the daemon with a `hub` key.

Synthetic data only: example origins, made-up names, verifiers and secrets generated here."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from harness import api as harness_api, audit_context, cli, local_owner, migrations
from harness import pairing_requests as pr
from harness.admin import PREFIX
from harness.db import Database
import sdk.harness_client as sdk_module
from sdk.harness_client import Harness

from test_household import household
from test_management_parity import _mounted_owner_routes, _owner_app

HUB_ORIGIN = "https://hub.example"
APP_ORIGIN = "https://shop.example"
CLAIM = PREFIX + "/hub-claim"
MINTS_OWNER_KEYS = {("POST", "/runner-pairing-codes")}   # its code redeems to an owner key


@pytest.fixture
def hh(tmp_path):
    client, m = household(tmp_path)
    with client:
        yield client, m


@pytest.fixture
def clock(monkeypatch):
    now = [1_900_000_000.0]
    monkeypatch.setattr(pr, "clock", lambda: now[0])
    return now


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, pr.challenge_of(verifier)


def _ask(client, origin="", name="Example Hub", **extra):
    verifier, challenge = _pkce()
    headers = {"Origin": origin} if origin else {}
    r = client.post("/api/v1/pair/requests", headers=headers, json={
        "name": name, "kind": "hub", "code_challenge": challenge, **extra})
    return r, verifier


def _redeem(client, rid, verifier, origin=""):
    headers = {"Origin": origin} if origin else {}
    return client.post(f"/api/v1/pair/requests/{rid}/token", headers=headers, json={"code_verifier": verifier})


def _host(m) -> dict:
    """What `harness hub ...` sends on the daemon host: the approval secret (the test client adds the local token)."""
    return {local_owner.HUB_HEADER: m.hub_approval_secret}


def _bearer(token: str, origin: str = "") -> dict:
    return {"Authorization": f"Bearer {token}", **({"Origin": origin} if origin else {})}


def _approve(client, m, rid, match):
    return client.post(f"{CLAIM}/requests/{rid}/approve", headers=_host(m), json={"match": match})


def _audit(client, action: str, **params) -> list[dict]:
    r = client.get(f"{PREFIX}/audit", params={"limit": 500, "action": action, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _keys(m) -> list[dict]:
    return [k for k in m.db.list_api_keys() if k["kind"] == "owner"]


def _claim(client, m, origin=HUB_ORIGIN) -> tuple[str, dict, str]:
    """A whole claim: ask, approve on the host, redeem. Returns (token, key row, request id)."""
    asked, verifier = _ask(client, origin)
    assert asked.status_code == 201, asked.text
    req = asked.json()
    assert _approve(client, m, req["id"], req["match_code"]).status_code == 200
    paired = _redeem(client, req["id"], verifier, origin)
    assert paired.status_code == 201, paired.text
    return paired.json()["token"], paired.json()["app"], req["id"]


def test_full_flow_redeems_one_hub_key_once(hh):
    client, m = hh
    assert client.get("/api/v1").json()["features"]["hub_claimed"] is False
    asked, verifier = _ask(client, HUB_ORIGIN, catalog_app_id="com.example.hub")
    assert asked.status_code == 201, asked.text
    req = asked.json()
    rid, match = req["id"], req["match_code"]
    assert req["state"] == "pending" and re.fullmatch(r"\d{6}", match)

    status = client.get(CLAIM).json()
    assert status["claimed"] is False and status["hub"] is None
    listed = next(r for r in status["requests"] if r["id"] == rid)
    assert listed["kind"] == "hub" and listed["scopes"] == ["admin"] and listed["needs"] == "approve"
    assert listed["elevated"] == ["admin"] and listed["disclosures"][0]["tier"] == "elevated"
    assert "harness hub approve" in listed["disclosures"][0]["text"]

    pending = _redeem(client, rid, verifier, HUB_ORIGIN)
    assert pending.status_code == 409 and pending.json()["error"]["code"] == "pairing_pending"
    wrong = "000000" if match != "000000" else "111111"
    assert _approve(client, m, rid, wrong).status_code == 400                      # wrong match code
    approved = _approve(client, m, rid, match)
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    assert _keys(m) == []                                                           # approval mints nothing

    assert _redeem(client, rid, _pkce()[0], HUB_ORIGIN).status_code == 400          # wrong verifier
    assert _redeem(client, rid, verifier, APP_ORIGIN).status_code == 403            # another origin
    assert _redeem(client, rid, verifier).status_code == 403                        # not from the browser Hub
    assert _keys(m) == []

    paired = _redeem(client, rid, verifier, HUB_ORIGIN)
    assert paired.status_code == 201, paired.text
    token, key = paired.json()["token"], paired.json()["app"]
    assert token.startswith("ho-") and key["role"] == "hub" and key["kind"] == "owner" and key["scopes"] == "admin"
    assert key["origins"] == [HUB_ORIGIN] and key["catalog_app_id"] == "com.example.hub"
    assert _redeem(client, rid, verifier, HUB_ORIGIN).status_code == 400            # exactly once
    assert len(_keys(m)) == 1

    status = client.get(CLAIM).json()
    assert status["claimed"] is True and status["requests"][0]["state"] == "redeemed"
    assert status["hub"] == {"key_id": key["id"], "name": "Example Hub", "kind": "browser", "origin": HUB_ORIGIN,
                             "request_id": rid, "claimed_at": status["hub"]["claimed_at"]}
    assert client.get("/api/v1").json()["features"]["hub_claimed"] is True
    assert client.get(PREFIX).json()["hub"] == {"claimed": True}

    # The Hub administers through the owner API from its origin; the key list shows it as the Hub.
    listed_keys = client.get(f"{PREFIX}/keys", headers=_bearer(token, HUB_ORIGIN))
    assert listed_keys.status_code == 200
    hub_row = next(k for k in listed_keys.json() if k["id"] == key["id"])
    assert hub_row["role"] == "hub" and "hash" not in hub_row and "key" not in hub_row
    assert client.get(f"{PREFIX}/keys", headers=_bearer(token, APP_ORIGIN)).status_code == 403
    redeems = [r["outcome"] for r in _audit(client, "hub.claim.redeem")]
    assert redeems.count("ok") == 1 and set(redeems) == {"ok", "denied"}
    assert [r["outcome"] for r in _audit(client, "hub.claim.request")] == ["ok"]


def test_native_claim_denied_and_expired_requests_mint_nothing(hh, clock):
    client, m = hh
    token, key, _ = _claim(client, m, origin="")
    assert key["origins"] == [] and client.get(CLAIM).json()["hub"]["kind"] == "native"
    assert client.get(f"{PREFIX}/keys", headers=_bearer(token)).status_code == 200
    released = client.post(f"{CLAIM}/release", headers=_host(m), json={"confirm": True})
    assert released.status_code == 200 and released.json()["released"]["key_id"] == key["id"]

    asked, verifier = _ask(client)
    rid = asked.json()["id"]
    denied = client.post(f"{CLAIM}/requests/{rid}/deny", headers=_host(m))
    assert denied.status_code == 200 and denied.json()["state"] == "denied"
    assert _redeem(client, rid, verifier).status_code == 403
    assert _approve(client, m, rid, asked.json()["match_code"]).status_code == 409

    asked, verifier = _ask(client)
    rid = asked.json()["id"]
    clock[0] += pr.APPROVE_TTL_SECONDS + 1
    assert _approve(client, m, rid, asked.json()["match_code"]).status_code == 409
    assert _redeem(client, rid, verifier).status_code == 400

    asked, verifier = _ask(client)   # approved, then not redeemed in time
    rid2 = asked.json()["id"]
    assert _approve(client, m, rid2, asked.json()["match_code"]).status_code == 200
    clock[0] += pr.REDEEM_TTL_SECONDS + 1
    assert _redeem(client, rid2, verifier).status_code == 400

    assert [k["id"] for k in _keys(m) if not k["revoked_at"]] == []
    assert {r["target_id"] for r in _audit(client, "hub.claim.expire")} == {rid, rid2}
    assert [r["outcome"] for r in _audit(client, "hub.claim.deny")] == ["ok"]


def test_scopes_and_unknown_kinds_are_refused(hh):
    client, _ = hh
    asked, _ = _ask(client, scopes=["sessions"])
    assert asked.status_code == 400 and "no scopes" in asked.json()["detail"]
    verifier, challenge = _pkce()
    odd = client.post("/api/v1/pair/requests", json={"name": "x", "kind": "root", "code_challenge": challenge})
    assert odd.status_code == 400
    # An ordinary App still cannot ask for admin.
    app = client.post("/api/v1/pair/requests", json={"name": "x", "scopes": ["admin"], "code_challenge": challenge})
    assert app.status_code == 400


def test_approve_deny_and_release_need_the_host_secret(hh):
    client, m = hh
    asked, verifier = _ask(client)
    rid, match = asked.json()["id"], asked.json()["match_code"]
    owner_token = client.post("/keys", json={"name": "owner tool", "scopes": ["admin"]}).json()["key"]
    app_token = client.post("/keys", json={"name": "an app", "kind": "app", "scopes": ["sessions"]}).json()["key"]
    secret = m.hub_approval_secret
    wrong = {local_owner.HUB_HEADER: secrets.token_urlsafe(32)}
    callers = [
        ("local owner, no secret", {}),
        ("local owner, wrong secret", wrong),
        ("owner token, no secret", _bearer(owner_token)),
        ("owner token, wrong secret", {**_bearer(owner_token), **wrong}),
        ("app key with the secret", {**_bearer(app_token), local_owner.HUB_HEADER: secret}),
        ("tailnet owner, no secret", {"Tailscale-User-Login": "me@example.com"}),
    ]
    for label, headers in callers:
        for path, body in ((f"/requests/{rid}/approve", {"match": match}), (f"/requests/{rid}/deny", None),
                           ("/release", {"confirm": True})):
            r = client.post(CLAIM + path, headers=headers, json=body)
            assert r.status_code == 403, (label, path, r.text)
    # The ordinary owner routes refuse a Hub claim too, whoever calls them.
    for path, body in ((f"/approve", {"match": match}), ("/confirm", {"match": match}), ("/deny", None)):
        r = client.post(f"{PREFIX}/pairing-requests/{rid}{path}", json=body)
        assert r.status_code == 403 and "harness hub approve" in r.json()["detail"], r.text
    assert _redeem(client, rid, verifier).status_code == 409   # still waiting: nothing changed
    denied = [r for a in ("hub.claim.approve", "hub.claim.deny", "hub.release") for r in _audit(client, a)]
    assert len(denied) == 3 * len(callers) and {r["outcome"] for r in denied} == {"denied"}
    assert {r["metadata"]["reason"] for r in denied} == {"host_proof_required"}
    assert len(_audit(client, "pairing_request.approve", outcome="denied")) == 2

    # With the secret it works, and the secret is the one in the owner-only file the CLI reads.
    assert local_owner.read_hub_secret(m.cfg.data_dir) == secret
    assert _approve(client, m, rid, match).status_code == 200


def test_the_secret_is_new_at_every_start_and_owner_only(tmp_path):
    first = local_owner.rotate_hub_secret(tmp_path)
    second = local_owner.rotate_hub_secret(tmp_path)
    assert first != second and local_owner.read_hub_secret(tmp_path) == second
    path = tmp_path / local_owner.HUB_SECRET_FILE
    if sys.platform == "win32":   # mode bits mean nothing there: the ACL names this user alone, nothing inherited
        me = subprocess.run(["whoami"], capture_output=True, text=True, timeout=60).stdout.strip().lower()
        listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=60).stdout
        entries = [line.replace(str(path), "").strip() for line in listing.splitlines()
                   if ":(" in line and not line.startswith("Successfully")]
        assert [e.lower() for e in entries] == [f"{me}:(f)"], listing
    else:
        assert path.stat().st_mode & 0o077 == 0


def test_many_app_requests_never_hide_an_open_hub_claim(hh):
    client, m = hh
    asked, _ = _ask(client)
    for n in range(101):
        m.db.main.insert_pairing_request({
            "id": f"pr-app{n:03d}", "kind": "app", "name": "an app", "scopes": "sessions", "state": pr.ARMED,
            "armed": 1, "created_at": pr.clock() + 1 + n, "expires_at": pr.clock() + 600, "key_id": ""})
    assert [r["id"] for r in client.get(CLAIM).json()["requests"]] == [asked.json()["id"]]


def test_one_hub_at_a_time_until_released(hh):
    client, m = hh
    # Two claims approved before either redeems: only the first to redeem becomes the Hub.
    a, va = _ask(client, HUB_ORIGIN)
    b, vb = _ask(client, HUB_ORIGIN, name="Second Hub")
    assert _approve(client, m, a.json()["id"], a.json()["match_code"]).status_code == 200
    assert _approve(client, m, b.json()["id"], b.json()["match_code"]).status_code == 200
    first = _redeem(client, a.json()["id"], va, HUB_ORIGIN)
    assert first.status_code == 201
    late = _redeem(client, b.json()["id"], vb, HUB_ORIGIN)
    assert late.status_code == 409 and late.json()["error"]["code"] == "hub_claimed"
    assert _redeem(client, b.json()["id"], vb, HUB_ORIGIN).status_code == 403   # that request is finished
    token = first.json()["token"]

    again, _ = _ask(client, HUB_ORIGIN)
    assert again.status_code == 409 and again.json()["error"]["code"] == "hub_claimed"
    assert "harness hub release --confirm" in again.json()["detail"]
    refused = _audit(client, "hub.claim.refused")
    assert len(refused) == 2 and {r["metadata"]["reason"] for r in refused} == {"hub_claimed"}
    assert len(_keys(m)) == 1

    assert client.post(f"{CLAIM}/release", headers=_host(m), json={}).status_code == 400   # needs --confirm
    released = client.post(f"{CLAIM}/release", headers=_host(m), json={"confirm": True})
    assert released.status_code == 200 and released.json()["claimed"] is False
    assert client.get(f"{PREFIX}/keys", headers=_bearer(token, HUB_ORIGIN)).status_code == 401
    assert client.post(f"{CLAIM}/release", headers=_host(m), json={"confirm": True}).status_code == 404
    assert [r["outcome"] for r in _audit(client, "hub.release")] == ["noop", "ok", "denied"]

    new_token, new_key, _ = _claim(client, m)
    assert new_token != token and client.get(CLAIM).json()["hub"]["key_id"] == new_key["id"]


def test_hub_key_cannot_decide_claims_mint_hub_keys_or_be_revoked_by_id(hh):
    client, m = hh
    token, key, _ = _claim(client, m)
    asked, _ = _ask(client)          # refused while claimed, so look at the ones the Hub might try anyway
    assert asked.status_code == 409
    hub = _bearer(token, HUB_ORIGIN)
    with_secret = {**hub, local_owner.HUB_HEADER: m.hub_approval_secret}
    for path, body in (("/requests/pr-x/approve", {"match": "123456"}), ("/requests/pr-x/deny", None),
                       ("/release", {"confirm": True})):
        r = client.post(CLAIM + path, headers=with_secret, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "hub_key", r.text
    hub_denials = [r for a in ("hub.claim.approve", "hub.claim.deny", "hub.release")
                   for r in _audit(client, a, outcome="denied")]
    assert {(r["actor_kind"], r["key_id"], r["metadata"]["reason"]) for r in hub_denials} == {
        ("hub", key["id"], "hub_key")}

    for body in ({"name": "x", "kind": "hub"}, {"name": "x", "scopes": ["admin"], "role": "hub"}):
        assert client.post(f"{PREFIX}/keys", headers=hub, json=body).status_code == 400
        assert client.post(f"{PREFIX}/keys", json=body).status_code == 400   # nobody can
    assert [k["role"] for k in m.db.list_api_keys() if k["role"]] == ["hub"]

    for headers in (hub, {}):
        r = client.delete(f"{PREFIX}/keys/{key['id']}", headers=headers)
        assert r.status_code == 409 and "harness hub release --confirm" in r.json()["detail"], r.text
    assert client.get(CLAIM, headers=hub).json()["claimed"] is True
    assert client.get(f"{PREFIX}/me", headers=hub).status_code == 200

    # Owner API calls made with the Hub key carry actor role `hub`.
    made = client.post(f"{PREFIX}/keys", headers=hub, json={"name": "a device", "scopes": ["inference"]})
    assert made.status_code == 201
    row = next(r for r in _audit(client, "key.create") if r["target_id"] == made.json()["id"])
    assert (row["actor_kind"], row["key_id"]) == ("hub", key["id"])


def test_hub_key_reaches_every_owner_route_the_owner_token_does(tmp_path, monkeypatch):
    """Admin-equivalent: on every mounted owner API route (the parity test's list) but the host-only Hub claim
    routes, the Hub key gets the answer an owner token gets."""
    async def caller_gone(*_args):
        return None, True
    monkeypatch.setattr(harness_api, "_next_bus_event", caller_gone)  # live feeds end after `: connected`
    app, cfg = _owner_app(tmp_path)
    with TestClient(app) as client:
        m = app.state.manager
        _, owner = m.db.main.create_api_key("owner tool", "admin", "owner")
        token, _, _ = _claim(client, m, origin="")
        routes = sorted(_mounted_owner_routes(app, cfg), key=lambda r: (r[1], r[0]))
        checked = 0
        for method, template in routes:
            if template.startswith("/hub-claim"):
                continue
            path = PREFIX + re.sub(r"\{[^}/]+\}", "x", template) if template != "/" else PREFIX
            kwargs = {"params": {"follow": "false"}} if template.endswith("/events") else {}
            if method != "GET":
                kwargs["json"] = {}
            as_owner, as_hub = (client.request(method, path, headers=_bearer(t), **kwargs).status_code
                                for t in (owner, token))
            if (method, template) in MINTS_OWNER_KEYS:   # refused to the Hub up front, whatever the body
                assert as_hub == 403, (method, template, as_owner, as_hub)
                continue
            assert as_hub == as_owner and as_hub not in (401, 403), (method, template, as_owner, as_hub)
            checked += 1
        assert checked > 100


def test_no_secret_reaches_a_response_cli_output_audit_row_or_log(hh, monkeypatch, capsys, tmp_path, caplog):
    client, m = hh
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []

    def api(method, path, **kwargs):
        r = client.request(method, PREFIX + path, **kwargs)
        texts.append(r.text)
        assert r.status_code < 400, r.text
        return r.json()
    home = tmp_path / ".agent-harness"
    monkeypatch.setattr(cli, "HARNESS_HOME", home)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", home / "client" / "config.json")
    monkeypatch.delenv("HARNESS_URL", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.setattr(cli, "api", api)
    monkeypatch.setattr("harness.config.resolve_data_dir", lambda: m.cfg.data_dir)

    asked, verifier = _ask(client, HUB_ORIGIN)
    texts.append(asked.text)
    rid, match = asked.json()["id"], asked.json()["match_code"]
    out = []
    for argv in (["hub", "claim-status"], ["hub", "approve", rid, "--match", match]):
        monkeypatch.setattr("sys.argv", ["harness", *argv])
        assert cli.main() == 0
        out.append(capsys.readouterr().out)
    assert rid in out[0] and '"approved"' in out[1]
    paired = _redeem(client, rid, verifier, HUB_ORIGIN)
    token = paired.json()["token"]
    for argv in (["hub", "claim-status"], ["keys", "list"], ["hub", "release", "--confirm"]):
        monkeypatch.setattr("sys.argv", ["harness", *argv])
        assert cli.main() == 0
        out.append(capsys.readouterr().out)
    assert '"claimed": true' in out[2] and '"hub"' in out[3] and '"released"' in out[4]
    texts.append(client.get(f"{PREFIX}/audit", params={"limit": 500}).text)
    texts.append(client.get(f"{PREFIX}/pairing-requests").text)
    texts.append(client.get("/api/v1").text)
    rows = m.db.main.conn.execute("SELECT * FROM account_audit").fetchall()
    texts.extend(json.dumps(dict(r)) for r in rows)
    texts.extend(out)
    texts.append(caplog.text)
    forbidden = (token, hashlib.sha256(token.encode()).hexdigest(), verifier, m.hub_approval_secret)
    for text in texts:
        for value in forbidden:
            assert value not in text


def test_cli_sends_the_secret_only_to_a_daemon_on_this_machine(tmp_path, monkeypatch):
    local_owner.rotate_hub_secret(tmp_path)
    monkeypatch.setattr("harness.config.resolve_data_dir", lambda: tmp_path)
    seen = []
    monkeypatch.setattr(cli, "api", lambda method, path, **kwargs: seen.append((method, path, kwargs)) or {})
    parser = cli._build_parser()
    monkeypatch.setattr(cli, "BASE", "http://127.0.0.1:8100")
    assert cli._cmd_admin(parser.parse_args(["hub", "release", "--confirm"])) == 0
    assert seen[-1] == ("POST", "/hub-claim/release", {
        "json": {"confirm": True}, "headers": {cli.HUB_APPROVAL_HEADER: local_owner.read_hub_secret(tmp_path)}})
    assert cli.HUB_APPROVAL_HEADER == local_owner.HUB_HEADER
    assert cli._cmd_admin(parser.parse_args(["hub", "claim-status"])) == 0
    assert "headers" not in seen[-1][2]                        # status needs no host proof
    monkeypatch.setattr(cli, "BASE", "https://harness.example")
    with pytest.raises(SystemExit):
        cli._cmd_admin(parser.parse_args(["hub", "approve", "pr-x", "--match", "123456"]))
    monkeypatch.setattr(cli, "BASE", "http://127.0.0.1:8100")
    monkeypatch.setattr("harness.config.resolve_data_dir", lambda: tmp_path / "elsewhere")
    with pytest.raises(SystemExit):
        cli._cmd_admin(parser.parse_args(["hub", "deny", "pr-x"]))
    assert len(seen) == 2


def test_sdk_requests_a_hub_claim_and_checks_the_feature(hh, monkeypatch):
    client, m = hh

    class Bridge:
        def __init__(self, base_url, timeout, headers):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def get(self, path):
            return client.get(path, headers=self.headers)

        def post(self, path, json):
            return client.post(path, json=json, headers=self.headers)

    real = httpx.Client
    monkeypatch.setattr(sdk_module.httpx, "Client", Bridge)
    pending = Harness.request_hub_claim("http://testserver", "SDK Hub", origin=HUB_ORIGIN)
    assert pending.code_verifier not in repr(pending) and re.fullmatch(r"\d{6}", pending.match_code)
    assert _approve(client, m, pending.id, pending.match_code).status_code == 200
    monkeypatch.setattr(sdk_module.httpx, "Client", lambda base_url, timeout, headers: real(
        base_url=base_url, timeout=timeout, headers=headers, transport=httpx.MockTransport(lambda r: httpx.Response(
            200))) if "Authorization" in headers else Bridge(base_url, timeout, headers))
    hub = Harness.redeem_pairing(pending)
    try:
        assert hub.token.startswith("ho-") and hub.paired_app["role"] == "hub"
    finally:
        hub.close()
    monkeypatch.setattr(sdk_module.httpx, "Client", Bridge)
    with pytest.raises(sdk_module.HarnessError) as taken:
        Harness.request_hub_claim("http://testserver", "Second Hub")
    assert taken.value.code == "hub_claimed"

    class Locked(Bridge):
        """A daemon reached on its own machine without the local owner token: its root answers 401."""
        def get(self, path):
            return httpx.Response(401, json={"detail": "requests from this machine need the local owner token"})
    monkeypatch.setattr(sdk_module.httpx, "Client", Locked)
    with pytest.raises(sdk_module.HarnessError) as locked:   # the probe steps aside; the request itself answers
        Harness.request_hub_claim("http://testserver", "Hub")
    assert locked.value.code == "hub_claimed"

    class OldServer(Bridge):
        def get(self, path):
            return httpx.Response(200, json={"features": {"pairing_requests": True}})
    monkeypatch.setattr(sdk_module.httpx, "Client", OldServer)
    with pytest.raises(sdk_module.HarnessError) as old:
        Harness.request_hub_claim("http://testserver", "Hub")
    assert old.value.code == "feature_unsupported"


def test_migration_adds_the_role_and_the_record(tmp_path):
    shipped = [s for s in migrations.discover() if s[0] <= 57]
    assert shipped[-1][0] == 57
    path = tmp_path / "harness.db"
    old = Database(path, migrations=shipped[:-1])
    old.conn.execute("INSERT INTO api_keys (id, name, prefix, hash, created_at, scopes, kind, origins) "
                     "VALUES ('k-old', 'legacy', 'ho-xxxxxxx', 'h', 1, 'admin', 'owner', '[]')")
    old.conn.commit()
    assert not old.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'hub_claim'").fetchone()
    old.close()
    db = Database(path, migrations=shipped)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 57
    assert next(k for k in db.list_api_keys() if k["id"] == "k-old")["role"] == ""
    columns = {r["name"] for r in db.conn.execute("PRAGMA table_info(hub_claim)")}
    assert columns == {"slot", "key_id", "name", "kind", "origin", "request_id", "claimed_at"}
    assert db.hub_claim() is None
    db.conn.execute("INSERT INTO hub_claim (slot, key_id, name, kind, claimed_at) VALUES (1, 'k', 'n', 'native', 1)")
    with pytest.raises(Exception):   # never a second row
        db.conn.execute("INSERT INTO hub_claim (slot, key_id, name, kind, claimed_at) VALUES (2, 'k', 'n', 'native', 1)")
    assert list((tmp_path / "pre-migration").glob("harness-v56-*.sqlite3"))
    db.close()


def test_hub_key_cannot_mint_an_owner_key_and_release_names_any_it_did(hh):
    """A key the Hub minted would outlive `harness hub release`, so it can mint no owner key at all."""
    client, m = hh
    token, key, _ = _claim(client, m)
    hub = _bearer(token, HUB_ORIGIN)
    before = {k["id"] for k in _keys(m)}
    for body in ({"name": "spare", "kind": "owner", "scopes": ["admin"]}, {"name": "spare", "scopes": ["admin"]},
                 {"name": "spare", "kind": "owner"}):
        for path, headers in ((f"{PREFIX}/keys", hub), ("/keys", _bearer(token))):
            r = client.post(path, headers=headers, json=body)
            assert r.status_code == 403 and "cannot mint owner keys" in r.text, (path, body, r.text)
    assert {k["id"] for k in _keys(m)} == before
    denied = _audit(client, "key.create", outcome="denied")
    assert len(denied) == 6 and {(r["actor_kind"], r["key_id"], r["metadata"]["reason"]) for r in denied} == {
        ("hub", key["id"], "hub_key")}
    # The owner still can, and device and App keys stay the Hub's to make.
    assert client.post(f"{PREFIX}/keys", json={"name": "owner tool", "scopes": ["admin"]}).status_code == 201
    assert client.post(f"{PREFIX}/keys", headers=hub, json={"name": "dev", "scopes": ["inference"]}).status_code == 201

    # An owner key a Hub minted before this rule existed is named by release, for the owner to revoke.
    row, _ = m.db.main.create_api_key("legacy spare", "admin", "owner")
    m.db.main.write(lambda: m.db.main.insert_audit(
        "owner", row["id"], "key.create", "ok", context=audit_context.owner_context(key), target_kind="api_key"))
    released = client.post(f"{CLAIM}/release", headers=_host(m), json={"confirm": True})
    assert released.status_code == 200, released.text
    assert [k["id"] for k in released.json()["hub_minted_owner_keys"]] == [row["id"]]
    assert "harness keys revoke" in released.json()["next"]
    assert client.get(f"{PREFIX}/keys", headers=hub).status_code == 401


def test_release_without_hub_minted_keys_lists_none(hh):
    client, m = hh
    _claim(client, m)
    released = client.post(f"{CLAIM}/release", headers=_host(m), json={"confirm": True}).json()
    assert released["hub_minted_owner_keys"] == [] and "next" not in released


def test_hub_key_cannot_make_a_runner_pairing_code(tmp_path):
    """A runner pairing code redeems to an owner key, so the Hub is refused one like an admin `POST /keys`."""
    from harness.api import create_app
    from test_module_runners import runner_manager
    m = runner_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        token, key, _ = _claim(client, m, origin="")
        r = client.post(f"{PREFIX}/runner-pairing-codes", headers=_bearer(token), json={"runner": "mac"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "hub_key", r.text
        assert m.db.list_runner_pairing_codes() == []
        denied = _audit(client, "runner_pairing.create", outcome="denied")
        assert [(r["actor_kind"], r["key_id"], r["metadata"]["reason"]) for r in denied] == [
            ("hub", key["id"], "hub_key")]
        assert client.post(f"{PREFIX}/runner-pairing-codes", json={"runner": "mac"}).status_code == 201


def test_a_bad_body_without_the_secret_is_403_and_audited(hh):
    """The host proof runs before the body is read: a prober without the secret leaves a trace whatever it sends."""
    client, m = hh
    asked, _ = _ask(client)
    rid = asked.json()["id"]
    bad = ({}, {"match": 123}, [], None, "not json")
    for body in bad:
        for path in (f"/requests/{rid}/approve", "/release"):
            kwargs = {"content": body} if isinstance(body, str) else {"json": body}
            r = client.post(CLAIM + path, **kwargs)
            assert r.status_code == 403 and r.json()["error"]["code"] == "host_proof_required", (path, body, r.text)
    for action in ("hub.claim.approve", "hub.release"):
        rows = _audit(client, action, outcome="denied")
        assert len(rows) == len(bad) and {r["metadata"]["reason"] for r in rows} == {"host_proof_required"}

    # With the secret a bad body is a 400, audited as invalid_request, and nothing changes.
    for path, action in ((f"/requests/{rid}/approve", "hub.claim.approve"), ("/release", "hub.release")):
        r = client.post(CLAIM + path, headers=_host(m), content="not json")
        assert r.status_code == 400, r.text
        assert _audit(client, action, outcome="denied")[0]["metadata"]["reason"] == "invalid_request"
    assert client.post(f"{CLAIM}/requests/{rid}/approve", headers=_host(m), json={}).status_code == 400
    assert client.get(CLAIM).json()["requests"][0]["state"] == "pending"


def _listed(response) -> list[dict]:
    body = response.json()
    return body if isinstance(body, list) else body.get("items", body.get("requests", []))


def test_status_never_shows_a_hub_match_code_but_shows_where_it_comes_from(hh):
    client, m = hh
    browser, _ = _ask(client, HUB_ORIGIN, name="Agent Harness Hub")
    native, _ = _ask(client, name="Agent Harness Hub")
    assert browser.json()["match_code"] and native.json()["match_code"]    # the Hub itself still sees its code
    for listing in (_listed(client.get(CLAIM)), _listed(client.get(f"{PREFIX}/pairing-requests"))):
        hubs = {r["id"]: r for r in listing if r["kind"] == "hub"}
        assert len(hubs) == 2 and {r["match_code"] for r in hubs.values()} == {""}
        assert (hubs[browser.json()["id"]]["origin"], hubs[browser.json()["id"]]["browser"]) == (HUB_ORIGIN, True)
        assert (hubs[native.json()["id"]]["origin"], hubs[native.json()["id"]]["browser"]) == ("", False)


def test_cli_status_shows_origins_and_approve_warns_on_several_claims(hh, monkeypatch, capsys):
    client, m = hh

    def api(method, path, **kwargs):
        r = client.request(method, PREFIX + path, **kwargs)
        assert r.status_code < 400, r.text
        return r.json()
    monkeypatch.setattr(cli, "api", api)
    monkeypatch.setattr(cli, "BASE", "http://127.0.0.1:8100")
    monkeypatch.setattr("harness.config.resolve_data_dir", lambda: m.cfg.data_dir)
    parser = cli._build_parser()
    real, _ = _ask(client, HUB_ORIGIN, name="Agent Harness Hub")
    rid, match = real.json()["id"], real.json()["match_code"]

    assert cli._cmd_admin(parser.parse_args(["hub", "claim-status"])) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["requests"][0]["match_code"] == "" and match not in out + err
    assert f"browser Hub at {HUB_ORIGIN}" in err and "not shown here" in err
    assert cli._cmd_admin(parser.parse_args(["hub", "approve", rid, "--match", match])) == 0
    assert "pending" not in capsys.readouterr().err                     # one claim: no warning

    client.post(f"{CLAIM}/requests/{rid}/deny", headers=_host(m))
    real, _ = _ask(client, HUB_ORIGIN, name="Agent Harness Hub")
    look_alike, _ = _ask(client, name="Agent Harness Hub")
    rid, match = real.json()["id"], real.json()["match_code"]
    cli._cmd_admin(parser.parse_args(["hub", "claim-status"]))
    assert "native Hub (no browser origin)" in capsys.readouterr().err
    assert cli._cmd_admin(parser.parse_args(["hub", "approve", rid, "--match", match])) == 0
    err = capsys.readouterr().err
    assert "2 Hub claims are pending" in err and f"* {rid}" in err and look_alike.json()["id"] in err
