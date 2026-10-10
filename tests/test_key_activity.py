"""Successful scoped/owner authentication updates inventory activity, never rejected credentials."""
import time

from fastapi.testclient import TestClient
import pytest

from harness.api import create_app
from harness.manager import Manager
from test_daemon import make_cfg


def headers(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def test_session_creation_polling_and_admin_reads_update_activity(tmp_path, monkeypatch):
    m = Manager(make_cfg(tmp_path))
    monkeypatch.setattr(m, "_spawn", lambda sid: None)
    app, token = m.db.create_api_key("App", "sessions", kind="app")
    owner, owner_token = m.db.create_api_key("Owner", "admin", kind="owner")
    assert m.db.api_key_by_secret(token)["last_used_at"] is None
    with TestClient(create_app(m)) as client:
        response = client.post("/api/v1/sessions", headers=headers(token), json={"prompt": "synthetic task"})
        assert response.status_code == 201, response.text
        assert m.db.api_key_by_secret(token)["last_used_at"] is not None
        m.db.main.write(lambda: m.db.main.conn.execute(
            "UPDATE api_keys SET last_used_at = 1 WHERE id = ?", (app["id"],)))
        polled = client.get("/api/v1/sessions/" + response.json()["id"], headers=headers(token))
        assert polled.status_code == 200
        assert m.db.api_key_by_secret(token)["last_used_at"] > 1
        assert client.get("/api/admin/v1/keys", headers=headers(owner_token)).status_code == 200
        assert m.db.api_key_by_secret(owner_token)["last_used_at"] is not None
        inventory = client.get("/api/admin/v1/hub").json()
        states = {row["id"]: row["state"] for row in inventory["apps"]}
        assert states[app["id"]] == states[owner["id"]] == "active"
        assert client.get("/api/admin/v1/hub", headers=headers(owner_token)).status_code == 200
        assert m.db.list_api_keys()[0]["last_used_at"] > time.time() - 60


@pytest.mark.parametrize("path, scope, extra, expected", [
    ("/api/v1/sessions", "inference", {}, 403),
    ("/api/v1/sessions", "sessions", {"Origin": "https://unapproved.example"}, 403),
    ("/api/admin/v1/keys", "sessions", {}, 403),
])
def test_rejected_scope_origin_or_admin_access_never_updates_activity(tmp_path, path, scope, extra, expected):
    m = Manager(make_cfg(tmp_path))
    row, token = m.db.create_api_key("App", scope, kind="app")
    with TestClient(create_app(m)) as client:
        assert client.get(path, headers=headers(token, **extra)).status_code == expected
    assert m.db.api_key_by_secret(token)["last_used_at"] is None


def test_activity_write_failure_preserves_successful_response(tmp_path, monkeypatch, caplog):
    m = Manager(make_cfg(tmp_path))
    row, token = m.db.create_api_key("Owner", "admin", kind="owner")
    def failed(*args):
        raise RuntimeError("synthetic-secret-write-error")
    monkeypatch.setattr(m.db.main, "touch_api_key", failed)
    with TestClient(create_app(m)) as client:
        assert client.get("/api/admin/v1/keys", headers=headers(token)).status_code == 200
    assert "key activity metadata could not be recorded" in caplog.text
    assert "synthetic-secret-write-error" not in caplog.text


def test_inference_discovery_tracks_bearer_and_anthropic_key_activity(tmp_path):
    m = Manager(make_cfg(tmp_path))
    m.cfg.endpoint.enabled = True
    device, token = m.db.create_api_key("Inference device", "inference", kind="device")
    app, app_token = m.db.create_api_key("No inference scope", "sessions", kind="app")
    with TestClient(create_app(m)) as client:
        assert client.get("/v1/models", headers=headers(app_token)).status_code == 401
        assert m.db.api_key_by_secret(app_token)["last_used_at"] is None
        for path in ("/v1/models", "/v1/capabilities"):
            for auth in (headers(token), {"x-api-key": token}):
                m.db.main.write(lambda: m.db.main.conn.execute(
                    "UPDATE api_keys SET last_used_at = NULL WHERE id = ?", (device["id"],)))
                assert client.get(path, headers=auth).status_code == 200
                assert m.db.api_key_by_secret(token)["last_used_at"] > time.time() - 60
        inventory = client.get("/api/admin/v1/hub").json()
        states = {row["id"]: row["state"] for row in inventory["apps"]}
        assert states[device["id"]] == "active" and states[app["id"]] == "never_used"
