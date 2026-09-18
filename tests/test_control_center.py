"""Agent Harness Web is a versioned first-party Agent Harness Server client."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager

from test_daemon import Script, make_cfg


ORIGIN = "https://control.example"


def make_client(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = Manager(cfg, chat=Script([Completion(content="done"), Completion(content="again")]))
    return TestClient(create_app(manager)), manager


def test_bundled_web_dogfoods_app_api_without_becoming_an_app(tmp_path):
    client, manager = make_client(tmp_path)
    with client:
        created = client.post("/api/v1/sessions", json={"prompt": "from bundled Agent Harness Web"})
        assert created.status_code == 201
        sid = created.json()["id"]
        assert manager.db.get_session(sid)["app_id"] == ""
        listed = client.get("/api/v1/sessions").json()[0]
        assert listed["id"] == sid
        assert listed["chat_summary"] == "from bundled Agent Harness Web — done"
        assert client.get(f"/api/v1/sessions/{sid}").status_code == 200
        assert client.get(f"/api/v1/sessions/{sid}/events?follow=false").status_code == 200


def test_independent_web_owner_token_cors_and_stream_ticket(tmp_path):
    client, manager = make_client(tmp_path)
    with client:
        existing = client.post("/sessions", json={"prompt": "existing"}).json()["id"]
        minted = client.post("/api/admin/v1/keys", json={
            "name": "agent-harness-web", "kind": "owner", "scopes": ["admin"], "origins": [ORIGIN],
        })
        assert minted.status_code == 201
        assert minted.json()["origins"] == [ORIGIN]
        token = minted.json()["key"]
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}

        for path, method in [("/api/admin/v1/me", "GET"), ("/api/v1/sessions", "POST")]:
            preflight = client.options(path, headers={
                "Origin": ORIGIN, "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "authorization, content-type",
            })
            assert preflight.status_code == 204
            assert preflight.headers["access-control-allow-origin"] == ORIGIN

        me = client.get("/api/admin/v1/me", headers=headers)
        assert me.status_code == 200
        assert me.headers["access-control-allow-origin"] == ORIGIN
        assert client.get(f"/api/v1/sessions/{existing}", headers=headers).status_code == 200

        created = client.post("/api/v1/sessions", headers=headers, json={"prompt": "remote owner"})
        assert created.status_code == 201
        sid = created.json()["id"]
        assert manager.db.get_session(sid)["app_id"] == ""
        ticket = client.post(f"/api/v1/sessions/{sid}/events/ticket", headers=headers)
        assert ticket.status_code == 201
        streamed = client.get(ticket.json()["events_url"] + "&follow=false", headers={"Origin": ORIGIN})
        assert streamed.status_code == 200
        assert streamed.headers["access-control-allow-origin"] == ORIGIN

        wrong = {"Authorization": f"Bearer {token}", "Origin": "https://wrong.example"}
        assert client.get("/api/admin/v1/me", headers=wrong).status_code == 403
        assert client.get("/api/v1/sessions", headers=wrong).status_code == 403


def test_manual_browser_origins_are_owner_only(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        response = client.post("/api/admin/v1/keys", json={
            "name": "not owner", "kind": "app", "scopes": ["sessions"], "origins": [ORIGIN],
        })
        assert response.status_code == 400
        assert "owner-only" in response.json()["detail"]


def test_app_origin_cannot_use_ambient_owner_identity_on_admin_api(tmp_path):
    client, manager = make_client(tmp_path)
    with client:
        manager.db.create_api_key("ordinary browser", "sessions", "app", [ORIGIN])
        preflight = client.options("/api/admin/v1/me", headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
        })
        assert preflight.status_code == 403

        response = client.get("/api/admin/v1/me", headers={"Origin": ORIGIN})
        assert response.status_code == 401
        assert "owner token" in response.json()["detail"]


def test_cross_origin_admin_request_never_falls_back_to_ambient_owner(tmp_path):
    client, manager = make_client(tmp_path)
    with client:
        # A pre-#91 display name remains a valid, visible credential.
        legacy, _ = manager.db.create_api_key("Control Center", "admin", "owner", [ORIGIN])
        assert manager.db.get_api_key(legacy["id"])["name"] == "Control Center"
        response = client.get("/api/admin/v1/me", headers={"Origin": ORIGIN})
        assert response.status_code == 401
        assert "owner token" in response.json()["detail"]


def test_web_shell_includes_transport_module_and_canonical_names():
    web = Path(__file__).parents[1] / "harness" / "web"
    app = (web / "app.js").read_text(encoding="utf-8")
    client = (web / "client.mjs").read_text(encoding="utf-8")
    worker = (web / "sw.js").read_text(encoding="utf-8")
    assert 'from "./client.mjs"' in app
    assert '"/api/v1"' in client and '"/api/admin/v1"' in client
    assert "body instanceof FormData" in client
    index = (web / "index.html").read_text(encoding="utf-8")
    assert 'src="/app.js?v=4"' in index and 'href="/style.css?v=4"' in index
    assert "<title>Agent Harness Web</title>" in index
    assert 'apple-mobile-web-app-title" content="Harness"' in index
    manifest = (web / "manifest.webmanifest").read_text(encoding="utf-8")
    assert '"name": "Agent Harness Web"' in manifest
    assert '"short_name": "Harness"' in manifest
    assert "Agent Harness Server URL" in app
    assert "Connect another Agent Harness Web" in app
    assert 'name: `agent-harness-web (' in app
    assert '"/client.mjs"' in worker
    assert 'const SHELL = "harness-shell-v4"' in worker
    assert 'fetch(event.request, { cache: "no-cache" })' in worker


def test_web_assets_work_at_static_root_and_compatibility_alias(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        for path in ("/app.js", "/client.mjs", "/style.css", "/static/app.js"):
            response = client.get(path)
            assert response.status_code == 200
