"""Agent Harness Web is a versioned first-party Agent Harness Server client."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager

from test_daemon import Script, make_cfg


ORIGIN = "https://control.example"


def make_client(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = Manager(cfg, chat=Script([Completion(content="done"), Completion(content="again")]))
    return TestClient(create_app(manager)), manager


def wait_done(manager, sid: str, timeout: float = 10) -> None:
    """The run continues on the app's loop after the create request returns (its writes are awaited, #294)."""
    deadline = time.monotonic() + timeout
    while manager.db.get_session(sid)["status"] != "done" or sid in manager.tasks:
        assert time.monotonic() < deadline, manager.db.get_session(sid)["status"]
        time.sleep(0.01)


def test_bundled_web_dogfoods_app_api_without_becoming_an_app(tmp_path):
    client, manager = make_client(tmp_path)
    with client:
        created = client.post("/api/v1/sessions", json={"prompt": "from bundled Agent Harness Web"})
        assert created.status_code == 201
        sid = created.json()["id"]
        assert manager.db.get_session(sid)["app_id"] == ""
        wait_done(manager, sid)
        listed = client.get("/api/v1/sessions").json()[0]
        assert listed["id"] == sid
        assert listed["chat_summary"] == "from bundled Agent Harness Web — done"
        assert client.get(f"/api/v1/sessions/{sid}").status_code == 200
        assert client.get(f"/api/v1/sessions/{sid}/events?follow=false").status_code == 200


def test_session_event_replay_order_and_last_event_id(tmp_path):
    client, manager = make_client(tmp_path)
    manager._spawn = lambda *_a, **_k: None
    with client:
        sid = client.post("/api/v1/sessions", json={"prompt": "stream"}).json()["id"]
        first = manager.db.insert_event(sid, "characterization_first", {"n": 1})
        second = manager.db.insert_event(sid, "characterization_second", {"n": 2})

        replay = client.get(f"/api/v1/sessions/{sid}/events?after={first['seq'] - 1}&follow=false")
        resumed = client.get(f"/api/v1/sessions/{sid}/events?after=0&follow=false",
                             headers={"Last-Event-ID": str(first["seq"])})

    assert replay.status_code == 200
    assert replay.text.index("characterization_first") < replay.text.index("characterization_second")
    assert '"n": 1' in replay.text
    assert '"n": 2' in replay.text
    assert "characterization_first" not in resumed.text
    assert "characterization_second" in resumed.text


def test_session_event_live_replay_deduplicates_and_closes(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from harness.apps import _session_events_stream

    client, manager = make_client(tmp_path)
    manager._spawn = lambda *_a, **_k: None
    with client:
        sid = client.post("/api/v1/sessions", json={"prompt": "stream"}).json()["id"]
        historical = manager.db.insert_event(sid, "historical", {"n": 1})

        async def disconnected():
            return False

        request = SimpleNamespace(is_disconnected=disconnected)

        async def consume():
            stream = _session_events_stream(request, manager, sid, "owner", historical["seq"] - 1, True)
            assert await stream.__anext__() == ": connected\n\n"
            assert "historical" in await stream.__anext__()
            subscription = next(iter(manager.bus._subs[sid]))
            subscription.queue.put_nowait({"seq": historical["seq"], "type": "duplicate"})
            manager.bus.ephemeral(sid, "token_delta", {"text": "live"})
            manager.bus.emit(sid, "live_event", {"n": 2})
            ephemeral = await stream.__anext__()
            live = await stream.__anext__()
            assert "token_delta" in ephemeral
            assert "live_event" in live
            manager.stream_epoch["owner"] = 1
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert not manager.bus._subs[sid]

        asyncio.run(consume())


def test_global_event_stream_timeout_sends_keepalive_and_closes(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from harness import apps
    from harness.apps import _global_events_stream
    from harness.api import GLOBAL_TYPES

    client, manager = make_client(tmp_path)
    with client:
        async def disconnected():
            return False

        request = SimpleNamespace(is_disconnected=disconnected)
        key = {"kind": "owner", "scope_set": set()}

        async def timeout_then_disconnect(awaitable, timeout):
            if hasattr(awaitable, "close"):
                awaitable.close()
            manager.stream_epoch["owner"] = 1
            raise asyncio.TimeoutError

        monkeypatch.setattr(apps.asyncio, "wait_for", timeout_then_disconnect)

        async def consume():
            stream = _global_events_stream(request, manager, "owner", key, 0, GLOBAL_TYPES)
            assert await stream.__anext__() == ": connected\n\n"
            assert await stream.__anext__() == ": keepalive\n\n"
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert not manager.bus._subs["*"]

            manager.stream_epoch["owner"] = 0

            async def now_disconnected():
                return True

            stream = _global_events_stream(SimpleNamespace(is_disconnected=now_disconnected), manager,
                                           "owner", key, 0, GLOBAL_TYPES)
            assert await stream.__anext__() == ": connected\n\n"
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert not manager.bus._subs["*"]

        asyncio.run(consume())


def test_session_event_stream_timeout_sends_keepalive_and_closes(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from harness import apps
    from harness.apps import _session_events_stream

    client, manager = make_client(tmp_path)
    manager._spawn = lambda *_a, **_k: None
    with client:
        sid = client.post("/api/v1/sessions", json={"prompt": "stream"}).json()["id"]
        manager.stream_epoch["owner"] = 0

        async def disconnected():
            return False

        request = SimpleNamespace(is_disconnected=disconnected)

        async def timeout_and_reconnect_epoch(awaitable, timeout):
            if hasattr(awaitable, "close"):
                awaitable.close()
            manager.stream_epoch["owner"] = 1
            raise asyncio.TimeoutError

        monkeypatch.setattr(apps.asyncio, "wait_for", timeout_and_reconnect_epoch)

        async def consume():
            stream = _session_events_stream(request, manager, sid, "owner", 10**9, True)
            assert await stream.__anext__() == ": connected\n\n"
            assert await stream.__anext__() == ": keepalive\n\n"
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert not manager.bus._subs[sid]

            manager.stream_epoch["owner"] = 0

            async def now_disconnected():
                return True

            stream = _session_events_stream(SimpleNamespace(is_disconnected=now_disconnected), manager,
                                            sid, "owner", 10**9, True)
            assert await stream.__anext__() == ": connected\n\n"
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            assert not manager.bus._subs[sid]

        asyncio.run(consume())


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
    assert '"/api/v1"' in client
    assert '"/api/admin/v1"' in client
    assert "body instanceof FormData" in client
    index = (web / "index.html").read_text(encoding="utf-8")
    assert 'src="/app.js?v=5"' in index
    assert 'href="/style.css?v=5"' in index
    assert "<title>Agent Harness Web</title>" in index
    assert 'apple-mobile-web-app-title" content="Harness"' in index
    manifest = (web / "manifest.webmanifest").read_text(encoding="utf-8")
    assert '"name": "Agent Harness Web"' in manifest
    assert '"short_name": "Harness"' in manifest
    assert "Agent Harness Server URL" in app
    assert "Connect another Agent Harness Web" in app
    assert 'name: `agent-harness-web (' in app
    assert '"/client.mjs"' in worker
    # The shell cache name is derived from BUILD_ID so a client build bumps it automatically (#69).
    assert 'const BUILD_ID = "' in worker
    assert "const SHELL = `harness-shell-${BUILD_ID}`" in worker
    assert 'fetch(event.request, { cache: "no-cache" })' in worker


def test_web_assets_work_at_static_root_and_compatibility_alias(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        for path in ("/app.js", "/client.mjs", "/style.css", "/static/app.js"):
            response = client.get(path)
            assert response.status_code == 200
        # browsers refuse a module script served without a JavaScript MIME type
        for path in ("/lib/markdown.mjs", "/static/lib/markdown.mjs"):
            response = client.get(path)
            assert response.status_code == 200
            assert "javascript" in response.headers["content-type"]
