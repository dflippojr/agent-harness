"""#909: the App API proxy lets one dedicated Docker network reach the loopback daemon's /api/v1, and nothing else.

Loopback stands in for the Docker bridge: the proxy listens on 127.0.0.1 (the "gateway"), admits peers in
127.0.0.0/29, and the client picks its source address (127.0.0.2 is on the network, 127.0.0.9 is not). Linux and
Windows both route all of 127.0.0.0/8 to loopback. On Linux the listener is also bound to the `lo` device, as it is
bound to the bridge on the tower; elsewhere SO_BINDTODEVICE doesn't exist and the proxy refuses a device.
The last tests put the real daemon behind the proxy with `trust_unverified_identity_headers` on, as on the tower."""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import ipaddress
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("harness_app_api_proxy", ROOT / "ops" / "app-api" / "harness_app_api_proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)

NETWORK = ipaddress.IPv4Network("127.0.0.0/29")
GATEWAY = ipaddress.IPv4Address("127.0.0.1")
MEMBER = "127.0.0.2"
OUTSIDER = "127.0.0.9"
DEVICE = "lo" if hasattr(socket, "SO_BINDTODEVICE") else None
FORGED = {"Tailscale-User-Login": "owner@example.com", "Tailscale-User-Name": "Owner",
          "X-Agent-Harness-Local-Token": "stolen", "X-Agent-Harness-Hub-Approval": "stolen",
          "X-Forwarded-For": "100.64.0.2", "Forwarded": "for=100.64.0.2", "Cookie": "harness_session=x",
          "Origin": "https://evil.example", "X-Api-Key": "k"}


class Upstream:
    """A stand-in daemon on 127.0.0.1 that records what reached it."""

    def __init__(self):
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": [(k.lower(), v) for k, v in self.headers.items()],
                                       "body": self.rfile.read(length) if length else b"",
                                       "peer": self.client_address[0]})
                if self.path.startswith("/api/v1/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for event in (b"data: one\n\n", b"data: two\n\n"):
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                    self.wfile.write(b"0\r\n\r\n")
                    return
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "harness_session=secret")
                self.send_header("X-Next-Before", "42")
                self.send_header("X-Internal", "1")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _any

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def start_proxy(upstream_port: int, device: str | None = DEVICE):
    server = proxy.Server(str(GATEWAY), 0, proxy.Peers(NETWORK, GATEWAY), upstream_port, device)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def chain():
    upstream = Upstream()
    server = start_proxy(upstream.port)
    yield upstream, server.server_address[1]
    server.shutdown()
    server.server_close()
    upstream.close()


def request(port, method, target, headers=None, body=None, source=MEMBER):
    conn = http.client.HTTPConnection(str(GATEWAY), port, timeout=10, source_address=(source, 0))
    try:
        conn.request(method, target, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


@pytest.mark.parametrize("target", [
    "/api/v1", "/api/v1/backends", "/api/v1/projects", "/api/v1/sessions", "/api/v1/sessions/s-1/messages",
    "/api/v1/sessions/s-1/tool_calls?status=pending", "/api/v1/end-users/a%40b.example/logins/claude",
    "/api/v1/sessions/s-1/events?ticket=t", "/api/v1/models/status",
])
def test_app_api_paths_are_forwarded(target):
    assert proxy.path_allowed(target)


@pytest.mark.parametrize("target", [
    "/", "/me", "/keys", "/health", "/metrics", "/v1/chat/completions", "/runners/r1/poll", "/api/admin/v1",
    "/api/admin/v1/keys", "/api/v1x", "/api/v10/backends", "/api", "/api/v1/", "/api/v1//backends",
    "/api/v1/../admin/v1/keys", "/api/v1/./backends", "/api/v1/%2e%2e/keys", "/api/v1/%2E%2E/keys",
    "/api/v1/sessions%2f..%2f..%2fkeys", "/api/v1/a%5cb", "/api/v1/a%00", "/api/v1/a\\b",
    "http://127.0.0.1:8100/api/v1", "127.0.0.1:8100", "*", "/api/v1#x", "/api/v1/backends?x=<script>",
    "/api/v1/pair", "/api/v1/pair/requests", "/api/v1/pair/requests/r1/token",
    # The daemon decodes the path before routing: encoded letters still spell the pairing routes.
    "/api/v1/%70air/requests", "/api/v1/p%61ir", "/api/v1/%70%61%69%72", "/api/v1/%70%61ir/requests/r1/token",
])
def test_everything_else_is_refused(target):
    assert not proxy.path_allowed(target)


@pytest.mark.parametrize("target", [
    "/api/v1/runner-pair", "/api/v1/runner-pair/", "/api/v1/runner-pair/x", "/api/v1/runner-pair?code=1",
    # Encoded letters still spell native runner pairing once the daemon decodes the path.
    "/api/v1/%72unner-pair", "/api/v1/runner%2Dpair", "/api/v1/runner-p%61ir", "/api/v1/%72%75%6e%6e%65%72-pair",
])
def test_native_runner_pairing_is_refused(target):
    """Codex round 1: `/api/v1/runner-pair` redeems an owner-approved Mac code for an owner_token."""
    assert not proxy.path_allowed(target)


def test_peers_are_the_network_without_its_gateway_and_edges():
    peers = proxy.Peers(ipaddress.IPv4Network("192.168.240.0/29"), ipaddress.IPv4Address("192.168.240.1"))
    assert peers.admits("192.168.240.2") and peers.admits("192.168.240.6") and peers.admits("::ffff:192.168.240.3")
    for host in ("192.168.240.1", "192.168.240.0", "192.168.240.7", "192.168.240.8", "172.17.0.2", "127.0.0.1",
                 "::1", "not-an-ip", ""):
        assert not peers.admits(host), host


def test_the_command_line_must_name_one_dedicated_network():
    good = ["--network", "192.168.240.0/29", "--listen", "192.168.240.1", "--device", "harness-api0"]
    args = proxy.parse_args(good)
    assert (str(args.network), str(args.listen), args.device, args.port, args.upstream_port) == (
        "192.168.240.0/29", "192.168.240.1", "harness-api0", 8100, 8100)
    for bad in (["--network", "10.0.0.0/8", "--listen", "10.0.0.1", "--device", "x"],
                ["--network", "127.0.0.0/29", "--listen", "127.0.0.1", "--device", "lo"],
                ["--network", "192.168.240.0/29", "--listen", "192.168.241.1", "--device", "x"],
                ["--network", "192.168.240.0/29", "--listen", "192.168.240.0", "--device", "x"],
                ["--network", "192.168.240.1/29", "--listen", "192.168.240.1", "--device", "x"],
                ["--network", "192.168.240.0/29", "--listen", "192.168.240.1", "--device", "a-very-long-name0"],
                ["--network", "192.168.240.0/29", "--listen", "192.168.240.1"]):
        with pytest.raises(SystemExit):
            proxy.parse_args(bad)


def test_the_listener_is_bound_to_the_bridge_device():
    """A device that doesn't exist, or a platform without SO_BINDTODEVICE, is a failure to start, never an open
    listener."""
    with pytest.raises(OSError):
        proxy.Server(str(GATEWAY), 0, proxy.Peers(NETWORK, GATEWAY), 1, "no-such-dev0")
    if DEVICE is None:
        with pytest.raises(OSError, match="needs Linux"):
            proxy.Server(str(GATEWAY), 0, proxy.Peers(NETWORK, GATEWAY), 1, "lo")
        return
    server = proxy.Server(str(GATEWAY), 0, proxy.Peers(NETWORK, GATEWAY), 1, DEVICE)
    try:
        bound = server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16).rstrip(b"\0")
        assert bound == DEVICE.encode()
    finally:
        server.server_close()


def test_only_app_headers_reach_the_daemon_and_host_is_its_loopback_address(chain):
    upstream, port = chain
    status, headers, body = request(port, "POST", "/api/v1/sessions", body=b'{"prompt":"hi"}', headers={
        "Authorization": "Bearer ha-app", "Content-Type": "application/json", "Accept": "application/json",
        "Idempotency-Key": "k1", "X-Agent-Harness-Client": "planner/1", "User-Agent": "Python-urllib/3.12",
        "Host": "192.168.240.1:8100", **FORGED})
    assert (status, body) == (200, b'{"ok":true}')
    [seen] = upstream.requests
    assert (seen["method"], seen["path"], seen["body"]) == ("POST", "/api/v1/sessions", b'{"prompt":"hi"}')
    assert seen["peer"] == "127.0.0.1"
    names = {name for name, _ in seen["headers"]}
    assert names == {"host", "authorization", "content-type", "accept", "idempotency-key", "x-agent-harness-client",
                     "user-agent", "content-length", "connection"}
    assert dict(seen["headers"])["host"] == f"127.0.0.1:{upstream.port}"
    assert dict(seen["headers"])["authorization"] == "Bearer ha-app"
    # Response headers: an allowlist, never a cookie.
    assert headers["x-next-before"] == "42" and headers["content-type"] == "application/json"
    assert "set-cookie" not in headers and "x-internal" not in headers


def test_a_request_with_only_forged_identity_reaches_the_daemon_with_none(chain):
    upstream, port = chain
    status, _, _ = request(port, "GET", "/api/v1/backends", headers=FORGED)
    assert status == 200
    names = {name for name, _ in upstream.requests[0]["headers"]}
    assert names == {"host", "connection"}


def test_refused_paths_and_methods_never_reach_the_daemon(chain):
    upstream, port = chain
    for method, target, expected in (("GET", "/me", 404), ("GET", "/api/admin/v1/keys", 404),
                                     ("POST", "/keys", 404), ("GET", "/v1/models", 404),
                                     ("POST", "/api/v1/pair/requests", 404), ("GET", "/api/v1/%2e%2e/keys", 404),
                                     ("OPTIONS", "/api/v1/sessions", 405), ("TRACE", "/api/v1", 501)):
        status, _, _ = request(port, method, target, headers={"Authorization": "Bearer owner-admin"})
        assert status == expected, (method, target)
    assert upstream.requests == []


def test_bodies_need_a_length_within_the_cap(chain, monkeypatch):
    upstream, port = chain
    conn = http.client.HTTPConnection(str(GATEWAY), port, timeout=10, source_address=(MEMBER, 0))
    conn.putrequest("POST", "/api/v1/sessions")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"2\r\n{}\r\n0\r\n\r\n")
    assert conn.getresponse().status == 411
    conn.close()
    monkeypatch.setattr(proxy, "MAX_BODY", 4)
    assert request(port, "POST", "/api/v1/sessions", body=b"12345")[0] == 413
    status, _, _ = request(port, "POST", "/api/v1/sessions", headers={"Content-Length": "x"}, body=None)
    assert status == 400
    assert upstream.requests == []


def test_a_streamed_response_passes_through(chain):
    _, port = chain
    status, headers, body = request(port, "GET", "/api/v1/stream/events")
    assert status == 200 and headers["content-type"] == "text/event-stream"
    assert body == b"data: one\n\ndata: two\n\n"


def test_peers_off_the_network_and_the_gateway_are_cut_off_before_a_request_is_read(chain):
    upstream, port = chain
    for source in (OUTSIDER, str(GATEWAY)):
        with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            request(port, "GET", "/api/v1", source=source)
    assert upstream.requests == []
    assert request(port, "GET", "/api/v1")[0] == 200


def test_runner_pairing_never_reaches_the_daemon_even_with_an_app_token(chain):
    upstream, port = chain
    app = {"Authorization": "Bearer ha-app", "Content-Type": "application/json"}
    for target in ("/api/v1/runner-pair", "/api/v1/%72unner-pair", "/api/v1/runner-p%61ir"):
        status, _, body = request(port, "POST", target, headers=app, body=b'{"code":"ah909-unknown-code"}')
        assert (status, body) == (404, b'{"detail":"not available through the App API proxy"}'), target
    assert upstream.requests == []


def test_a_repeated_content_length_or_forwarded_header_is_refused(chain):
    """Two values for one header could be read differently by the proxy and the daemon: refuse them all."""
    upstream, port = chain
    for name, values in (("Content-Length", ("2", "2")), ("Content-Length", ("2", "3")),
                         ("Authorization", ("Bearer ha-app", "Bearer ha-other")),
                         ("Content-Type", ("application/json", "text/plain"))):
        conn = http.client.HTTPConnection(str(GATEWAY), port, timeout=10, source_address=(MEMBER, 0))
        try:
            conn.putrequest("POST", "/api/v1/sessions")
            for value in values:
                conn.putheader(name, value)
            if name != "Content-Length":
                conn.putheader("Content-Length", "2")
            conn.endheaders(b"{}")
            assert conn.getresponse().status == 400, (name, values)
        finally:
            conn.close()
    assert upstream.requests == []


def test_handler_slots_are_released_after_every_kind_of_request(monkeypatch):
    """With one slot, a held connection turns the next one away. Once it closes, and after each forwarded, refused,
    malformed or failed request, the slot is free again."""
    from waits import scaled

    upstream = Upstream()
    monkeypatch.setattr(proxy, "MAX_CONNECTIONS", 1)
    server = start_proxy(upstream.port)
    port = server.server_address[1]

    def free_slots(expected):
        deadline = time.monotonic() + scaled(10)
        while server.slots._value != expected and time.monotonic() < deadline:  # noqa: SLF001 - the slot count
            time.sleep(0.01)
        return server.slots._value  # noqa: SLF001

    try:
        held = socket.create_connection((str(GATEWAY), port), timeout=10, source_address=(MEMBER, 0))
        held.sendall(b"GET /api/v1 HTTP/1.0\r\n")  # the handler waits for the rest of the request in its slot
        assert free_slots(0) == 0
        with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            request(port, "GET", "/api/v1")
        held.close()
        assert free_slots(1) == 1
        for method, target, headers, expected in (
                ("GET", "/api/v1", {}, 200), ("GET", "/api/v1/backends", {}, 200), ("GET", "/keys", {}, 404),
                ("OPTIONS", "/api/v1", {}, 405), ("POST", "/api/v1/sessions", {"Content-Length": "x"}, 400),
                ("POST", "/api/v1/pair/requests", {}, 404)):
            assert request(port, method, target, headers=headers)[0] == expected, (method, target)
            assert free_slots(1) == 1, (method, target)
        upstream.close()  # the daemon goes away: a 502 frees its slot too
        assert request(port, "GET", "/api/v1")[0] == 502
        assert free_slots(1) == 1
        # The held request line goes upstream once its client closes (end of headers); refusals never do.
        assert [r["path"] for r in upstream.requests] == ["/api/v1", "/api/v1", "/api/v1/backends"]
    finally:
        server.shutdown()
        server.server_close()


def test_a_daemon_that_is_down_is_a_502():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    server = start_proxy(dead)
    try:
        assert request(server.server_address[1], "GET", "/api/v1")[0] == 502
    finally:
        server.shutdown()
        server.server_close()


# The real daemon behind the proxy, configured as the tower is: it trusts unverified Tailscale identity headers.

LOGIN = "owner@example.com"


@pytest.fixture
def daemon(tmp_path):
    import httpx
    import uvicorn

    from harness import local_owner, tailscale_peer
    from harness.api import create_app
    from harness.llm import Completion
    from harness.manager import Manager

    from test_daemon import Script, make_cfg
    from waits import scaled

    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = [LOGIN]
    cfg.trust_unverified_identity_headers = True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        cfg.port = probe.getsockname()[1]
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    # As on the Linux tower: no tailscaled check, so trust_unverified_identity_headers decides.
    m.tailscale_peer = tailscale_peer.PeerCheck(platform_ok=lambda: False)
    server = uvicorn.Server(uvicorn.Config(create_app(m), host="127.0.0.1", port=cfg.port, log_level="warning",
                                           proxy_headers=False, forwarded_allow_ips=""))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    front = start_proxy(cfg.port)
    try:
        deadline = time.monotonic() + scaled(10)
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        direct = f"http://127.0.0.1:{cfg.port}"
        local = {local_owner.HEADER: local_owner.read_token(cfg.data_dir)}
        app = httpx.post(f"{direct}/keys", headers=local, timeout=10,
                         json={"name": "planner", "kind": "app", "scopes": ["sessions"]}).json()["key"]
        owner = httpx.post(f"{direct}/keys", headers=local, timeout=10,
                           json={"name": "o", "kind": "owner", "scopes": ["admin"]}).json()["key"]
        yield {"direct": direct, "port": front.server_address[1], "local": local, "app": app, "owner": owner}
    finally:
        front.shutdown()
        front.server_close()
        server.should_exit = True
        thread.join(timeout=10)


def test_an_app_discovers_the_daemon_through_the_proxy_with_its_token(daemon):
    """The planner's discover(): GET /api/v1 without a token, then /backends and /projects with its App token."""
    import json

    status, _, body = request(daemon["port"], "GET", "/api/v1", headers={"Accept": "application/json"})
    assert status == 200 and json.loads(body)["server"] == "agent-harness"
    bearer = {"Authorization": f"Bearer {daemon['app']}", "Accept": "application/json"}
    status, _, body = request(daemon["port"], "GET", "/api/v1/backends", headers=bearer)
    assert status == 200 and isinstance(json.loads(body), list)
    assert request(daemon["port"], "GET", "/api/v1/projects", headers=bearer)[0] == 200
    assert request(daemon["port"], "GET", "/api/v1/backends")[0] == 401


def test_forged_identity_and_the_local_owner_token_never_make_a_proxied_request_the_owner(daemon):
    """The same request that makes a direct loopback caller the owner gets nothing through the proxy."""
    import httpx

    tailnet = {"Tailscale-User-Login": LOGIN}
    for headers in (tailnet, daemon["local"], {**tailnet, **daemon["local"]}):
        direct = httpx.get(f"{daemon['direct']}/api/v1/me", headers=headers, timeout=10)
        assert direct.status_code == 200, headers
        status, _, _ = request(daemon["port"], "GET", "/api/v1/me", headers=headers)
        assert status == 401, headers
        assert request(daemon["port"], "GET", "/api/v1/backends", headers=headers)[0] == 401


def test_the_owner_admin_token_reaches_no_owner_route_through_the_proxy(daemon):
    import httpx

    admin = {"Authorization": f"Bearer {daemon['owner']}"}
    assert httpx.get(f"{daemon['direct']}/api/admin/v1/keys", headers=admin, timeout=10).status_code == 200
    for target in ("/api/admin/v1/keys", "/keys", "/me", "/settings"):
        assert request(daemon["port"], "GET", target, headers=admin)[0] == 404, target


def test_native_runner_pairing_is_reached_directly_but_never_through_the_proxy(daemon):
    """Directly, the daemon answers `/api/v1/runner-pair` (400 for a code it doesn't know). Through the proxy, with
    the App's own token, it is the proxy's 404 for every spelling."""
    import httpx

    app = {"Authorization": f"Bearer {daemon['app']}", "Content-Type": "application/json"}
    direct = httpx.post(f"{daemon['direct']}/api/v1/runner-pair", headers=app, json={"code": "ah909-unknown-code"},
                        timeout=10)
    assert (direct.status_code, direct.json()["detail"]) == (400, "invalid runner pairing code")
    for target in ("/api/v1/runner-pair", "/api/v1/%72unner-pair", "/api/v1/runner-p%61ir"):
        status, _, body = request(daemon["port"], "POST", target, headers=app, body=b'{"code":"ah909-unknown-code"}')
        assert (status, body) == (404, b'{"detail":"not available through the App API proxy"}'), target


def test_no_pairing_or_runner_route_of_the_daemon_passes_the_proxy(tmp_path):
    """The class, not one route: every route the daemon registers whose path names pairing or runners is refused,
    as written and with its first letter percent-encoded. A new one under /api/v1 fails here until it is refused."""
    import re

    from harness.api import create_app
    from harness.llm import Completion
    from harness.manager import Manager

    from test_daemon import Script, make_cfg

    app = create_app(Manager(make_cfg(tmp_path), chat=Script([Completion(content="hi")])))
    paths = sorted({re.sub(r"\{[^}]+\}", "x1", route.path) for route in app.routes
                    if re.search("pair|runner", getattr(route, "path", ""))})
    assert "/api/v1/runner-pair" in paths and "/api/v1/pair/requests/x1/token" in paths
    for path in paths:
        assert not proxy.path_allowed(path), path
        head, _, tail = path.rpartition("/")
        assert not proxy.path_allowed(f"{head}/%{ord(tail[0]):02x}{tail[1:]}"), path
