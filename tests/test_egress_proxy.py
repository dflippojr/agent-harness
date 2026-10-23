"""Issue #671: the sandbox egress proxy forwards to public addresses only, by resolved IP, without rebinding.

The policy is a pure function; the proxy runs in-process on loopback with an injected resolver and connector, so no
test needs Docker, DNS or the network. Addresses are documentation ranges, synthetic names, and two well-known
public resolver addresses standing in for "the public internet"."""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path

import pytest

from harness import egress_proxy as ep

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "ops/egress/sandbox-proxy.conf"
POLICY = ep.Policy()                    # the built-in floor alone: no extra deny ranges
COMMITTED = ep.load_policy(CONFIG)
PUBLIC = ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "2606:4700:4700::1111",
          "64:ff9b::808:808", "::ffff:8.8.8.8"]    # NAT64 and mapped forms of a public address are public too

DENIED = {
    "loopback": ["127.0.0.1", "127.255.255.254", "::1"],
    "unspecified": ["0.0.0.0", "0.1.2.3", "::"],
    "RFC 1918": ["10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.254", "192.168.0.1", "192.168.255.254"],
    "Docker host gateway": ["172.17.0.1", "172.18.0.1"],
    "host.docker.internal": ["192.168.65.254", "192.168.65.2"],
    "tailnet": ["100.64.0.1", "100.100.100.100", "100.127.255.254", "fd7a:115c:a1e0::1"],
    "link-local": ["169.254.0.1", "169.254.169.254", "fe80::1", "fe80::1%eth0"],
    "unique-local": ["fc00::1", "fd00::1", "fdff:ffff::1"],
    "multicast": ["224.0.0.1", "239.255.255.250", "ff02::1", "ff0e::1", "ff05::2"],
    "reserved and broadcast": ["240.0.0.1", "255.255.255.255", "192.0.0.1", "198.18.0.1"],
    "documentation": ["192.0.2.10", "198.51.100.7", "203.0.113.5", "2001:db8::5"],
    "IPv4-mapped": ["::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:172.17.0.1", "::ffff:192.168.65.254",
                    "::ffff:100.64.0.1", "::ffff:169.254.169.254", "::ffff:224.0.0.1", "::ffff:255.255.255.255"],
    "NAT64": ["64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::ac11:1", "64:ff9b::c0a8:41fe", "64:ff9b::6440:1",
              "64:ff9b::a9fe:a9fe", "64:ff9b::e000:1", "64:ff9b:1::a00:1"],
    "IPv4-compatible and SIIT": ["::10.0.0.1", "::127.0.0.1", "::ffff:0:a00:1"],
    "6to4": ["2002:a00:1::1", "2002:7f00:1::1", "2002:c0a8:41fe::1"],
    "Teredo": ["2001:0:4136:e378:8000:63bf:3fff:fdd2"],
}
ALL_DENIED = [(kind, a) for kind, addresses in DENIED.items() for a in addresses]


# policy

@pytest.mark.parametrize("policy", [POLICY, COMMITTED], ids=["floor", "committed"])
@pytest.mark.parametrize("kind, address", ALL_DENIED, ids=[f"{k}:{a}" for k, a in ALL_DENIED])
def test_every_non_public_address_is_refused(policy, kind, address):
    assert ep.address_refusal(policy, address) is not None
    assert ep.decide(policy, True, 443, [address]) is not None


@pytest.mark.parametrize("policy", [POLICY, COMMITTED], ids=["floor", "committed"])
@pytest.mark.parametrize("address", PUBLIC)
def test_a_public_address_is_allowed(policy, address):
    assert ep.address_refusal(policy, address) is None
    assert ep.decide(policy, True, 443, [address]) is None
    assert ep.decide(policy, False, 80, [address]) is None


def test_multicast_is_refused_although_python_calls_it_global():
    assert ipaddress.ip_address("224.0.0.1").is_global and ipaddress.ip_address("ff0e::1").is_global
    assert ep.address_refusal(POLICY, "224.0.0.1") == "224.0.0.1 is not a public address"
    assert ep.address_refusal(POLICY, "ff0e::1") is not None


def test_embedded_ipv4_is_checked_although_python_calls_the_wrapper_global():
    for wrapped in ("64:ff9b::a00:1", "::a00:1"):
        assert ipaddress.ip_address(wrapped).is_global
        assert ep.address_refusal(POLICY, wrapped) == f"{wrapped} is not a public address"
    assert ep.embedded_ipv4(ipaddress.ip_address("8.8.8.8")) is None
    assert ep.embedded_ipv4(ipaddress.ip_address("2606:4700:4700::1111")) is None


def test_a_name_with_one_public_and_one_private_address_is_refused():
    assert ep.decide(POLICY, True, 443, ["8.8.8.8", "10.1.2.3"]) == "10.1.2.3 is not a public address"
    assert ep.decide(POLICY, True, 443, ["2001:4860:4860::8888", "fd00::1"]) is not None
    assert ep.decide(POLICY, True, 443, ["8.8.8.8", "1.1.1.1"]) is None


def test_no_address_and_a_non_address_are_refused():
    assert ep.decide(POLICY, True, 443, []) == "the destination has no address"
    assert ep.address_refusal(POLICY, "pkg.example") == "'pkg.example' is not an IP address"


@pytest.mark.parametrize("connect, port, allowed", [
    (True, 443, True), (False, 80, True),
    (True, 80, False), (False, 443, False), (True, 22, False), (True, 8443, False), (False, 8080, False),
    (True, 8100, False), (False, 25, False),
])
def test_only_connect_443_and_http_80_are_allowed(connect, port, allowed):
    assert (ep.decide(POLICY, connect, port, ["8.8.8.8"]) is None) is allowed
    if not allowed:
        assert f"port {port} is not allowed" in ep.port_refusal(POLICY, connect, port)


def test_the_committed_deny_list_covers_what_the_floor_lets_through():
    """192.0.0.9 is a globally reachable anycast address inside the IETF block, and Python calls deprecated site-local
    addresses global; the committed file denies both blocks."""
    assert ep.address_refusal(POLICY, "192.0.0.9") is None
    assert ep.address_refusal(COMMITTED, "192.0.0.9") == "192.0.0.9 is in the denied range 192.0.0.0/24"
    assert ep.address_refusal(POLICY, "fec0::1") is None   # deprecated site-local
    assert ep.address_refusal(COMMITTED, "fec0::1") == "fec0::1 is in the denied range fec0::/10"


def test_the_deny_list_only_adds_ranges():
    policy = ep.Policy(deny=(ipaddress.ip_network("8.8.8.0/24"),))
    assert ep.address_refusal(policy, "8.8.8.8") == "8.8.8.8 is in the denied range 8.8.8.0/24"
    assert ep.address_refusal(policy, "::ffff:8.8.8.8") == "::ffff:8.8.8.8 is in the denied range 8.8.8.0/24"
    assert ep.address_refusal(policy, "1.1.1.1") is None


# the committed, owner-reviewed config

def test_the_committed_config():
    assert COMMITTED.connect_ports == {443} and COMMITTED.http_ports == {80}
    assert (COMMITTED.listen, COMMITTED.port, COMMITTED.max_clients) == ("0.0.0.0", 8888, 64)
    assert (COMMITTED.connect_timeout, COMMITTED.idle_timeout) == (10, 600)
    named = {str(n) for n in COMMITTED.deny}
    for net in ("127.0.0.0/8", "0.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::1/128", "::/128", "fc00::/7", "fe80::/10",
                "ff00::/8"):
        assert net in named


@pytest.mark.parametrize("text, error", [
    ("Bogus 1\n", "unknown key"), ("Deny 10.0.0.1/8\n", "host bits"), ("ConnectPort 70000\n", "out of range"),
    ("Port\n", "missing value"), ("ConnectPort https\n", "invalid literal"),
])
def test_a_bad_config_line_stops_the_proxy(tmp_path, text, error):
    path = tmp_path / "proxy.conf"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error) as e:
        ep.load_policy(path)
    assert f"{path}:1:" in str(e.value)
    assert ep.main(["--config", str(path)]) == 2
    assert ep.main(["--config", str(tmp_path / "missing.conf")]) == 2


def test_an_empty_config_forwards_nothing(tmp_path):
    path = tmp_path / "proxy.conf"
    path.write_text("# nothing\n\n", encoding="utf-8")
    policy = ep.load_policy(path)
    assert policy.connect_ports == frozenset() and policy.http_ports == frozenset()
    assert ep.port_refusal(policy, True, 443) == "port 443 is not allowed for CONNECT (allowed: none)"


# request parsing

def test_parse_connect():
    request = ep.parse_request(b"CONNECT Pkg.Example:443 HTTP/1.1\r\nHost: pkg.example:443\r\n\r\n")
    assert (request.connect, request.host, request.port) == (True, "pkg.example", 443)
    request = ep.parse_request(b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\n\r\n")
    assert (request.host, request.port) == ("2001:db8::1", 443)


def test_parse_plain_http_strips_hop_headers():
    head = (b"GET http://pkg.example/simple/six/?a=1 HTTP/1.1\r\nHost: pkg.example\r\n"
            b"Proxy-Authorization: Basic c2VjcmV0\r\nProxy-Connection: keep-alive\r\nConnection: keep-alive, X-Hop\r\n"
            b"X-Hop: 1\r\nAccept: */*\r\nTransfer-Encoding: chunked\r\n\r\n")
    request = ep.parse_request(head)
    assert (request.connect, request.host, request.port) == (False, "pkg.example", 80)
    assert request.head == (b"GET /simple/six/?a=1 HTTP/1.1\r\nHost: pkg.example\r\nAccept: */*\r\n"
                            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
    assert ep.parse_request(b"GET http://pkg.example:8080 HTTP/1.0\r\n\r\n").port == 8080
    assert ep.parse_request(b"GET http://pkg.example HTTP/1.1\r\n\r\n").head.startswith(b"GET / HTTP/1.1\r\n")


@pytest.mark.parametrize("head", [
    b"GET / HTTP/1.1\r\nHost: pkg.example\r\n\r\n",            # origin form: not a proxy request
    b"GET https://pkg.example/ HTTP/1.1\r\n\r\n",               # https goes through CONNECT
    b"GET ftp://pkg.example/ HTTP/1.1\r\n\r\n",
    b"CONNECT pkg.example HTTP/1.1\r\n\r\n",                    # no port
    b"CONNECT pkg.example:99999 HTTP/1.1\r\n\r\n",
    b"CONNECT bad_host$:443 HTTP/1.1\r\n\r\n",
    b"CONNECT :443 HTTP/1.1\r\n\r\n",
    b"CONNECT pkg.example:443\r\n\r\n",
    b"CONNECT pkg.example:443 SPDY/3\r\n\r\n",
])
def test_parse_refuses_what_is_not_a_proxy_request(head):
    with pytest.raises(ep.BadRequest):
        ep.parse_request(head)


# the proxy, in-process on loopback

class Net:
    """An injected resolver and connector. Every connection lands on one loopback echo server, so a test sees which
    address the proxy chose without any real route."""

    def __init__(self, names: dict[str, list[list[str]]]):
        self.names = names                  # name -> one answer per lookup, the last one repeating
        self.lookups: list[str] = []
        self.connects: list[tuple[str, int]] = []
        self.received = b""
        self.refuse = False

    async def resolve(self, host, port):
        self.lookups.append(host)
        answers = self.names.get(host)
        if answers is None:
            raise OSError("no such name")
        return answers[min(len(self.lookups) - 1, len(answers) - 1)]

    async def connect(self, address, port):
        self.connects.append((address, port))
        if self.refuse:
            raise ConnectionRefusedError
        return await asyncio.open_connection("127.0.0.1", self.upstream_port)

    async def echo(self, reader, writer):
        data = await reader.read(65536)
        self.received += data
        if data.startswith(b"GET "):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
        else:
            writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()


async def proxied(net: Net, request: bytes, then: bytes = b"", policy: ep.Policy = COMMITTED, hang_up=False):
    """Send one request through a fresh proxy; return (response bytes, log lines)."""
    logs: list[str] = []
    upstream = await asyncio.start_server(net.echo, "127.0.0.1", 0)
    net.upstream_port = upstream.sockets[0].getsockname()[1]
    proxy = ep.EgressProxy(policy, resolver=net.resolve, connector=net.connect, log=logs.append)
    server = await proxy.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        if hang_up:
            writer.write_eof()
        response = b""
        if then:
            response = await reader.readuntil(b"\r\n\r\n")
            writer.write(then)
            await writer.drain()
        response += await asyncio.wait_for(reader.read(), 5)
        writer.close()
        return response, logs
    finally:
        server.close()
        upstream.close()
        await server.wait_closed()
        await upstream.wait_closed()


def run(coro):
    return asyncio.run(coro)


def test_an_allowed_connect_tunnels():
    net = Net({"pkg.example": [["8.8.8.8"]]})
    response, logs = run(proxied(net, b"CONNECT pkg.example:443 HTTP/1.1\r\nHost: pkg.example:443\r\n\r\n",
                                 then=b"client hello"))
    assert response.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert response.endswith(b"echo:client hello")
    assert net.connects == [("8.8.8.8", 443)]
    assert logs[0].endswith(" pkg.example 443 allow 8.8.8.8")


@pytest.mark.parametrize("answer", [["10.0.0.5"], ["192.168.65.254"], ["172.17.0.1"], ["8.8.8.8", "100.64.0.9"],
                                    ["::ffff:127.0.0.1"], ["fd7a:115c:a1e0::9"]])
def test_a_denied_connect_answers_403_and_opens_nothing(answer):
    net = Net({"host.docker.internal": [answer]})
    response, logs = run(proxied(net, b"CONNECT host.docker.internal:443 HTTP/1.1\r\n\r\n"))
    assert response.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert b"blocked by the sandbox egress policy" in response
    assert net.connects == []
    assert len(logs) == 1 and " host.docker.internal 443 deny (" in logs[0]


def test_rebinding_cannot_redirect_the_connection():
    """The name answers public first and private after: the proxy looks it up once and connects to what it checked."""
    net = Net({"rebind.example": [["8.8.8.8"], ["10.0.0.5"]]})
    response, _ = run(proxied(net, b"CONNECT rebind.example:443 HTTP/1.1\r\n\r\n", then=b"x"))
    assert response.startswith(b"HTTP/1.1 200")
    assert net.lookups == ["rebind.example"]
    assert net.connects == [("8.8.8.8", 443)]


@pytest.mark.parametrize("target", [b"10.0.0.1:443", b"127.0.0.1:443", b"[::1]:443", b"[::ffff:10.0.0.1]:443",
                                    b"169.254.169.254:443", b"[fe80::1]:443"])
def test_an_ip_literal_is_checked_without_a_lookup(target):
    net = Net({})
    response, logs = run(proxied(net, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n"))
    assert response.startswith(b"HTTP/1.1 403")
    assert net.lookups == [] and net.connects == []


def test_a_public_ip_literal_connects_to_itself():
    net = Net({})
    response, _ = run(proxied(net, b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n\r\n", then=b"x"))
    assert response.startswith(b"HTTP/1.1 200")
    assert net.lookups == [] and net.connects == [("8.8.8.8", 443)]


@pytest.mark.parametrize("request_line", [b"CONNECT pkg.example:22 HTTP/1.1", b"CONNECT pkg.example:8100 HTTP/1.1",
                                          b"GET http://pkg.example:8080/ HTTP/1.1", b"CONNECT pkg.example:80 HTTP/1.1"])
def test_other_ports_are_refused_before_any_lookup(request_line):
    net = Net({"pkg.example": [["8.8.8.8"]]})
    response, logs = run(proxied(net, request_line + b"\r\n\r\n"))
    assert response.startswith(b"HTTP/1.1 403")
    assert net.lookups == [] and net.connects == []
    assert "is not allowed" in logs[0]


def test_plain_http_is_forwarded_without_proxy_headers_and_logged_without_them():
    net = Net({"pkg.example": [["1.1.1.1"]]})
    response, logs = run(proxied(net, b"GET http://pkg.example/simple/ HTTP/1.1\r\nHost: pkg.example\r\n"
                                      b"Proxy-Authorization: Basic c2VjcmV0\r\nCookie: session=c2VjcmV0\r\n\r\n"))
    assert response.startswith(b"HTTP/1.1 200 OK") and response.endswith(b"hi")
    assert net.received.startswith(b"GET /simple/ HTTP/1.1\r\nHost: pkg.example\r\n")
    assert b"Proxy-Authorization" not in net.received and b"Connection: close\r\n" in net.received
    assert net.connects == [("1.1.1.1", 80)]
    assert len(logs) == 1 and logs[0].endswith(" pkg.example 80 allow 1.1.1.1")
    assert "c2VjcmV0" not in "".join(logs) and "simple" not in "".join(logs)


def test_an_unresolvable_name_answers_502():
    net = Net({})
    response, logs = run(proxied(net, b"CONNECT nowhere.example:443 HTTP/1.1\r\n\r\n"))
    assert response.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert logs[0].endswith(" nowhere.example 443 fail (could not resolve)")


def test_an_unreachable_destination_answers_502_after_trying_each_address():
    net = Net({"pkg.example": [["8.8.8.8", "1.1.1.1"]]})
    net.refuse = True
    response, logs = run(proxied(net, b"CONNECT pkg.example:443 HTTP/1.1\r\n\r\n"))
    assert response.startswith(b"HTTP/1.1 502")
    assert net.connects == [("8.8.8.8", 443), ("1.1.1.1", 443)]
    assert logs[0].endswith("fail (could not connect)")


def test_a_bad_request_answers_400():
    net = Net({})
    response, logs = run(proxied(net, b"GET / HTTP/1.1\r\nHost: pkg.example\r\n\r\n"))
    assert response.startswith(b"HTTP/1.1 400 Bad Request")
    assert logs[0].endswith(" - - deny (only CONNECT and absolute http:// URLs are proxied)")


def test_a_client_that_hangs_up_early_is_dropped_quietly():
    net = Net({})
    response, logs = run(proxied(net, b"CONNECT pkg.exa", hang_up=True))
    assert response == b"" and logs == []


def test_the_system_resolver_and_connector_work_on_loopback():
    async def go():
        assert "127.0.0.1" in await ep.system_resolve("127.0.0.1", 443)

        async def hello(reader, writer):
            writer.write(b"hi")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(hello, "127.0.0.1", 0)
        reader, writer = await ep.system_connect("127.0.0.1", server.sockets[0].getsockname()[1])
        assert await reader.read() == b"hi"
        writer.close()
        server.close()
        await server.wait_closed()
    run(go())


def test_an_idle_tunnel_is_closed():
    async def go():
        async def silent(reader, writer):
            await reader.read()
            writer.close()
        upstream = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]

        async def connector(address, _):
            return await asyncio.open_connection("127.0.0.1", port)

        async def resolver(host, _):
            return ["8.8.8.8"]
        proxy = ep.EgressProxy(ep.Policy(idle_timeout=0.2), resolver=resolver, connector=connector, log=lambda _: None)
        server = await proxy.serve("127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(b"CONNECT pkg.example:443 HTTP/1.1\r\n\r\n")
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        assert await asyncio.wait_for(reader.read(), 5) == b""     # closed by the proxy, not by the test
        writer.close()
        server.close()
        upstream.close()
    run(go())
