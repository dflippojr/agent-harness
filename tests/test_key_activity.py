"""Successful scoped/owner authentication updates inventory activity, never rejected credentials."""
import asyncio
import time

import httpx

from fastapi.testclient import TestClient
import pytest

from harness.api import create_app
from harness.manager import Manager
from test_daemon import make_cfg


def headers(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def drain_activity(client, manager):
    async def drain():
        await asyncio.gather(*list(manager._key_activity_tasks.values()))
    client.portal.call(drain)


def test_session_creation_polling_and_admin_reads_update_activity(tmp_path, monkeypatch):
    m = Manager(make_cfg(tmp_path))
    monkeypatch.setattr(m, "_spawn", lambda sid: None)
    app, token = m.db.create_api_key("App", "sessions", kind="app")
    owner, owner_token = m.db.create_api_key("Owner", "admin", kind="owner")
    assert m.db.api_key_by_secret(token)["last_used_at"] is None
    with TestClient(create_app(m)) as client:
        response = client.post("/api/v1/sessions", headers=headers(token), json={"prompt": "synthetic task"})
        assert response.status_code == 201, response.text
        drain_activity(client, m)
        assert m.db.api_key_by_secret(token)["last_used_at"] is not None
        m.db.main.write(lambda: m.db.main.conn.execute(
            "UPDATE api_keys SET last_used_at = 1 WHERE id = ?", (app["id"],)))
        m._key_activity_last_touch[app["id"]] -= 60
        polled = client.get("/api/v1/sessions/" + response.json()["id"], headers=headers(token))
        assert polled.status_code == 200
        drain_activity(client, m)
        assert m.db.api_key_by_secret(token)["last_used_at"] > 1
        assert client.get("/api/admin/v1/keys", headers=headers(owner_token)).status_code == 200
        drain_activity(client, m)
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
                m._key_activity_last_touch.pop(device["id"], None)
                assert client.get(path, headers=auth).status_code == 200
                drain_activity(client, m)
                assert m.db.api_key_by_secret(token)["last_used_at"] > time.time() - 60
        inventory = client.get("/api/admin/v1/hub").json()
        states = {row["id"]: row["state"] for row in inventory["apps"]}
        assert states[device["id"]] == "active" and states[app["id"]] == "never_used"


def test_burst_activity_is_background_throttled_per_key_and_pending_write(tmp_path, monkeypatch):
    m = Manager(make_cfg(tmp_path))
    key, token = m.db.create_api_key("Owner", "admin", kind="owner")
    other, other_token = m.db.create_api_key("Other", "admin", kind="owner")
    calls = []
    original = m.db.main.awrite

    async def exercise():
        release = asyncio.Event()

        async def delayed(fn, *args, **kwargs):
            if fn == m.db.main.touch_api_key:
                calls.append(args[0])
                await release.wait()
            return await original(fn, *args, **kwargs)

        monkeypatch.setattr(m.db.main, "awrite", delayed)
        app = create_app(m)
        app.state.manager = m
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{m.cfg.port}") as client:
            async def read(credential):
                response = await asyncio.wait_for(client.get("/api/admin/v1/keys", headers=headers(credential)), 2)
                assert response.status_code == 200
                await asyncio.sleep(0)

            try:
                for _ in range(20):
                    await read(token)
                assert calls == [key["id"]]
                # Even after the window expires, a still-pending write suppresses another write.
                m._key_activity_last_touch[key["id"]] -= 60
                await read(token)
                assert calls == [key["id"]]
                await read(other_token)
                assert calls == [key["id"], other["id"]]
                release.set()
                await asyncio.gather(*list(m._key_activity_tasks.values()))
                await read(token)
                await asyncio.gather(*list(m._key_activity_tasks.values()))
                assert calls == [key["id"], other["id"], key["id"]]
                for _ in range(20):
                    await read(token)
                assert calls.count(key["id"]) == 2
            finally:
                release.set()
                await asyncio.gather(*list(m._key_activity_tasks.values()), return_exceptions=True)

    try:
        asyncio.run(exercise())
        assert m.db.api_key_by_secret(token)["last_used_at"] is not None
    finally:
        m.db.close()


@pytest.mark.parametrize("stream", [False, True])
def test_inference_accounting_does_not_queue_an_extra_activity_write(tmp_path, monkeypatch, stream):
    from test_module_endpoint import endpoint_manager
    m = endpoint_manager(tmp_path)
    key, token = m.db.create_api_key("Inference", "inference", kind="device")
    calls = []
    monkeypatch.setattr(m, "record_key_activity", calls.append)
    if stream:
        m.endpoint_transport = httpx.MockTransport(lambda request: httpx.Response(
            200, content=b'data: {"usage":{"prompt_tokens":2}}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"}))
    with TestClient(create_app(m)) as client:
        response = client.post("/v1/chat/completions", headers=headers(token), json={"stream": stream})
        assert response.status_code == 200
        assert m.db.api_key_by_secret(token)["last_used_at"] is not None
        assert calls == []
        # Discovery and validation errors have no accounting row, so still need background activity.
        assert client.get("/v1/models", headers=headers(token)).status_code == 200
        assert client.post("/v1/chat/completions", headers=headers(token), content=b"invalid").status_code == 400
        assert calls == [key["id"], key["id"]]
