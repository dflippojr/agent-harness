"""Synthetic regression coverage for guest reads, device scope, and stream revocation (#527)."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import PlainTextResponse

from harness import api as harness_api, apps
from harness.access import guest_forbidden, resolve_access
from harness.api import GLOBAL_TYPES
from harness.config import GuestAccess
from test_api import make_client as guest_client
from test_control_center import ORIGIN, make_client
from test_hub_claim import CLAIM, HUB_ORIGIN, _bearer, _claim, _host
from test_household import household
from test_phase7 import seed


OWNER_STATUS_PATHS = (
    "/memory", "/pairing-codes", "/runner-pairing-codes", "/runners", "/backends",
    "/gpu", "/resources", "/resources/diagnostics", "/models/status",
)


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


# The full surface x tick product for an app key; owner and device keys share the stream code, so each keeps one
# live-stream case and one replay case; a restored key is checked once per surface.
REVOCATION_CASES = (
    [("app", False, surface, tick) for surface in ("global", "session", "ticket")
     for tick in ("event", "keepalive", "replay") if (surface, tick) != ("global", "replay")]
    + [(kind, False, surface, tick) for kind in ("owner", "device")
       for surface, tick in (("global", "event"), ("ticket", "replay"))]
    + [("app", True, "global", "event"), ("device", True, "session", "event"), ("app", True, "ticket", "event")])


@pytest.mark.parametrize("kind,restore,surface,tick", REVOCATION_CASES)
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


# #787: the admin-surface streams (`/api/admin/v1/events`, `/api/admin/v1/sessions/{ref}/events`) run the Web handlers.
# Each test opens one through the real alias, so `require_admin` records the bearer key as it does in production,
# then consumes the stream the handler built with a short tick.
def _open_admin_stream(client, monkeypatch, path: str, headers: dict):
    """GET `path` through the app and return the event generator its handler built, unconsumed."""
    opened = []

    def capture(stream):
        opened.append(stream)
        return PlainTextResponse("")

    monkeypatch.setattr(harness_api, "_event_stream_response", capture)
    response = client.get(path, headers=headers)
    monkeypatch.undo()
    assert response.status_code == 200, response.text
    return opened[0]


def _short_tick(monkeypatch, before=None):
    """Make each stream wait return at once: the queued event, or a quiet interval, with the client still connected
    (the request the handler kept has already been answered). `before` runs first."""
    async def tick(awaitable, timeout):
        if before is not None:
            before()
        if isinstance(awaitable, asyncio.Future) or not hasattr(awaitable, "close"):
            return await awaitable
        task = asyncio.ensure_future(awaitable)
        await asyncio.sleep(0)
        if task.done():
            return task.result()
        task.cancel()
        raise asyncio.TimeoutError

    async def connected(_request):
        return False
    monkeypatch.setattr(harness_api.asyncio, "wait_for", tick)
    monkeypatch.setattr(Request, "is_disconnected", connected)


ADMIN_STREAMS = {
    "global": ("/api/admin/v1/events", "*"),
    "session": ("/api/admin/v1/sessions/owner00001/events?follow=true&after={last}", "owner00001"),
}


def _seeded_stream_path(m, surface: str) -> tuple[str, str]:
    """Seed owner00001 and return (path, bus subscription); a session stream starts after its persisted events."""
    seed(m.db, "owner00001", "scratch", "stream", [("status", {"status": "done"})])
    path, subscription = ADMIN_STREAMS[surface]
    return path.format(last=m.db.events("owner00001")[-1]["seq"]), subscription


@pytest.mark.parametrize("surface", sorted(ADMIN_STREAMS))
@pytest.mark.parametrize("when", ["before_wait", "during_wait", "keepalive"])
def test_revoking_an_owner_key_ends_its_admin_surface_stream(tmp_path, monkeypatch, surface, when):
    client, m = make_client(tmp_path)
    key, token = m.db.create_api_key("reader", "admin", "owner", origins=[ORIGIN])
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with client:
        path, subscription = _seeded_stream_path(m, surface)
        stream = _open_admin_stream(client, monkeypatch, path, headers)

        def revoke_during_wait():
            if not m.db.revoke_api_key(key["id"]):
                return  # revoked on an earlier tick, which should already have ended the stream
            if when == "during_wait":
                m.bus.emit("owner00001", "status", {"status": "done", "answer": "must not leak"})

        async def consume():
            assert await anext(stream) == ": connected\n\n"
            if when == "before_wait":
                revoked = await asyncio.to_thread(client.delete, f"/api/admin/v1/keys/{key['id']}")
                assert revoked.status_code == 204, revoked.text
                m.bus.emit("owner00001", "status", {"status": "done", "answer": "must not leak"})
                _short_tick(monkeypatch)
            else:
                _short_tick(monkeypatch, before=revoke_during_wait)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert not m.bus._subs[subscription]

        asyncio.run(consume())
        monkeypatch.undo()
        reconnect = path.replace("follow=true", "follow=false")
        assert client.get(reconnect, headers=headers).status_code == 401


@pytest.mark.parametrize("surface", sorted(ADMIN_STREAMS))
def test_releasing_the_hub_ends_the_admin_surface_stream_its_key_holds(tmp_path, monkeypatch, surface):
    client, m = household(tmp_path)
    with client:
        token, _, _ = _claim(client, m)
        headers = _bearer(token, HUB_ORIGIN)
        path, subscription = _seeded_stream_path(m, surface)
        stream = _open_admin_stream(client, monkeypatch, path, headers)

        async def consume():
            assert await anext(stream) == ": connected\n\n"
            released = await asyncio.to_thread(client.post, f"{CLAIM}/release", headers=_host(m),
                                               json={"confirm": True})
            assert released.status_code == 200, released.text
            m.bus.emit("owner00001", "status", {"status": "done", "answer": "must not leak"})
            _short_tick(monkeypatch)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert not m.bus._subs[subscription]

        asyncio.run(consume())
        monkeypatch.undo()
        assert client.get(path.replace("follow=true", "follow=false"), headers=headers).status_code == 401


@pytest.mark.parametrize("surface", sorted(ADMIN_STREAMS))
def test_ambient_owner_admin_stream_survives_revoking_an_unrelated_key(tmp_path, monkeypatch, surface):
    client, m = make_client(tmp_path)
    other, _ = m.db.create_api_key("other", "admin", "owner", origins=[ORIGIN])
    with client:
        path, _ = _seeded_stream_path(m, surface)
        stream = _open_admin_stream(client, monkeypatch, path, {})

        async def consume():
            assert await anext(stream) == ": connected\n\n"
            revoked = await asyncio.to_thread(client.delete, f"/api/admin/v1/keys/{other['id']}")
            assert revoked.status_code == 204, revoked.text
            _short_tick(monkeypatch)
            assert await anext(stream) == ": keepalive\n\n"
            m.bus.emit("owner00001", "status", {"status": "done", "answer": "still live"})
            assert "still live" in await anext(stream)
            await stream.aclose()

        asyncio.run(consume())
