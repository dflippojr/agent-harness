"""Owner/Hub preview uses daemon disclosure copy without allocating or changing pairing state (#604)."""
from __future__ import annotations

import json

import httpx
import pytest

from harness import cli, modules
from harness import pairing_requests as pr
from harness.apps import all_scopes
from harness.config import GuestAccess
from sdk.harness_client import Harness, HarnessError

from test_household import ALICE, GUEST, H, OWNER, create_member, household
from test_hub_claim import HUB_ORIGIN, _bearer, _claim
from test_pairing_requests import BASE, CATALOG_ID, _ask

PREVIEW = BASE + "/preview"


@pytest.fixture
def hh(tmp_path):
    client, manager = household(tmp_path, guests=[GuestAccess(login=GUEST, until="2099-01-01T00:00:00+00:00")])
    with client:
        yield client, manager


def test_preview_discloses_every_builtin_scope_and_deduplicates_in_order(hh):
    client, m = hh
    scopes = list(all_scopes(m.cfg))
    response = client.post(PREVIEW, headers=H(OWNER), json={"scopes": scopes + scopes[:2]})
    assert response.status_code == 200, response.text
    assert response.json() == [{"scope": s, "tier": "elevated" if s in pr.ELEVATED else "standard",
                                "text": pr.DISCLOSURES[s]} for s in scopes]
    assert {d["scope"] for d in response.json() if d["tier"] == "elevated"} == pr.ELEVATED
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_preview_uses_addon_declared_text_from_the_daemons_config(hh, monkeypatch):
    client, m = hh
    addon = modules.Module(name="example", switches=("example",),
                           app_scopes={"example:read": "This app can read your example collection."})
    original = modules.discovered

    def discovered(cfg):
        assert cfg is m.cfg
        return (*original(cfg), addon)

    monkeypatch.setattr(modules, "discovered", discovered)
    response = client.post(PREVIEW, headers=H(OWNER), json={"scopes": ["example:read", "sessions"]})
    assert response.status_code == 200, response.text
    assert response.json()[0] == {"scope": "example:read", "tier": "standard",
                                  "text": addon.app_scopes["example:read"]}
    armed = client.post(BASE, headers=H(OWNER), json={"catalog_app_id": CATALOG_ID,
                                                  "scopes": ["example:read", "sessions"]})
    assert armed.status_code == 201, armed.text
    assert armed.json()["disclosures"] == response.json()


@pytest.mark.parametrize("scopes", [[], ["bogus"], ["sessions", "bogus"], ["admin"], ["sessions", "admin"],
                                    [" sessions"], ["sessions approvals"]])
def test_preview_refuses_the_same_invalid_app_scopes_as_arming(hh, scopes):
    client, _ = hh
    preview = client.post(PREVIEW, headers=H(OWNER), json={"scopes": scopes})
    armed = client.post(BASE, headers=H(OWNER), json={"catalog_app_id": CATALOG_ID, "scopes": scopes})
    assert preview.status_code == armed.status_code == 400
    assert preview.json()["detail"] == armed.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"scopes": None}, {"scopes": "sessions"}, {"scopes": [1]}])
def test_preview_requires_a_scope_list(hh, body):
    client, _ = hh
    assert client.post(PREVIEW, headers=H(OWNER), json=body).status_code == 422


@pytest.mark.parametrize("body, status", [({"scopes": ["sessions", "approvals"]}, 200),
                                         ({"scopes": []}, 400), ({"scopes": ["admin"]}, 400), ({}, 422)])
def test_preview_leaves_all_database_state_untouched_even_after_expiry(hh, monkeypatch, body, status):
    client, m = hh
    now = [1_900_000_000.0]
    monkeypatch.setattr(pr, "clock", lambda: now[0])
    asked, _ = _ask(client)
    assert asked.status_code == 201
    slot = client.post(BASE, headers=H(OWNER), json={"catalog_app_id": CATALOG_ID})
    assert slot.status_code == 201
    before = list(m.db.main.conn.iterdump())
    now[0] += pr.KEEP_SECONDS + pr.APPROVE_TTL_SECONDS + 1

    def no_allocation_or_sweep(*_args, **_kwargs):
        pytest.fail("preview must not allocate pairing material or sweep existing requests")

    monkeypatch.setattr(pr, "sweep", no_allocation_or_sweep)
    monkeypatch.setattr(pr, "_unused_match_code", no_allocation_or_sweep)
    response = client.post(PREVIEW, headers=H(OWNER), json=body)
    assert response.status_code == status, response.text
    assert list(m.db.main.conn.iterdump()) == before
    if response.status_code == 200:
        assert set(response.json()[0]) == {"scope", "tier", "text"}


def test_preview_accepts_owner_identity_owner_key_and_origin_bound_hub_key(hh):
    client, m = hh
    _, owner_token = m.db.main.create_api_key("owner", "admin", "owner")
    hub_token, _, _ = _claim(client, m)
    client.local_owner = False
    for headers in (H(OWNER), _bearer(owner_token), _bearer(hub_token), _bearer(hub_token, HUB_ORIGIN)):
        response = client.post(PREVIEW, headers=headers, json={"scopes": ["approvals"]})
        assert response.status_code == 200, response.text
        assert response.json() == [{"scope": "approvals", "tier": "elevated", "text": pr.DISCLOSURES["approvals"]}]
    assert client.post(PREVIEW, headers=_bearer(hub_token, "https://other.example"),
                       json={"scopes": ["sessions"]}).status_code == 403
    assert client.post(PREVIEW, headers={**H(OWNER), "Origin": "https://other.example"},
                       json={"scopes": ["sessions"]}).status_code == 403


def test_preview_refuses_apps_devices_members_guests_and_unauthenticated_callers(hh):
    client, m = hh
    create_member(client, ALICE, "Alice")
    client.local_owner = False
    assert client.post(PREVIEW, json={"scopes": ["sessions"]}).status_code == 401
    for headers in (H(ALICE), H(GUEST), H("stranger@example.com")):
        assert client.post(PREVIEW, headers=headers, json={"scopes": ["sessions"]}).status_code == 403
    for kind, scopes in (("app", "sessions approvals"), ("device", "inference"), ("owner", "sessions")):
        _, token = m.db.main.create_api_key(kind, scopes, kind)
        response = client.post(PREVIEW, headers={**H(OWNER), **_bearer(token)}, json={"scopes": ["sessions"]})
        assert response.status_code == 403, response.text
    assert client.post(PREVIEW, headers=_bearer("invalid"), json={"scopes": ["sessions"]}).status_code == 401


def test_cli_previews_without_arming(hh, tmp_path, monkeypatch, capsys):
    client, m = hh
    calls = []

    def api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return client.request(method, "/api/admin/v1" + path, headers=H(OWNER), **kwargs).json()

    monkeypatch.setattr(cli, "HARNESS_HOME", tmp_path / "cli")
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", tmp_path / "cli" / "config.json")
    monkeypatch.setattr(cli, "api", api)
    monkeypatch.setattr("sys.argv", ["harness", "pairing-requests", "preview", "--scopes", "sessions", "approvals"])
    assert cli.main() == 0
    assert calls == [("POST", "/pairing-requests/preview", {"json": {"scopes": ["sessions", "approvals"]}})]
    assert json.loads(capsys.readouterr().out) == pr.disclosures(["sessions", "approvals"], m.cfg)
    assert m.db.main.list_pairing_requests() == []


def test_sdk_previews_with_owner_auth_and_validates_the_contract(hh):
    client, m = hh
    _, token = m.db.main.create_api_key("owner", "admin", "owner")
    with Harness("http://testserver", token) as sdk:
        sdk.client.close()
        sdk.client = httpx.Client(base_url=sdk.base, headers=_bearer(token), transport=httpx.MockTransport(
            lambda r: client.request(r.method, r.url.path,
                                     headers={k: v for k, v in r.headers.items() if k != "host"}, content=r.content)))
        sdk.validate_openapi(client.get("/openapi.json").json())
        assert sdk.preview_pairing(["sessions", "approvals"]) == pr.disclosures(["sessions", "approvals"], m.cfg)
        with pytest.raises(HarnessError) as invalid:
            sdk.preview_pairing(["admin"])
        assert invalid.value.status == 400
    assert m.db.main.list_pairing_requests() == []
