"""The Tailscale identity headers count only when tailscaled is the other end of the connection."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import local_owner, tailscale_peer
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager

from conftest import FAKE_NODE_IPS, FAKE_PEER_IP, FAKE_TAILSCALE_DIR, fake_tailscaled_check
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


def test_the_real_tailscaled_run_by_someone_else_is_not_the_service(tmp_path):
    """Any local user can start the installed tailscaled.exe in userspace mode as a proxy and pick its headers (#623)."""
    check = fake_tailscaled_check(owner=lambda client, server: 5151, service=lambda: 4242)
    client, m = make_client(tmp_path, [LOGIN], check)
    with client:
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN,
                                          "X-Forwarded-For": FAKE_PEER_IP}).status_code == 401
        assert client.post("/keys", json={"name": "k", "kind": "owner", "scopes": ["admin"]},
                           headers={"Tailscale-User-Login": LOGIN}).status_code == 401
        assert m.db.list_api_keys() == []
    assert check.cached(CLIENT) is False


def test_the_network_child_the_service_starts_is_tailscaled(tmp_path):
    """The service supervises a `tailscaled.exe /subproc` child, and that child owns Serve's socket (PIDs 7500 and
    10312 on the tower with tailscaled 1.102.4)."""
    spawned = []

    def child_of(pid, parent):
        spawned.append((pid, parent))
        return (pid, parent) == (10312, 7500)

    check = fake_tailscaled_check(owner=lambda client, server: 10312, service=lambda: 7500, child_of=child_of)
    client, _ = make_client(tmp_path, [LOGIN], check)
    with client:
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).json()["role"] == "owner"
    assert spawned == [(10312, 7500)]
    # Not the service's own child: the real tailscaled.exe started by some other process.
    assert not fake_tailscaled_check(owner=lambda client, server: 5151, service=lambda: 7500,
                                     child_of=child_of).verify(CLIENT, SERVER)


def test_a_missing_or_unreadable_service_ignores_the_headers(tmp_path):
    def missing():
        raise LookupError("service 'Tailscale' does not exist")

    for service in (missing, lambda: None):  # psutil gives no PID for a stopped service
        client, _ = make_client(tmp_path, [LOGIN], fake_tailscaled_check(service=service))
        with client:
            assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).status_code == 401


def test_tailscaled_forwarding_this_node_itself_is_not_the_owner(tmp_path):
    """`tailscale serve` names a local program that connects to this node's tailnet URL as the node's owner (#623)."""
    client, m = make_client(tmp_path, [LOGIN], fake_tailscaled_check())
    with client:
        for own in (*FAKE_NODE_IPS, "::ffff:100.64.0.1"):
            assert client.get("/me", headers={"Tailscale-User-Login": LOGIN, "X-Forwarded-For": own}).status_code == 401
        self_node = {"Tailscale-User-Login": LOGIN, "X-Forwarded-For": FAKE_NODE_IPS[0]}
        assert client.post("/keys", json={"name": "k", "kind": "owner", "scopes": ["admin"]},
                           headers=self_node).status_code == 401
        assert m.db.list_api_keys() == []
        # The local owner token still works for a program on this machine, and the forged name is not shown.
        me = client.get("/me", headers={"Tailscale-User-Login": LOGIN, "Tailscale-User-Name": "Self",
                                        "X-Forwarded-For": FAKE_NODE_IPS[0], local_owner.HEADER: m.local_owner_token})
        assert me.json()["role"] == "owner" and me.json().get("name") != "Self"


def test_tailscaled_forwarding_another_tailnet_node_is_accepted(tmp_path):
    client, _ = make_client(tmp_path, [LOGIN], fake_tailscaled_check())
    with client:
        for peer in (FAKE_PEER_IP, "fd7a:115c:a1e0::2"):
            me = client.get("/me", headers={"Tailscale-User-Login": LOGIN, "X-Forwarded-For": peer})
            assert me.json()["role"] == "owner"


def test_more_than_one_forwarded_address_is_refused(tmp_path):
    client, _ = make_client(tmp_path, [LOGIN], fake_tailscaled_check())
    with client:
        client.serve_forwarded = False
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).status_code == 401  # missing
        client.serve_forwarded = True
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN,
                                          "X-Forwarded-For": f"{FAKE_PEER_IP}, 100.64.0.3"}).status_code == 401
        assert client.get("/me", headers=[("Tailscale-User-Login", LOGIN), ("X-Forwarded-For", FAKE_PEER_IP),
                                          ("X-Forwarded-For", "100.64.0.3")]).status_code == 401
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN,
                                          "X-Forwarded-For": "nonsense"}).status_code == 401


def test_a_forwarded_address_from_an_unverified_peer_changes_nothing(forged):
    client, m = forged
    for source in (FAKE_PEER_IP, FAKE_NODE_IPS[0]):
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN, "X-Forwarded-For": source}).status_code == 401
    assert m.tailscale_peer.lookups == 1  # the connection's verdict, cached; the node's addresses were never read
    assert not m.tailscale_peer.own_ips_cached()


def test_unknown_node_addresses_refuse_every_forwarded_request(tmp_path):
    def unavailable():
        raise FileNotFoundError("tailscale.exe not found")

    client, _ = make_client(tmp_path, [LOGIN], fake_tailscaled_check(self_ips=unavailable))
    with client:
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN,
                                          "X-Forwarded-For": FAKE_PEER_IP}).status_code == 401


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


def test_the_forwarded_address_must_be_one_address_of_another_node():
    check = fake_tailscaled_check()
    assert check.from_other_node([FAKE_PEER_IP.encode()])
    assert not check.from_other_node([])  # missing
    assert not check.from_other_node([b"100.64.0.1"])
    assert not check.from_other_node([b"100.64.0.2", b"100.64.0.3"])
    assert not check.from_other_node([b"100.64.0.2,100.64.0.3"])
    assert not check.from_other_node([b""])


def test_the_node_addresses_are_cached_and_a_failure_is_retried():
    clock, calls = Clock(), []

    def ips():
        calls.append(clock.now)
        if len(calls) == 1:
            raise OSError("tailscaled not running")
        return FAKE_NODE_IPS

    check = fake_tailscaled_check(self_ips=ips, clock=clock, self_ips_ttl=60)
    assert not check.from_other_node([FAKE_PEER_IP.encode()])
    assert check.from_other_node([FAKE_PEER_IP.encode()]) and check.from_other_node([FAKE_PEER_IP.encode()])
    assert len(calls) == 2 and check.own_ips_cached()
    clock.now += 60
    assert not check.own_ips_cached()
    assert check.from_other_node([FAKE_PEER_IP.encode()]) and len(calls) == 3


def test_a_changed_node_address_is_refused_before_the_cache_expires():
    """After a profile switch the cached `tailscale ip` addresses are stale, but the adapter already has the new one."""
    current = {"addrs": ("127.0.0.1", *FAKE_NODE_IPS)}
    check = fake_tailscaled_check(local_ips=lambda: current["addrs"])
    assert check.from_other_node([FAKE_PEER_IP.encode()])
    current["addrs"] = ("127.0.0.1", "fe80::1%12", FAKE_PEER_IP)  # this node now holds the address
    assert check.own_ips_cached()
    assert not check.from_other_node([FAKE_PEER_IP.encode()])


def test_unreadable_interfaces_refuse_the_request():
    def broken():
        raise OSError("no interface table")

    assert not fake_tailscaled_check(local_ips=broken).from_other_node([FAKE_PEER_IP.encode()])


def test_interface_ips_lists_every_address_without_a_zone(monkeypatch):
    import socket

    class Addr:
        def __init__(self, family, address):
            self.family, self.address = family, address

    table = {"Tailscale": [Addr(socket.AF_INET, "100.75.184.105"), Addr(socket.AF_INET6, "fe80::1%12")],
             "Ethernet": [Addr(-1, "aa-bb-cc-dd-ee-ff"), Addr(socket.AF_INET, "192.168.1.5")]}
    monkeypatch.setattr(tailscale_peer.psutil, "net_if_addrs", lambda: table)
    assert tailscale_peer.interface_ips() == {"100.75.184.105", "fe80::1", "192.168.1.5"}


def test_node_ips_reads_tailscale_ip_from_the_install_directory(monkeypatch, tmp_path):
    (tmp_path / "tailscale.exe").write_bytes(b"")
    seen = []

    def run(args, **kw):
        seen.append(args)
        return type("Done", (), {"stdout": "100.75.184.105\nfd7a:115c:a1e0::8c31:b86a\n"})()

    monkeypatch.setattr(tailscale_peer.subprocess, "run", run)
    assert tailscale_peer.node_ips([tmp_path]) == {"100.75.184.105", "fd7a:115c:a1e0::8c31:b86a"}
    assert seen == [[str(tmp_path / "tailscale.exe"), "ip"]]
    with pytest.raises(FileNotFoundError):
        tailscale_peer.node_ips([tmp_path / "missing"])


def test_strip_identity_removes_every_tailscale_header():
    scope = {"headers": [(b"tailscale-user-login", b"x"), (b"Tailscale-User-Name", b"y"),
                         (b"tailscale-funnel-request", b"?1"), (b"host", b"127.0.0.1")]}
    assert tailscale_peer.has_identity(scope)
    tailscale_peer.strip_identity(scope)
    assert scope["headers"] == [(b"host", b"127.0.0.1")]
    assert not tailscale_peer.has_identity(scope)


def test_service_pid_asks_for_the_tailscale_service(monkeypatch):
    asked = []

    class Service:
        def pid(self):
            return 7500

    monkeypatch.setattr(tailscale_peer.psutil, "win_service_get", lambda name: asked.append(name) or Service(),
                        raising=False)
    assert tailscale_peer.service_pid() == 7500
    assert asked == ["Tailscale"]


def test_started_by_needs_the_parent_pid_and_a_newer_child(monkeypatch):
    procs = {10312: (7500, 200.0), 5151: (7500, 50.0), 6000: (1252, 300.0), 7500: (1252, 100.0)}

    class Proc:
        def __init__(self, pid):
            self.parent, self.created = procs[pid]

        def ppid(self):
            return self.parent

        def create_time(self):
            return self.created

    monkeypatch.setattr(tailscale_peer.psutil, "Process", Proc)
    assert tailscale_peer.started_by(10312, 7500)
    assert not tailscale_peer.started_by(5151, 7500)  # older than the service: its parent PID was recycled
    assert not tailscale_peer.started_by(6000, 7500)


def test_install_dirs_follow_program_files(monkeypatch):
    monkeypatch.setenv("ProgramFiles", "D:/Apps")
    monkeypatch.delenv("ProgramW6432", raising=False)
    assert tailscale_peer.install_dirs() == [Path("D:/Apps") / "Tailscale"]


def test_the_daemon_keeps_the_real_tcp_peer_as_the_client(monkeypatch, tmp_path):
    """uvicorn would take the client from X-Forwarded-For on loopback; the peer check needs the real socket."""
    import harness.__main__ as daemon

    seen = {}
    monkeypatch.setattr(daemon.config_mod, "load", lambda *a: make_cfg(tmp_path))
    monkeypatch.setattr(daemon, "create_app", lambda m: "app")
    monkeypatch.setattr(daemon, "Manager", lambda cfg: None)
    monkeypatch.setattr(daemon.uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr("sys.argv", ["harness"])
    daemon.main()
    assert seen["proxy_headers"] is False
