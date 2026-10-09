"""The Tailscale identity headers count only when tailscaled is the other end of the connection."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import local_owner, tailscale_peer
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager

from conftest import FAKE_TAILSCALE_DIR, fake_tailscaled_check
from test_daemon import Script, make_cfg

LOGIN = "me@example.com"
CLIENT = ("127.0.0.1", 51234)
SERVER = ("127.0.0.1", 8100)
TAILSCALED = str(FAKE_TAILSCALE_DIR / "tailscaled.exe")


def not_tailscaled(**overrides):
    """A connection table whose peer is some other local program."""
    return fake_tailscaled_check(exe=lambda pid: "C:/Users/someone/tool.exe", **overrides)


def make_client(tmp_path, allowed_logins, check):
    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = allowed_logins
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    m.tailscale_peer = check
    client = TestClient(create_app(m), client=CLIENT)
    client.local_owner = False  # this client sends only the headers each test gives it
    return client, m


@pytest.fixture
def forged(tmp_path):
    client, m = make_client(tmp_path, [LOGIN], not_tailscaled())
    with client:
        yield client, m


def test_a_login_header_from_another_local_program_is_ignored(forged):
    client, m = forged
    owner = {"Tailscale-User-Login": LOGIN}
    assert client.get("/me", headers=owner).status_code == 401
    assert client.post("/keys", json={"name": "k", "kind": "owner", "scopes": ["admin"]},
                       headers=owner).status_code == 401
    assert m.db.list_api_keys() == []


def test_the_same_login_from_tailscaled_is_the_owner(tmp_path):
    seen = []

    def owner(client, server):
        seen.append((client, server))
        return 4242

    client, _ = make_client(tmp_path, [LOGIN], fake_tailscaled_check(owner=owner))
    with client:
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).json()["role"] == "owner"
    assert seen and seen[0][0] == CLIENT


def test_open_owner_mode_cannot_be_reached_with_a_forged_login(tmp_path):
    client, m = make_client(tmp_path, [], not_tailscaled())
    with client:
        for login in (LOGIN, "anyone@example.com"):
            assert client.get("/me", headers={"Tailscale-User-Login": login}).status_code == 401
            assert client.post("/keys", json={"name": "k", "kind": "owner", "scopes": ["admin"]},
                               headers={"Tailscale-User-Login": login}).status_code == 401
        assert m.db.list_api_keys() == []


def test_a_failed_lookup_ignores_the_headers(tmp_path):
    def broken(client, server):
        raise PermissionError("connection table unavailable")

    check = fake_tailscaled_check(owner=broken)
    client, _ = make_client(tmp_path, [LOGIN], check)
    with client:
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).status_code == 401
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).status_code == 401
    assert check.lookups == 2  # a failure is not cached


def test_ignored_headers_are_removed_before_any_route_reads_them(forged):
    client, m = forged
    me = client.get("/me", headers={"Tailscale-User-Login": LOGIN, "Tailscale-User-Name": "Forged Name",
                                    local_owner.HEADER: m.local_owner_token}).json()
    assert me["role"] == "owner"
    assert me.get("name") != "Forged Name"
    assert me.get("login") in (None, "")


def test_the_local_owner_token_and_api_tokens_still_work_beside_ignored_headers(forged):
    client, m = forged
    header = {"Tailscale-User-Login": "someone@example.com"}
    created = client.post("/keys", json={"name": "o", "kind": "owner", "scopes": ["admin"]},
                          headers={**header, local_owner.HEADER: m.local_owner_token})
    assert created.status_code == 201
    assert client.get("/api/admin/v1/keys", headers={
        **header, "Authorization": f"Bearer {created.json()['key']}"}).status_code == 200


def test_unsupported_platforms_ignore_the_headers_unless_configured(tmp_path):
    client, m = make_client(tmp_path, [LOGIN], fake_tailscaled_check(platform_ok=lambda: False))
    with client:
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).status_code == 401
        m.cfg.trust_unverified_identity_headers = True
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).json()["role"] == "owner"
    assert m.tailscale_peer.lookups == 0


# --- the check itself -------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_process_name_alone_is_not_enough():
    dirs = [FAKE_TAILSCALE_DIR]
    assert tailscale_peer.is_tailscaled(TAILSCALED, dirs)
    assert not tailscale_peer.is_tailscaled("C:/Users/someone/tailscaled.exe", dirs)
    assert not tailscale_peer.is_tailscaled("C:/Program Files/TailscaleEvil/tailscaled.exe", dirs)
    assert not tailscale_peer.is_tailscaled("C:/Program Files/Tailscale/sub/tailscaled.exe", dirs)
    assert not tailscale_peer.is_tailscaled(str(FAKE_TAILSCALE_DIR / "tailscale.exe"), dirs)
    assert not tailscale_peer.is_tailscaled("", dirs)
    assert not tailscale_peer.is_tailscaled(TAILSCALED, [])


def test_no_owning_process_is_not_tailscaled():
    assert not fake_tailscaled_check(owner=lambda client, server: None).verify(CLIENT, SERVER)
    assert not fake_tailscaled_check().verify(None, SERVER)


def test_a_verdict_is_cached_per_connection_until_it_expires():
    clock = Clock()
    check = fake_tailscaled_check(clock=clock, ttl=5)
    assert check.verify(CLIENT, SERVER) and check.verify(CLIENT, SERVER)
    assert check.lookups == 1
    assert check.verify(("127.0.0.1", 51235), SERVER)
    assert check.lookups == 2
    clock.now += 5
    assert check.verify(CLIENT, SERVER)
    assert check.lookups == 3


def test_a_refusal_is_cached_too():
    check = not_tailscaled()
    assert not check.verify(CLIENT, SERVER) and not check.verify(CLIENT, SERVER)
    assert check.lookups == 1


def test_the_cache_is_bounded_and_drops_the_least_recent():
    check = fake_tailscaled_check(size=2)
    a, b, c = ("127.0.0.1", 1), ("127.0.0.1", 2), ("127.0.0.1", 3)
    check.verify(a, SERVER)
    check.verify(b, SERVER)
    check.verify(a, SERVER)  # a is now the most recent
    check.verify(c, SERVER)  # evicts b
    assert check.cached(a) is True and check.cached(c) is True
    assert check.cached(b) is None
    assert check.lookups == 3


def test_the_connection_table_matches_the_peer_socket_not_the_daemon_socket(monkeypatch):
    class Addr:
        def __init__(self, ip, port):
            self.ip, self.port = ip, port

    class Conn:
        def __init__(self, laddr, raddr, pid):
            self.laddr, self.raddr, self.pid = Addr(*laddr), Addr(*raddr), pid

    table = [
        Conn(SERVER, CLIENT, 1),            # the daemon's own end of the connection
        Conn(("127.0.0.1", 51234), ("127.0.0.1", 9999), 2),  # same port, another destination
        Conn(("::ffff:127.0.0.1", 51234), ("::ffff:127.0.0.1", 8100), 3),
    ]
    monkeypatch.setattr(tailscale_peer.psutil, "net_connections", lambda kind: table)
    assert tailscale_peer.connection_owner(CLIENT, SERVER) == 3
    assert tailscale_peer.connection_owner(("::1", 51234), SERVER) is None
    assert tailscale_peer.connection_owner(("testclient", 50000), SERVER) is None


def test_strip_identity_removes_every_tailscale_header():
    scope = {"headers": [(b"tailscale-user-login", b"x"), (b"Tailscale-User-Name", b"y"),
                         (b"tailscale-funnel-request", b"?1"), (b"host", b"127.0.0.1")]}
    assert tailscale_peer.has_identity(scope)
    tailscale_peer.strip_identity(scope)
    assert scope["headers"] == [(b"host", b"127.0.0.1")]
    assert not tailscale_peer.has_identity(scope)


def test_install_dirs_follow_program_files(monkeypatch):
    monkeypatch.setenv("ProgramFiles", "D:/Apps")
    monkeypatch.delenv("ProgramW6432", raising=False)
    assert tailscale_peer.install_dirs() == [Path("D:/Apps") / "Tailscale"]
