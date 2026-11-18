"""#946: what `GET /api/v1/backends` tells an App about the backends it can use.

- The local model (`local`, the documented session backend) is listed to App and device tokens, as discovery's
  `features.app_tools_only_backends` and the admin list already do. Listing it reports a permission every App
  already has; it grants nothing new.
- Claude's `logged_in` reflects the credential a session would really use: with the owner's token configured (#390)
  that is the token, not the shared login volume, and an App the token is not for is never logged in on it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from harness import backend_state
from harness.api import create_app
from harness.config import BackendConfig
from harness.llm import Completion
from harness.manager import Manager
from test_daemon import Script, make_cfg

SECRET = "sk-ant-oat01-NEVER-IN-A-RESPONSE"


def _manager(tmp_path, **claude) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", **claude)
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.6-sol")
    cfg.backends["cursor"] = BackendConfig(enabled=True, model="composer-2.5-fast")
    return Manager(cfg, chat=Script([Completion(content="hi")]))


def _key(client, kind="app", name="planner") -> tuple[dict, dict]:
    key = client.post("/keys", json={"name": name, "kind": kind, "scopes": ["sessions"]}).json()
    return {"Authorization": f"Bearer {key['key']}"}, key


def _token(tmp_path, expires="2099-01-01") -> str:
    path = tmp_path / "claude-oauth-token"
    path.write_text(SECRET + "\n", encoding="utf-8")
    if expires:
        (tmp_path / "claude-oauth-token.expires").write_text(expires, encoding="utf-8")
    return str(path)


# --- the local model is listed --------------------------------------------------------------------------------------

def test_an_app_sees_the_local_model_it_can_start_sessions_on(tmp_path):
    manager = _manager(tmp_path)
    with TestClient(create_app(manager)) as client:
        headers, _ = _key(client)
        rows = client.get("/api/v1/backends", headers=headers).json()
        assert [row["name"] for row in rows][:1] == ["local"]
        local = rows[0]
        assert local["available"] and local["logged_in"] and local["app_tools_only"]
        assert local["model"] == manager.cfg.default_model
        assert local["provider_policy"] is None  # the local model is no provider: no policy to report


def test_the_list_agrees_with_discovery_on_app_tools_only_backends(tmp_path):
    """The class of drift behind #946: discovery named `local` as an App-tools-only backend the list never showed."""
    manager = _manager(tmp_path)
    with TestClient(create_app(manager)) as client:
        headers, _ = _key(client)
        advertised = client.get("/api/v1").json()["features"]["app_tools_only_backends"]
        rows = client.get("/api/v1/backends", headers=headers).json()
        assert [row["name"] for row in rows if row["app_tools_only"]] == advertised
        assert {row["name"] for row in rows} >= set(advertised)


def test_a_device_token_sees_the_local_model_too(tmp_path):
    manager = _manager(tmp_path)
    with TestClient(create_app(manager)) as client:
        headers, _ = _key(client, kind="device", name="laptop")
        rows = {row["name"]: row for row in client.get("/api/v1/backends", headers=headers).json()}
        assert rows["local"]["available"]
        assert rows["claude"]["available"] is False  # device tokens still never reach a hosted backend (#786)


def test_the_local_row_follows_the_local_model_module(tmp_path):
    manager = _manager(tmp_path)
    manager.cfg.modules.local_model = False
    with TestClient(create_app(manager)) as client:
        headers, _ = _key(client)
        rows = {row["name"]: row for row in client.get("/api/v1/backends", headers=headers).json()}
        assert rows["local"]["available"] is False and rows["local"]["app_tools_only"] is False


def test_listing_local_grants_nothing_new_any_app_could_already_start_a_local_session(tmp_path):
    """No widening (#946 criterion 3): an App, managed or not, starts `local` sessions with no provider grant."""
    manager = _manager(tmp_path)
    manager._spawn = lambda *_a, **_k: None
    with TestClient(create_app(manager)) as client:
        plain, _ = _key(client, name="plain")
        managed, key = _key(client, name="managed")
        manager.set_app_provider_credential(key["id"], "claude", "", "subscription", ["claude-opus-5"])
        assert manager.db.app_provider_managed(key["id"])
        for headers in (plain, managed):
            created = client.post("/api/v1/sessions", headers=headers, json={"prompt": "hi", "backend": "local"})
            assert created.status_code == 201, created.text
            assert created.json()["backend"] == "local"


# --- Claude's logged_in under the owner's token ---------------------------------------------------------------------
# conftest stubs the shared-login-volume probe to "not logged in", as on a server that moved to the token.

def test_with_the_owner_token_web_is_logged_in_although_the_login_volume_is_empty(tmp_path):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path))
    row = backend_state.view(manager, "claude")
    assert row["logged_in"] is True
    assert SECRET not in str(row)


def test_an_app_the_token_is_for_is_logged_in_and_one_it_is_not_for_is_not(tmp_path, monkeypatch):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path), oauth_token_apps=["k-owner-app"])
    assert backend_state.view(manager, "claude", app_id="k-owner-app")["logged_in"] is True
    # A stale shared login must not make the other App look signed in: its sessions are refused (#390).
    monkeypatch.setattr(backend_state, "_probe_subscription", lambda *_a, **_k: True)
    backend_state._auth_cache.clear()
    assert backend_state.view(manager, "claude", app_id="k-other-app")["logged_in"] is False


def test_a_missing_empty_or_expired_token_is_not_logged_in(tmp_path):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path, expires="2020-01-01"))
    assert backend_state.view(manager, "claude")["logged_in"] is False
    empty = tmp_path / "claude-oauth-token"
    empty.write_text("", encoding="utf-8")
    (tmp_path / "claude-oauth-token.expires").unlink()
    assert backend_state.view(manager, "claude")["logged_in"] is False
    manager.cfg.backends["claude"].oauth_token_file = str(tmp_path / "gone")
    assert backend_state.view(manager, "claude")["logged_in"] is False


def test_a_token_with_no_expiry_file_is_not_logged_in(tmp_path):
    """Fail closed (#947 security round): a nonempty token file alone is no login without valid expiry evidence."""
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path, expires=""))
    assert backend_state.view(manager, "claude")["logged_in"] is False


def test_a_malformed_expiry_is_not_logged_in(tmp_path):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path, expires=""))
    for bad in ("not-a-date", "2099-13-40", "", "  "):
        (tmp_path / "claude-oauth-token.expires").write_text(bad, encoding="utf-8")
        assert backend_state.view(manager, "claude")["logged_in"] is False, repr(bad)


def test_an_unreadable_expiry_is_not_logged_in(tmp_path, monkeypatch):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path))
    sidecar = tmp_path / "claude-oauth-token.expires"
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if self == sidecar:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", denied)
    assert backend_state.view(manager, "claude")["logged_in"] is False


def test_an_expired_token_stays_logged_out_when_its_expiry_goes_missing(tmp_path):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path, expires="2020-01-01"))
    assert backend_state.view(manager, "claude")["logged_in"] is False
    (tmp_path / "claude-oauth-token.expires").unlink()
    assert backend_state.view(manager, "claude")["logged_in"] is False


def test_an_allowlisted_app_fails_closed_without_expiry_too(tmp_path):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path, expires=""), oauth_token_apps=["k-owner-app"])
    assert backend_state.view(manager, "claude", app_id="k-owner-app")["logged_in"] is False


def test_public_discovery_does_not_reveal_the_token(tmp_path):
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path))
    assert backend_state.view(manager, "claude", check_auth=False)["logged_in"] is False
    with TestClient(create_app(manager)) as client:
        assert SECRET not in client.get("/api/v1").text


def test_an_app_key_still_decides_for_an_app_the_token_is_not_for(tmp_path, monkeypatch):
    secret = tmp_path / "planner.key"
    secret.write_text("planner-api-key", encoding="utf-8")
    manager = _manager(tmp_path, oauth_token_file=_token(tmp_path))
    manager.cfg.provider_secret_files = {"planner": str(secret)}
    with TestClient(create_app(manager)) as client:
        headers, key = _key(client)
        manager.set_app_provider_credential(key["id"], "claude", "planner", "api_key", [])
        row = {r["name"]: r for r in client.get("/api/v1/backends", headers=headers).json()}["claude"]
        assert row["logged_in"] is True and row["api_key_available"] is True
        assert "planner-api-key" not in str(row) and SECRET not in str(row)
