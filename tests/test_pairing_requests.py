"""Zero-touch App pairing (#519): the daemon hands an App its token directly; the owner never sees one.

Synthetic data only: example origins, made-up catalog ids and verifiers generated here."""
from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path

import httpx
import pytest

from harness import cli, migrations
from harness import pairing_requests as pr
from harness.admin import PREFIX
from harness.db import Database
import sdk.harness_client as sdk_module
from sdk.harness_client import Harness

from test_household import H, OWNER, household

ORIGIN = "https://shop.example"
OTHER = "https://other.example"
CATALOG_ID = "com.example.shopping-list"
BASE = PREFIX + "/pairing-requests"
WEB = Path(__file__).resolve().parents[1] / "harness" / "web"
SECRET_PREFIXES = ("ha-", "ho-", "hp-")


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


class Recorder:
    """Every owner-side response body in a flow, to prove none of them carries a secret."""

    def __init__(self, client):
        self.client, self.texts = client, []

    def owner(self, method: str, path: str, **kwargs):
        r = self.client.request(method, path, headers=H(OWNER), **kwargs)
        self.texts.append(r.text)
        return r


def _ask(client, origin="", scopes=("sessions",), name="Shopping list", **extra):
    verifier, challenge = _pkce()
    headers = {"Origin": origin} if origin else {}
    r = client.post("/api/v1/pair/requests", headers=headers, json={
        "name": name, "scopes": list(scopes), "code_challenge": challenge, **extra})
    return r, verifier


def _redeem(client, rid, verifier, origin=""):
    headers = {"Origin": origin} if origin else {}
    return client.post(f"/api/v1/pair/requests/{rid}/token", headers=headers, json={"code_verifier": verifier})


def _audit(client, action: str, **params) -> list[dict]:
    r = client.get(f"{PREFIX}/audit", params={"limit": 500, "action": action, **params}, headers=H(OWNER))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _app_keys(m) -> list[dict]:
    return [k for k in m.db.list_api_keys() if k["kind"] == "app"]


def _assert_no_secrets(texts, *secrets_seen):
    for text in texts:
        for prefix in SECRET_PREFIXES:
            assert prefix not in text, text
        for value in secrets_seen:
            assert value not in text


def test_browser_request_approve_and_redeem_once(hh):
    client, m = hh
    rec = Recorder(client)
    asked, verifier = _ask(client, ORIGIN, catalog_app_id=CATALOG_ID)
    assert asked.status_code == 201, asked.text
    assert asked.headers["cache-control"] == "no-store"
    req = asked.json()
    assert req["state"] == "pending" and req["browser"] is True and re.fullmatch(r"\d{6}", req["match_code"])
    assert set(req) == {"id", "state", "match_code", "expires_at", "browser"}
    rid = req["id"]

    listed = next(r for r in rec.owner("GET", BASE).json() if r["id"] == rid)
    assert listed["origin"] == ORIGIN and listed["match_code"] == req["match_code"] and listed["needs"] == "approve"
    assert listed["scopes"] == ["sessions"] and listed["catalog_app_id"] == CATALOG_ID and listed["elevated"] == []
    assert listed["disclosures"] == [{"scope": "sessions", "tier": "standard", "text": pr.DISCLOSURES["sessions"]}]

    # Before approval the verifier's holder hears "pending"; nothing is minted.
    pending = _redeem(client, rid, verifier, ORIGIN)
    assert pending.status_code == 409 and pending.json()["error"]["code"] == "pairing_pending"
    assert rec.owner("POST", f"{BASE}/{rid}/approve", json={}).status_code == 422
    assert rec.owner("POST", f"{BASE}/{rid}/approve", json={"match": "000000" if req["match_code"] != "000000"
                                                            else "111111"}).status_code == 400
    approved = rec.owner("POST", f"{BASE}/{rid}/approve", json={"match": req["match_code"]})
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    assert approved.json()["needs"] == "redeem" and approved.json()["match_code"] == ""
    assert _app_keys(m) == []  # approval is a state change only

    assert _redeem(client, rid, _pkce()[0], ORIGIN).status_code == 400          # wrong verifier
    assert _redeem(client, rid, "short", ORIGIN).status_code == 400              # not a verifier at all
    assert _redeem(client, rid, verifier, OTHER).status_code == 403              # wrong origin (CORS refuses it)
    assert _redeem(client, rid, verifier).status_code == 403                     # no origin: not the browser App
    assert _app_keys(m) == []

    paired = _redeem(client, rid, verifier, ORIGIN)
    assert paired.status_code == 201, paired.text
    assert paired.headers["cache-control"] == "no-store"
    token, app = paired.json()["token"], paired.json()["app"]
    assert token.startswith("ha-") and app["origins"] == [ORIGIN] and app["catalog_app_id"] == CATALOG_ID
    assert app["scopes"] == "sessions" and app["kind"] == "app" and "key" not in app and "hash" not in app
    assert client.get("/api/v1/sessions", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN}
                      ).status_code == 200
    assert _redeem(client, rid, verifier, ORIGIN).status_code != 201             # exactly once
    assert len(_app_keys(m)) == 1

    done = next(r for r in rec.owner("GET", BASE).json() if r["id"] == rid)
    assert done["state"] == "redeemed" and done["key_id"] == app["id"]
    audit = rec.owner("GET", f"{PREFIX}/audit?limit=500")
    rows = [r for r in audit.json()["items"] if r["action"].startswith("pairing_request.")]
    assert {r["action"] for r in rows} >= {"pairing_request.create", "pairing_request.approve",
                                           "pairing_request.redeem"}
    redeem = next(r for r in rows if r["action"] == "pairing_request.redeem" and r["outcome"] == "ok")
    assert redeem["actor_kind"] == "app" and redeem["key_id"] == app["id"] and redeem["source"] == "app_api"
    assert redeem["metadata"] == {"key_id": app["id"], "request_id": rid, "kind": "app", "scopes": ["sessions"],
                                  "catalog_app_id": CATALOG_ID, "browser": True}
    reasons = {r["metadata"].get("reason") for r in rows if r["outcome"] == "denied"}
    assert {"match_mismatch", "invalid_verifier", "origin_mismatch"} <= reasons
    _assert_no_secrets(rec.texts, token, verifier)
    stored = m.db.main.get_pairing_request(rid)
    assert verifier not in json.dumps(stored) and pr.challenge_of(verifier) not in json.dumps(stored)


def test_native_request_needs_the_match_code_and_never_redeems_from_a_browser(hh):
    client, _ = hh
    asked, verifier = _ask(client)
    req = asked.json()
    assert asked.status_code == 201 and req["browser"] is False
    rid = req["id"]
    wrong = "123456" if req["match_code"] != "123456" else "654321"
    assert client.post(f"{BASE}/{rid}/approve", json={"match": wrong}, headers=H(OWNER)).status_code == 400
    assert client.post(f"{BASE}/{rid}/approve", json={"match": ""}, headers=H(OWNER)).status_code == 400
    assert _redeem(client, rid, verifier).status_code == 409
    spaced = f"{req['match_code'][:3]} {req['match_code'][3:]}"  # the owner may type it as the App groups it
    assert client.post(f"{BASE}/{rid}/approve", json={"match": spaced}, headers=H(OWNER)).status_code == 200
    assert _redeem(client, rid, verifier, ORIGIN).status_code == 403  # a web page can't take a native App's token
    paired = _redeem(client, rid, verifier)
    assert paired.status_code == 201 and paired.json()["app"]["origins"] == []
    assert _redeem(client, rid, verifier).status_code == 400


def test_denied_request_mints_nothing(hh):
    client, m = hh
    asked, verifier = _ask(client, ORIGIN)
    rid = asked.json()["id"]
    denied = client.post(f"{BASE}/{rid}/deny", headers=H(OWNER))
    assert denied.status_code == 200 and denied.json()["state"] == "denied"
    assert client.post(f"{BASE}/{rid}/deny", headers=H(OWNER)).status_code == 409
    assert client.post(f"{BASE}/{rid}/approve", json={"match": asked.json()["match_code"]},
                       headers=H(OWNER)).status_code == 409
    assert _redeem(client, rid, verifier, ORIGIN).status_code == 403
    assert client.post(f"{BASE}/pr-nope/deny", headers=H(OWNER)).status_code == 404
    assert _redeem(client, "pr-nope", verifier).status_code == 404
    assert _app_keys(m) == []
    assert _audit(client, "pairing_request.deny", outcome="ok")[0]["metadata"] == {"request_id": rid}


def test_unapproved_and_unredeemed_requests_expire(hh, clock):
    client, m = hh
    late, _ = _ask(client)
    clock[0] += pr.APPROVE_TTL_SECONDS + 1
    rid = late.json()["id"]
    assert next(r for r in client.get(BASE, headers=H(OWNER)).json() if r["id"] == rid)["state"] == "expired"
    assert client.post(f"{BASE}/{rid}/approve", json={"match": late.json()["match_code"]},
                       headers=H(OWNER)).status_code == 409

    asked, verifier = _ask(client)
    rid2 = asked.json()["id"]
    clock[0] += pr.APPROVE_TTL_SECONDS - 5  # approved just in time
    assert client.post(f"{BASE}/{rid2}/approve", json={"match": asked.json()["match_code"]},
                       headers=H(OWNER)).status_code == 200
    clock[0] += pr.REDEEM_TTL_SECONDS + 1
    gone = _redeem(client, rid2, verifier)
    assert gone.status_code == 400 and "expired" in gone.json()["detail"]
    assert _app_keys(m) == []
    expired = _audit(client, "pairing_request.expire")
    assert {r["target_id"] for r in expired} == {rid, rid2}
    assert all(r["actor_kind"] == "system" and r["outcome"] == "ok" for r in expired)


def test_elevated_scopes_need_a_separate_acknowledgement_and_admin_is_refused(hh):
    client, _ = hh
    for scope in ("sessions:all", "approvals", "remote_control"):
        asked, _ = _ask(client, scopes=("sessions", scope))
        assert asked.status_code == 201, (scope, asked.text)
        rid, match = asked.json()["id"], asked.json()["match_code"]
        row = next(r for r in client.get(BASE, headers=H(OWNER)).json() if r["id"] == rid)
        assert row["elevated"] == [scope]
        assert next(d for d in row["disclosures"] if d["scope"] == scope)["tier"] == "elevated"
        no_ack = client.post(f"{BASE}/{rid}/approve", json={"match": match}, headers=H(OWNER))
        assert no_ack.status_code == 400 and "acknowledge" in no_ack.json()["detail"]
        ok = client.post(f"{BASE}/{rid}/approve", json={"match": match, "acknowledge_elevated": True},
                         headers=H(OWNER))
        assert ok.status_code == 200
        client.post(f"{BASE}/{rid}/deny", headers=H(OWNER))  # keep under the pending cap
    assert _audit(client, "pairing_request.approve", outcome="ok")[0]["metadata"]["acknowledged"] is True
    refused, _ = _ask(client, scopes=("admin",))
    assert refused.status_code == 400 and "owner scope" in refused.json()["detail"]
    assert _ask(client, scopes=("bogus",))[0].status_code == 400
    assert _ask(client, scopes=())[0].status_code == 400
    assert _ask(client, code_challenge_method="plain")[0].status_code == 400
    bad = client.post("/api/v1/pair/requests", json={"name": "x", "code_challenge": "too-short"})
    assert bad.status_code == 400
    assert client.post(BASE, json={"catalog_app_id": CATALOG_ID, "scopes": ["approvals"]},
                       headers=H(OWNER)).status_code == 400
    assert client.post(BASE, json={"catalog_app_id": CATALOG_ID, "scopes": ["admin"]},
                       headers=H(OWNER)).status_code == 400
    assert _audit(client, "pairing_request.create", outcome="denied")


def test_hub_armed_browser_slot(hh):
    client, m = hh
    rec = Recorder(client)
    armed = rec.owner("POST", BASE, json={"catalog_app_id": CATALOG_ID, "scopes": ["sessions"], "origin": ORIGIN})
    assert armed.status_code == 201, armed.text
    slot = armed.json()
    assert slot["state"] == "armed" and slot["needs"] == "claim" and slot["name"] == CATALOG_ID
    assert slot["armed"] is True and slot["match_code"] == ""
    rid = slot["id"]
    verifier, challenge = _pkce()
    assert client.post(f"/api/v1/pair/requests/{rid}/token", headers={"Origin": ORIGIN},
                       json={"code_verifier": verifier}).status_code == 400  # nothing claimed it yet
    assert client.post(f"/api/v1/pair/requests/{rid}/claim", json={"code_challenge": challenge}).status_code == 403
    claimed = client.post(f"/api/v1/pair/requests/{rid}/claim", headers={"Origin": ORIGIN},
                          json={"code_challenge": challenge})
    assert claimed.status_code == 200 and claimed.json()["state"] == "approved"  # exact origin is the check
    assert client.post(f"/api/v1/pair/requests/{rid}/claim", headers={"Origin": ORIGIN},
                       json={"code_challenge": _pkce()[1]}).status_code == 409   # claimed once
    paired = _redeem(client, rid, verifier, ORIGIN)
    assert paired.status_code == 201 and paired.json()["app"]["catalog_app_id"] == CATALOG_ID
    assert _redeem(client, rid, verifier, ORIGIN).status_code != 201
    assert len(_app_keys(m)) == 1
    rec.owner("GET", BASE)
    _assert_no_secrets(rec.texts, paired.json()["token"], verifier)
    create = _audit(client, "pairing_request.create", outcome="ok")[0]
    assert create["actor_kind"] == "owner" and create["metadata"]["armed"] is True


def test_hub_armed_native_slot_waits_for_the_owner_to_confirm(hh):
    client, _ = hh
    rec = Recorder(client)
    rid = rec.owner("POST", BASE, json={"catalog_app_id": CATALOG_ID, "scopes": ["sessions", "approvals"],
                                        "acknowledge_elevated": True}).json()["id"]
    verifier, challenge = _pkce()
    assert client.post(f"/api/v1/pair/requests/{rid}/claim", headers={"Origin": ORIGIN},
                       json={"code_challenge": challenge}).status_code == 403  # a web page can't claim a native slot
    claimed = client.post(f"/api/v1/pair/requests/{rid}/claim", json={"code_challenge": challenge})
    assert claimed.status_code == 200 and claimed.json()["state"] == "claimed"
    match = claimed.json()["match_code"]
    assert re.fullmatch(r"\d{6}", match)
    listed = next(r for r in rec.owner("GET", BASE).json() if r["id"] == rid)
    assert listed["needs"] == "confirm" and listed["match_code"] == match
    assert _redeem(client, rid, verifier).status_code == 409  # no token before the confirm
    assert rec.owner("POST", f"{BASE}/{rid}/approve", json={"match": match}).status_code == 409
    wrong = "123456" if match != "123456" else "654321"
    assert rec.owner("POST", f"{BASE}/{rid}/confirm", json={"match": wrong}).status_code == 400
    assert _redeem(client, rid, verifier).status_code == 409
    confirmed = rec.owner("POST", f"{BASE}/{rid}/confirm", json={"match": match})
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "approved"
    paired = _redeem(client, rid, verifier)
    assert paired.status_code == 201 and paired.json()["app"]["scopes"] == "sessions approvals"
    assert _redeem(client, rid, verifier).status_code == 400
    _assert_no_secrets(rec.texts, paired.json()["token"], verifier)
    assert _audit(client, "pairing_request.approve", outcome="ok")[0]["metadata"]["confirmed"] is True


def test_concurrent_redeems_mint_one_key(hh):
    client, m = hh
    asked, verifier = _ask(client)
    rid = asked.json()["id"]
    client.post(f"{BASE}/{rid}/approve", json={"match": asked.json()["match_code"]}, headers=H(OWNER))
    results = []
    threads = [threading.Thread(target=lambda: results.append(_redeem(client, rid, verifier).status_code))
               for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201, 400, 400, 400]
    assert len(_app_keys(m)) == 1


def test_cap_and_rate_limit_per_source(hh, clock):
    client, _ = hh
    for _ in range(pr.SOURCE_PENDING_CAP):
        assert _ask(client, ORIGIN)[0].status_code == 201
    capped = _ask(client, ORIGIN)[0]
    assert capped.status_code == 429 and capped.json()["error"]["code"] == "too_many_pending"
    assert _ask(client, OTHER)[0].status_code == 201  # another source is unaffected
    for row in client.get(BASE, headers=H(OWNER)).json():
        client.post(f"{BASE}/{row['id']}/deny", headers=H(OWNER))
    for _ in range(pr.SOURCE_RATE[0] - pr.SOURCE_PENDING_CAP):
        rid = _ask(client, ORIGIN)[0].json()["id"]
        client.post(f"{BASE}/{rid}/deny", headers=H(OWNER))
    limited = _ask(client, ORIGIN)[0]
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    clock[0] += pr.SOURCE_RATE[1] + 1
    assert _ask(client, ORIGIN)[0].status_code == 201
    reasons = {r["metadata"]["reason"] for r in _audit(client, "pairing_request.create", outcome="denied")}
    assert reasons == {"too_many_pending", "rate_limited"}


def test_an_app_on_this_machine_pairs_without_the_local_owner_token(hh):
    client, _ = hh
    client.local_owner = False
    try:
        assert client.get("/api/v1/sessions").status_code == 401  # it is not the owner
        assert client.post("/api/v1/pair/requests/pr-x/other").status_code == 401  # only the three pairing routes
        assert client.post("/api/v1/pair/requests/pr-x/token/more").status_code == 401
        asked, verifier = _ask(client)
        assert asked.status_code == 201
        rid = asked.json()["id"]
        client.post(f"{BASE}/{rid}/approve", json={"match": asked.json()["match_code"]}, headers=H(OWNER))
        assert _redeem(client, rid, verifier).status_code == 201
    finally:
        client.local_owner = True


def test_existing_pairing_codes_still_work(hh):
    client, _ = hh
    code = client.post("/pairing-codes", json={"name": "old", "origin": ORIGIN, "scopes": ["sessions"]}).json()
    paired = client.post("/api/v1/pair", headers={"Origin": ORIGIN}, json={"code": code["code"]})
    assert paired.status_code == 201 and paired.json()["token"].startswith("ha-")


def test_cli_rows_call_the_owner_routes_and_print_no_secret(hh, monkeypatch, capsys, tmp_path):
    client, _ = hh
    asked, verifier = _ask(client, ORIGIN)
    rid, match = asked.json()["id"], asked.json()["match_code"]

    def api(method, path, **kwargs):
        r = client.request(method, PREFIX + path, headers=H(OWNER), **kwargs)
        assert r.status_code < 400, r.text
        return r.json()
    home = tmp_path / ".agent-harness"
    monkeypatch.setattr(cli, "HARNESS_HOME", home)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", home / "client" / "config.json")
    monkeypatch.delenv("HARNESS_URL", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.setattr(cli, "api", api)
    out = []
    for argv in (["pairing-requests", "list"], ["pairing-requests", "approve", rid, "--match", match],
                 ["pairing-requests", "arm", CATALOG_ID, "--scopes", "sessions", "--origin", ORIGIN]):
        monkeypatch.setattr("sys.argv", ["harness", *argv])
        assert cli.main() == 0
        out.append(capsys.readouterr().out)
    assert rid in out[0] and match in out[0] and '"approved"' in out[1] and '"armed"' in out[2]
    assert _redeem(client, rid, verifier, ORIGIN).status_code == 201
    slot = json.loads(out[2])["id"]
    monkeypatch.setattr("sys.argv", ["harness", "pairing-requests", "deny", slot])
    assert cli.main() == 0
    out.append(capsys.readouterr().out)
    _assert_no_secrets(out, verifier)


def test_web_never_calls_the_redeem_route_or_reads_a_token_from_it():
    for path in [*WEB.rglob("*.mjs"), *WEB.rglob("*.js"), *WEB.rglob("*.html")]:
        text = path.read_text(encoding="utf-8")
        assert "pair/requests" not in text, path
        assert not re.search(r"pairing-requests[^\"'`]*/token", text), path


def test_sdk_requests_and_redeems_against_the_daemon(hh, monkeypatch):
    client, _ = hh

    class Bridge:
        """httpx.Client for the SDK's one-shot pairing calls, served by the test daemon."""
        def __init__(self, base_url, timeout, headers):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def post(self, path, json):
            return client.post(path, json=json, headers=self.headers)

    real = httpx.Client
    monkeypatch.setattr(sdk_module.httpx, "Client", Bridge)
    pending = Harness.request_pairing("http://testserver", "SDK app", scopes=["sessions"], origin=ORIGIN,
                                      catalog_app_id=CATALOG_ID)
    assert pending.code_verifier not in repr(pending) and re.fullmatch(r"\d{6}", pending.match_code)
    with pytest.raises(sdk_module.HarnessError) as waiting:
        Harness.redeem_pairing(pending, wait=0)
    assert waiting.value.code == "pairing_pending"
    client.post(f"{BASE}/{pending.id}/approve", json={"match": pending.match_code}, headers=H(OWNER))
    monkeypatch.setattr(sdk_module.httpx, "Client", lambda base_url, timeout, headers: real(
        base_url=base_url, timeout=timeout, headers=headers, transport=httpx.MockTransport(lambda r: httpx.Response(
            200))) if "Authorization" in headers else Bridge(base_url, timeout, headers))
    harness = Harness.redeem_pairing(pending)
    try:
        assert harness.token.startswith("ha-") and harness.origin == ORIGIN
        assert harness.paired_app["catalog_app_id"] == CATALOG_ID and "token" not in harness.paired_app
    finally:
        harness.close()

    monkeypatch.setattr(sdk_module.httpx, "Client", Bridge)
    slot = client.post(BASE, json={"catalog_app_id": CATALOG_ID}, headers=H(OWNER)).json()["id"]
    claimed = Harness.claim_pairing("http://testserver", slot)
    assert claimed.state == "claimed" and claimed.match_code
    client.post(f"{BASE}/{slot}/confirm", json={"match": claimed.match_code}, headers=H(OWNER))
    monkeypatch.setattr(sdk_module.httpx, "Client", lambda base_url, timeout, headers: real(
        base_url=base_url, timeout=timeout, headers=headers) if "Authorization" in headers
        else Bridge(base_url, timeout, headers))
    native = Harness.redeem_pairing(claimed)
    try:
        assert native.token.startswith("ha-") and native.paired_app["origins"] == []
    finally:
        native.close()


def test_migration_adds_the_table(tmp_path):
    shipped = [s for s in migrations.discover() if s[0] <= 56]
    assert shipped[-1][0] == 56
    path = tmp_path / "harness.db"
    old = Database(path, migrations=shipped[:-1])
    assert not old.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'pairing_requests'").fetchone()
    old.close()
    db = Database(path, migrations=shipped)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 56
    columns = {r["name"] for r in db.conn.execute("PRAGMA table_info(pairing_requests)")}
    assert {"challenge_hash", "match_code", "state", "source", "key_id"} <= columns
    assert not columns & {"token", "verifier", "code_verifier", "secret"}
    assert list((tmp_path / "pre-migration").glob("harness-v55-*.sqlite3"))
    db.close()
