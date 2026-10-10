"""Synthetic regression coverage for guest reads, device scope, and stream revocation (#527)."""

import asyncio
from types import SimpleNamespace

import pytest

from harness import apps
from harness.access import guest_forbidden, resolve_access
from harness.api import GLOBAL_TYPES
from harness.config import GuestAccess
from test_api import LOGIN, make_client as guest_client
from test_control_center import ORIGIN, make_client
from test_phase7 import seed


OWNER_STATUS_PATHS = (
    "/memory", "/pairing-codes", "/runner-pairing-codes", "/runners", "/backends",
    "/gpu", "/resources", "/resources/diagnostics", "/models/status",
)


@pytest.mark.parametrize("path", OWNER_STATUS_PATHS)
def test_guest_owner_status_denied_owner_still_allowed(tmp_path, monkeypatch, path):
    from harness_modules.local_model import routes
    from harness_modules.local_model import resources

    client, m, _ = guest_client(tmp_path, [])
    m.cfg.guests = [GuestAccess(login="guest@example.com", until="2099-01-01T00:00:00+00:00")]

    async def fake_status(*_args):
        return {"state": "unloaded"}

    async def fake_model_state(*_args):
        return "unloaded"

    monkeypatch.setattr(routes, "_resources_status", fake_status)
    monkeypatch.setattr(resources, "diagnostics", fake_status)
    monkeypatch.setattr(m.warmer, "state", fake_model_state)
    with client:
        guest = {"Tailscale-User-Login": "guest@example.com"}
        assert client.get(path, headers=guest).status_code == 403
        assert client.get(path, headers={"Tailscale-User-Login": LOGIN}).status_code == 200


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_guest_allowlist_denies_unknown_and_status_subroutes(tmp_path, method):
    client, m, _ = guest_client(tmp_path, [])
    m.cfg.guests = [GuestAccess(login="guest@example.com", until="2099-01-01T00:00:00+00:00")]
    guest = resolve_access(m.cfg, "guest@example.com")
    for path in (*OWNER_STATUS_PATHS, "/future-owner-status", "/models/future-status", "/profile/secrets",
                 "/memory/profile", "/api/v1/me", "/api/v1/events", "/sessions/x/future-admin"):
        assert guest_forbidden(guest, method, path) is not None
    for path in ("/", "/static/app.js", "/lib/api.mjs", "/me", "/profile", "/models", "/sessions",
                 "/sessions/x/events", "/search", "/queue", "/templates"):
        assert guest_forbidden(guest, method, path) is None
    m.db.close()


@pytest.mark.parametrize("read_all", [False, True])
def test_device_search_queue_and_global_events_require_read_all(tmp_path, monkeypatch, read_all):
    client, m = make_client(tmp_path)
    m.cfg.search.enabled = True
    key, token = m.db.create_api_key("device", "sessions" + (" sessions:all" if read_all else ""), "device")
    key = m.db.api_key_by_secret(token)
    key["scope_set"] = set(key["scopes"].split())
    with client:
        seed(m.db, "owner00001", "scratch", "owner secret", [("user_message", {"content": "boundarymarker"})])
        foreign, _ = m.db.create_api_key("foreign", "sessions", "app")
        seed(m.db, "foreign001", "scratch", "foreign secret", [("user_message", {"content": "boundarymarker"})],
             app_id=foreign["id"])
        monkeypatch.setattr(m.scheduler, "positions", lambda: {"owner00001": 1, "foreign001": 2})
        headers = {"Authorization": f"Bearer {token}"}
        results = client.get("/api/v1/search?q=boundarymarker", headers=headers)
        assert results.status_code == 200
        assert [row["id"] for row in results.json()["results"]] == (["owner00001"] if read_all else [])
        assert client.get("/api/v1/queue", headers=headers).json() == (
            [{"session_id": "owner00001", "position": 1}] if read_all else [])

        original_wait = asyncio.wait_for

        async def short_tick(awaitable, timeout):
            return await original_wait(awaitable, timeout=0.01)

        async def disconnected():
            return False

        monkeypatch.setattr(apps.asyncio, "wait_for", short_tick)

        async def consume():
            stream = apps._global_events_stream(SimpleNamespace(is_disconnected=disconnected), m, "owner", key,
                                                0, GLOBAL_TYPES)
            assert await anext(stream) == ": connected\n\n"
            m.bus.emit("foreign001", "status", {"status": "done"})
            m.bus.emit("owner00001", "status", {"status": "done"})
            chunk = await anext(stream)
            assert ('"session_id": "owner00001"' in chunk) if read_all else chunk == ": keepalive\n\n"
            assert "foreign001" not in chunk
            await stream.aclose()
            assert not m.bus._subs["*"]

        asyncio.run(consume())


@pytest.mark.parametrize("kind,restore", [("owner", False), ("app", False), ("device", False),
                                         ("app", True), ("device", True)])
@pytest.mark.parametrize("surface,tick", [(surface, tick) for surface in ("global", "session", "ticket")
                                         for tick in ("event", "keepalive", "replay")
                                         if (surface, tick) != ("global", "replay")])
def test_revocation_closes_stream_on_next_tick_and_reconnect_is_unauthorized(
        tmp_path, monkeypatch, kind, restore, surface, tick):
    client, m = make_client(tmp_path)
    key, token = m.db.create_api_key("reader", "admin" if kind == "owner" else "sessions sessions:all", kind,
                                     origins=[ORIGIN])
    key = m.db.api_key_by_secret(token)
    key["scope_set"] = set(key["scopes"].split())
    with client:
        seed(m.db, "owner00001", "scratch", "stream", [("status", {"status": "done"})])
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        if surface == "ticket":
            ticket = client.post("/api/v1/sessions/owner00001/events/ticket", headers=headers)
            assert ticket.status_code == 201
            reconnect_path = ticket.json()["events_url"] + "&follow=false"
            reconnect_headers = {"Origin": ORIGIN}
            key = m.db.stream_ticket_key(ticket.json()["ticket"], "owner00001", ORIGIN)
        else:
            reconnect_path = "/api/v1/events" if surface == "global" else "/api/v1/sessions/owner00001/events?follow=false"
            reconnect_headers = headers

        async def disconnected():
            return False

        restored = []

        def revoke():
            assert m.db.revoke_api_key(key["id"])
            if restore:
                fresh_key, fresh_token = m.db.restore_api_key(key["id"])
                assert fresh_key["id"] == key["id"]
                restored.append(fresh_token)

        async def revoke_during_wait(awaitable, timeout):
            revoke()
            if tick == "keepalive":
                awaitable.close()
                raise asyncio.TimeoutError
            m.bus.emit("owner00001", "status", {"status": "done", "answer": "must not leak"})
            return await awaitable

        monkeypatch.setattr(apps.asyncio, "wait_for", revoke_during_wait)

        async def consume():
            request = SimpleNamespace(is_disconnected=disconnected)
            if surface == "global":
                stream = apps._global_events_stream(request, m, "owner", key, 0, GLOBAL_TYPES)
                subscription = "*"
            else:
                stream = apps._session_events_stream(request, m, "owner00001", "owner",
                                                    0 if tick == "replay" else 10**9, True, key)
                subscription = "owner00001"
            assert await anext(stream) == ": connected\n\n"
            if tick == "replay":
                revoke()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert not m.bus._subs[subscription]

        asyncio.run(consume())
        assert client.get(reconnect_path, headers=reconnect_headers).status_code == 401

        if restore:
            new_headers = {"Authorization": f"Bearer {restored[0]}", "Origin": ORIGIN}
            new_stream = client.get("/api/v1/sessions/owner00001/events?follow=false", headers=new_headers)
            assert new_stream.status_code == 200
            assert '"status": "done"' in new_stream.text
