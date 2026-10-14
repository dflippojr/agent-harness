"""Owner/Hub preview uses daemon disclosure copy without allocating or changing pairing state (#604)."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from harness import cli, modules
from harness import pairing_requests as pr
from harness.apps import all_scopes
from harness.config import GuestAccess
from sdk.harness_client import Harness, HarnessError

from test_household import GUEST, H, OWNER, household
from test_hub_claim import _bearer
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
    canonical = json.dumps(response.json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert response.headers["X-Disclosures-Digest"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert "X-Disclosures-Digest" in response.headers["Access-Control-Expose-Headers"]


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


@pytest.mark.parametrize("description", ["", " \t\n"])
def test_addon_missing_description_has_warning_in_preview_arm_and_list(hh, monkeypatch, description):
    client, m = hh
    addon = modules.Module(name="example", switches=("example",), app_scopes={"example:read": description})
    original = modules.discovered
    monkeypatch.setattr(modules, "discovered", lambda cfg: (*original(cfg), addon))
    preview = client.post(PREVIEW, headers=H(OWNER), json={"scopes": ["example:read"]})
    assert preview.status_code == 200, preview.text
    assert preview.json() == [{"scope": "example:read", "tier": "standard",
                               "text": "No description was provided for scope example:read. Ask its module publisher "
                                       "what it allows before approving."}]
    armed = client.post(BASE, headers=H(OWNER), json={"catalog_app_id": CATALOG_ID, "scopes": ["example:read"]})
    assert armed.status_code == 201, armed.text
    listed = client.get(BASE, headers=H(OWNER))
    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json() if r["id"] == armed.json()["id"])
    assert preview.json() == armed.json()["disclosures"] == row["disclosures"]


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


@pytest.mark.parametrize("change", ["text", "tier", "module", "scope", "order"])
def test_arm_refuses_stale_preview_digest_without_changing_pairing_state(hh, monkeypatch, change):
    client, m = hh
    scopes = ["sessions", "images"]
    preview = client.post(PREVIEW, headers=H(OWNER), json={"scopes": scopes})
    assert preview.status_code == 200, preview.text
    before = list(m.db.main.conn.iterdump())
    if change == "text":
        monkeypatch.setitem(pr.DISCLOSURES, "sessions", "Updated session access disclosure.")
    elif change == "tier":
        monkeypatch.setattr(pr, "ELEVATED", pr.ELEVATED | {"sessions"})
    elif change == "module":
        scopes = ["example:read"]
        addon = modules.Module(name="example", switches=("example",), app_scopes={"example:read": "Read café files."})
        original = modules.discovered
        monkeypatch.setattr(modules, "discovered", lambda cfg: (*original(cfg), addon))
        preview = client.post(PREVIEW, headers=H(OWNER), json={"scopes": scopes})
        assert preview.status_code == 200, preview.text
        assert Harness.disclosures_digest(preview.json()) == preview.headers["X-Disclosures-Digest"]
        addon.app_scopes["example:read"] = "Write café files."
    elif change == "scope":
        scopes = ["sessions"]
    else:
        scopes.reverse()

    def no_sweep(*_args):
        pytest.fail("a stale digest must be refused before sweeping pairing state")

    monkeypatch.setattr(pr, "sweep", no_sweep)
    armed = client.post(BASE, headers=H(OWNER), json={
        "catalog_app_id": CATALOG_ID, "scopes": scopes, "acknowledge_elevated": True,
        "expected_disclosures_digest": preview.headers["X-Disclosures-Digest"]})
    assert armed.status_code == 409, armed.text
    assert armed.json()["error"]["code"] == "disclosures_changed"
    assert list(m.db.main.conn.iterdump()) == before


@pytest.mark.parametrize("digest_mode", ["matching", "omitted", "null"])
def test_arm_accepts_matching_digest_and_legacy_callers(hh, digest_mode):
    client, _ = hh
    scopes = ["sessions", "approvals", "sessions"]
    preview = client.post(PREVIEW, headers=H(OWNER), json={"scopes": scopes})
    body = {"catalog_app_id": CATALOG_ID, "scopes": scopes, "acknowledge_elevated": True}
    if digest_mode != "omitted":
        body["expected_disclosures_digest"] = preview.headers["X-Disclosures-Digest"] if digest_mode == "matching" else None
    armed = client.post(BASE, headers=H(OWNER), json=body)
    assert armed.status_code == 201, armed.text
    assert armed.json()["disclosures"] == preview.json()


@pytest.mark.parametrize("digest", ["", "xyz", "a" * 63, "A" * 64, 123])
def test_arm_rejects_malformed_digest_without_allocating(hh, digest):
    client, m = hh
    response = client.post(BASE, headers=H(OWNER), json={
        "catalog_app_id": CATALOG_ID, "expected_disclosures_digest": digest})
    assert response.status_code == 422, response.text
    assert m.db.main.list_pairing_requests() == []


def test_cli_arm_passes_preview_digest(hh, tmp_path, monkeypatch, capsys):
    client, _ = hh
    monkeypatch.setattr(cli, "HARNESS_HOME", tmp_path / "cli")
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", tmp_path / "cli" / "config.json")
    monkeypatch.setattr(cli, "api", lambda method, path, **kwargs: client.request(
        method, "/api/admin/v1" + path, headers=H(OWNER), **kwargs).json())
    monkeypatch.setattr("sys.argv", ["harness", "pairing-requests", "preview", "--scopes", "sessions"])
    assert cli.main() == 0
    output = capsys.readouterr()
    preview = json.loads(output.out)
    digest = output.err.strip().removeprefix("Disclosures digest: ")
    assert digest == Harness.disclosures_digest(preview)
    monkeypatch.setattr("sys.argv", ["harness", "pairing-requests", "arm", CATALOG_ID, "--scopes", "sessions",
                                    "--expected-disclosures-digest", digest])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["disclosures"] == preview


def test_sdk_binds_arm_to_preview_and_surfaces_drift(hh, monkeypatch):
    client, m = hh
    _, token = m.db.main.create_api_key("owner", "admin", "owner")
    with Harness("http://testserver", token) as sdk:
        sdk.client.close()
        sdk.client = httpx.Client(base_url=sdk.base, headers=_bearer(token), transport=httpx.MockTransport(
            lambda r: client.request(r.method, r.url.path,
                                     headers={k: v for k, v in r.headers.items() if k != "host"}, content=r.content)))
        sdk.validate_openapi(client.get("/openapi.json").json())
        preview = sdk.preview_pairing(["approvals"])
        digest = sdk.disclosures_digest(preview)
        armed = sdk.arm_pairing(CATALOG_ID, ["approvals"], acknowledge_elevated=True,
                               expected_disclosures_digest=digest)
        assert armed["disclosures"] == preview
        monkeypatch.setitem(pr.DISCLOSURES, "approvals", "New approval disclosure.")
        with pytest.raises(HarnessError) as changed:
            sdk.arm_pairing(CATALOG_ID, ["approvals"], acknowledge_elevated=True,
                           expected_disclosures_digest=digest)
        assert changed.value.status == 409
        assert changed.value.code == "disclosures_changed"
    assert len(m.db.main.list_pairing_requests()) == 1


@pytest.mark.parametrize("scope", [*all_scopes(), None], ids=[*all_scopes(), "all-scopes"])
def test_preview_arm_and_list_disclosures_match_for_every_known_scope(hh, scope):
    client, m = hh
    known = all_scopes(m.cfg)
    assert set(known) == set(all_scopes())
    scopes = list(known) if scope is None else [scope]
    preview = client.post(PREVIEW, headers=H(OWNER), json={"scopes": scopes})
    assert preview.status_code == 200, preview.text
    armed = client.post(BASE, headers=H(OWNER), json={
        "catalog_app_id": CATALOG_ID, "scopes": scopes, "acknowledge_elevated": True,
        "expected_disclosures_digest": preview.headers["X-Disclosures-Digest"]})
    assert armed.status_code == 201, armed.text
    listed = client.get(BASE, headers=H(OWNER))
    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json() if r["id"] == armed.json()["id"])
    assert preview.json() == armed.json()["disclosures"] == row["disclosures"]
