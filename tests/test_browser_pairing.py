"""Issue #25: browser pairing, exact-origin CORS, and authenticated resumable SSE."""

from __future__ import annotations

import time

import pytest

from harness.apps import normalize_origin
from harness.llm import Completion
from test_phase6 import app_client, wait_for


ORIGIN = "https://control.example"
OTHER = "https://other.example"


def pair(client, origin=ORIGIN, name="browser"):
    approved = client.post("/pairing-codes", json={"name": name, "origin": origin, "scopes": ["sessions"]})
    assert approved.status_code == 201
    code = approved.json()["code"]
    response = client.post("/api/v1/pair", headers={"Origin": origin}, json={"code": code})
    assert response.status_code == 201
    return response.json()


@pytest.mark.parametrize("raw,expected", [
    ("https://EXAMPLE.com/", "https://example.com"),
    ("https://example.com:443", "https://example.com"),
    ("http://localhost:3000", "http://localhost:3000"),
    ("http://[::1]:8100", "http://[::1]:8100"),
])
def test_normalize_origin(raw, expected):
    assert normalize_origin(raw) == expected


@pytest.mark.parametrize("raw", ["null", "ftp://example.com", "http://example.com", "https://example.com/path",
                                  "https://user@example.com", "https://example.com?x=1", "example.com"])
def test_reject_non_origins(raw):
    with pytest.raises(ValueError):
        normalize_origin(raw)


def test_owner_approved_pairing_is_one_time_and_cors_is_exact(tmp_path):
    client, m = app_client(tmp_path, [Completion(content="hi"), Completion(content="hi again")])
    with client:
        approved = client.post("/pairing-codes", json={
            "name": "separate Agent Harness App", "origin": ORIGIN, "scopes": ["sessions"]})
        assert approved.headers["cache-control"] == "no-store"
        approved = approved.json()
        assert approved["code"].startswith("hp-")
        listed = client.get("/pairing-codes").json()[0]
        assert "code" not in listed
        assert "hash" not in listed
        assert listed["origin"] == ORIGIN
        stored_hash = m.db.conn.execute(
            "SELECT hash FROM pairing_codes WHERE id = ?", (approved["id"],)).fetchone()[0]
        assert stored_hash != approved["code"]

        preflight = client.options("/api/v1/pair", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == ORIGIN
        assert "credentials" not in " ".join(preflight.headers).lower()

        # A code approved for one exact origin cannot be redeemed from a suffix lookalike or without Origin.
        evil = f"{ORIGIN}.evil.example"
        assert client.post("/api/v1/pair", headers={"Origin": evil},
                           json={"code": approved["code"]}).status_code == 403
        # A non-browser client has no ambient browser authority, but pairing itself still requires Origin.
        assert client.post("/api/v1/pair", json={"code": approved["code"]}).status_code == 400

        redeemed = client.post("/api/v1/pair", headers={"Origin": ORIGIN},
                               json={"code": approved["code"]})
        assert redeemed.status_code == 201
        token, app = redeemed.json()["token"], redeemed.json()["app"]
        assert token.startswith("ha-")
        assert app["origins"] == [ORIGIN]
        assert redeemed.headers["cache-control"] == "no-store"
        # The code stops authorizing CORS as soon as it is spent.
        assert client.post("/api/v1/pair", headers={"Origin": ORIGIN},
                           json={"code": approved["code"]}).status_code == 403

        auth = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        api_preflight = client.options("/api/v1/sessions", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type"})
        assert api_preflight.status_code == 204
        created = client.post("/api/v1/sessions", headers=auth, json={"prompt": "hello"})
        assert created.status_code == 201
        assert created.headers["access-control-allow-origin"] == ORIGIN
        assert "access-control-allow-credentials" not in created.headers

        # Make OTHER a known origin, proving route auth checks this token's origin rather than the global allowlist.
        other = client.post("/pairing-codes", json={"name": "other", "origin": OTHER,
                                                    "scopes": ["sessions"]}).json()
        assert client.post("/api/v1/sessions",
                           headers={"Authorization": f"Bearer {token}", "Origin": OTHER},
                           json={"prompt": "spoof"}).status_code == 403
        assert client.post("/api/v1/pair", headers={"Origin": OTHER},
                           json={"code": other["code"]}).status_code == 201

        # Non-browser clients remain supported, while a cross-site request without bearer auth is refused.
        assert client.post("/api/v1/sessions", headers={"Authorization": f"Bearer {token}"},
                           json={"prompt": "server client"}).status_code == 201
        assert client.post("/api/v1/sessions", headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
                           json={"prompt": "csrf"}).status_code == 401


def test_stream_tickets_resume_expire_and_follow_key_revocation(tmp_path):
    client, m = app_client(tmp_path, [Completion(content="finished")])
    with client:
        paired = pair(client)
        token, kid = paired["token"], paired["app"]["id"]
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        sid = client.post("/api/v1/sessions", headers=headers, json={"prompt": "hello"}).json()["id"]
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")

        # Native EventSource has no Authorization header, so an unauthenticated stream is still rejected.
        bare = client.get(f"/api/v1/sessions/{sid}/events?follow=false", headers={"Origin": ORIGIN})
        assert bare.status_code == 401

        minted = client.post(f"/api/v1/sessions/{sid}/events/ticket", headers=headers)
        assert minted.status_code == 201
        assert minted.json()["ticket"].startswith("hs-")
        stored_ticket_hash = m.db.conn.execute("SELECT hash FROM stream_tickets").fetchone()[0]
        assert stored_ticket_hash != minted.json()["ticket"]
        assert minted.headers["cache-control"] == "no-store"
        ticket_url = minted.json()["events_url"] + "&follow=false"
        assert token not in ticket_url
        streamed = client.get(ticket_url, headers={"Origin": ORIGIN})
        assert streamed.status_code == 200
        assert "event: status" in streamed.text
        assert streamed.headers["access-control-allow-origin"] == ORIGIN
        assert streamed.headers["referrer-policy"] == "no-referrer"

        # EventSource reconnects can reuse the ticket briefly and resume from Last-Event-ID.
        seq = max(e["seq"] for e in m.db.events(sid))
        resumed = client.get(ticket_url, headers={"Origin": ORIGIN, "Last-Event-ID": str(seq)})
        assert resumed.status_code == 200
        assert "id: " not in resumed.text
        assert client.get(ticket_url, headers={"Origin": OTHER}).status_code == 401

        # After a long iOS suspension the client mints a fresh ticket, retaining its last sequence separately.
        m.db.conn.execute("UPDATE stream_tickets SET expires_at = ?", (time.time() - 1,))
        assert client.get(ticket_url, headers={"Origin": ORIGIN}).status_code == 401
        fresh = client.post(f"/api/v1/sessions/{sid}/events/ticket", headers=headers).json()["events_url"]
        assert client.get(fresh + "&follow=false", headers={"Origin": ORIGIN,
                          "Last-Event-ID": str(seq)}).status_code == 200

        # Revoking the parent app credential immediately invalidates outstanding stream tickets too.
        assert client.delete(f"/keys/{kid}").status_code == 204
        assert client.get(fresh + "&follow=false", headers={"Origin": ORIGIN}).status_code == 401
        assert client.post(f"/api/v1/sessions/{sid}/events/ticket", headers=headers).status_code == 403
        assert client.options("/api/v1/sessions", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type"}).status_code == 403


def test_expired_pairing_code_never_mints_a_key(tmp_path):
    client, m = app_client(tmp_path, [])
    with client:
        approved = client.post("/pairing-codes", json={"name": "late", "origin": ORIGIN,
                                                       "scopes": ["sessions"]}).json()
        m.db.conn.execute("UPDATE pairing_codes SET expires_at = ? WHERE id = ?",
                          (time.time() - 1, approved["id"]))
        assert client.post("/api/v1/pair", headers={"Origin": ORIGIN},
                           json={"code": approved["code"]}).status_code == 403
        assert not [key for key in client.get("/keys").json() if key["name"] == "late"]
