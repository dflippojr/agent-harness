#!/usr/bin/env python3
"""Let containers on one dedicated Docker network reach the loopback daemon's App API, and nothing else (#909).

The tower's daemon listens on 127.0.0.1:8100 only, and containers that use host-gateway reach every host address, so a
bind address is no boundary. This proxy admits a connection only when all of these hold:

- it arrived on the dedicated network's bridge interface (the listening socket is bound to that device with
  SO_BINDTODEVICE, so the kernel refuses a connection to the same address that came in on docker0 or another bridge);
- its source address is inside that network and is not the gateway (the host's own address on the bridge);
- the request is for the App API: `/api/v1` or `/api/v1/...`, never `/api/admin/v1`, the Web UI, `/keys`, `/v1/*`
  (inference), `/runners/*` or the pairing routes, with an origin-form target and no dot, encoded-slash or backslash
  tricks.

A forwarded request carries only the headers an App client needs (an allowlist). Identity and trust headers never
pass: `Tailscale-*` (a tower with `trust_unverified_identity_headers` would take them as the owner), the local owner
token, the Hub approval secret, cookies, `Origin` and `X-Forwarded-*`. `Host` is rewritten to the daemon's loopback
address. So the daemon sees a loopback request with no Tailscale identity and no local owner token: it admits it only
with a credential its route checks (an App token), or for the public discovery document.

Standard library only, so it runs as `python3 -I` under a DynamicUser that cannot read the daemon's data directory.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import ipaddress
import re
import socket
import sys
import threading

API = "/api/v1"
UPSTREAM_HOST = "127.0.0.1"
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
# Request headers an App client needs. Everything else is dropped, whatever its name.
REQUEST_HEADERS = ("Authorization", "Content-Type", "Accept", "Idempotency-Key", "X-Agent-Harness-Client",
                   "Last-Event-ID", "User-Agent")
# Response headers passed back. Never Set-Cookie, and no hop-by-hop headers.
RESPONSE_HEADERS = frozenset(h.lower() for h in (
    "Content-Type", "Content-Length", "Cache-Control", "Retry-After", "Location", "ETag", "Last-Modified",
    "WWW-Authenticate", "Warning", "Referrer-Policy", "Idempotency-Replayed", "X-Next-Before",
    "X-Agent-Harness-Deprecation", "X-Agent-Harness-Audit-Warning"))
# Path segments: RFC 3986 pchar, so no space, backslash, quote or control character can reach the daemon.
SEGMENT = r"[A-Za-z0-9._~!$&'()*+,;=:@%-]+"
API_PATH = re.compile(re.escape(API) + rf"(?:/{SEGMENT})*")
QUERY = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*")
# Encoded '.', '/', '\' and NUL: a decoded path must keep the shape that was checked.
ENCODED = re.compile(r"%(?:2e|2f|5c|00)", re.IGNORECASE)
REFUSED_PREFIXES = (API + "/pair",)  # loopback pairing assumes an App on the host; the planner never pairs
MAX_BODY = 16 * 1024 * 1024
MAX_CONNECTIONS = 32
CLIENT_TIMEOUT = 30.0
UPSTREAM_TIMEOUT = 300.0
CHUNK = 64 * 1024


def path_allowed(target: str) -> bool:
    """Whether a request target is an origin-form App API path (and query) this proxy forwards."""
    if not target.startswith("/") or "#" in target:
        return False
    path, _, query = target.partition("?")
    if not API_PATH.fullmatch(path) or ENCODED.search(target) or not QUERY.fullmatch(query):
        return False
    if any(segment in (".", "..") for segment in path.split("/")):
        return False
    return not any(path == prefix or path.startswith(prefix + "/") for prefix in REFUSED_PREFIXES)


class Peers:
    """The addresses allowed to connect: inside the dedicated network, except the gateway and the network's own
    network and broadcast addresses."""

    def __init__(self, network: ipaddress.IPv4Network, gateway: ipaddress.IPv4Address):
        self.network = network
        self.gateway = gateway

    def admits(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return (ip in self.network and ip != self.gateway and ip != self.network.network_address
                and ip != self.network.broadcast_address)


def handler_for(upstream_port: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # one request per connection; a streamed body ends when the connection closes
        timeout = CLIENT_TIMEOUT
        server_version = "harness-app-api-proxy"
        sys_version = ""

        def _refuse(self, status: int) -> None:
            self.close_connection = True
            body = b'{"detail":"not available through the App API proxy"}' if status == 404 else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _forward(self) -> None:
            if self.command not in METHODS:
                self._refuse(405)
                return
            if not path_allowed(self.path):
                self._refuse(404)
                return
            if self.headers.get("Transfer-Encoding") is not None:
                self._refuse(411)
                return
            outgoing = []
            for name in REQUEST_HEADERS:
                values = self.headers.get_all(name) or []
                if len(values) > 1:
                    self._refuse(400)
                    return
                if values:
                    outgoing.append((name, values[0]))
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) > 1 or (lengths and not lengths[0].strip().isdigit()):
                self._refuse(400)
                return
            length = int(lengths[0]) if lengths else 0
            if length > MAX_BODY:
                self._refuse(413)
                return
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                self._refuse(400)
                return
            conn = http.client.HTTPConnection(UPSTREAM_HOST, upstream_port, timeout=UPSTREAM_TIMEOUT)
            try:
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", f"{UPSTREAM_HOST}:{upstream_port}")
                for name, value in outgoing:
                    conn.putheader(name, value)
                if body or self.command in ("POST", "PUT", "PATCH"):
                    conn.putheader("Content-Length", str(len(body)))
                conn.putheader("Connection", "close")
                conn.endheaders(body or None)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException, ValueError):
                conn.close()
                self._refuse(502)
                return
            try:
                self.close_connection = True
                self.send_response(resp.status, resp.reason)
                for name, value in resp.getheaders():
                    if name.lower() in RESPONSE_HEADERS:
                        self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := resp.read1(CHUNK):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (OSError, http.client.HTTPException):
                pass  # the client or the daemon went away mid-response; the connection closes either way
            finally:
                conn.close()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _forward

        def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's signature
            pass  # no request lines: a path can carry an end-user id, and refusals are logged by the server

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, listen: str, port: int, peers: Peers, upstream_port: int, device: str | None = None):
        self.peers = peers
        self.device = device
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__((listen, port), handler_for(upstream_port))

    def server_bind(self):
        if self.device:
            # Only connections that arrived on this interface reach the socket (unprivileged since Linux 5.7).
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.device.encode())
        super().server_bind()

    def verify_request(self, request, client_address):
        if not self.peers.admits(client_address[0]):
            print(f"refused a connection from {client_address[0]}: not on the App API network",
                  file=sys.stderr, flush=True)
            return False
        if not self.slots.acquire(blocking=False):
            print(f"refused a connection from {client_address[0]}: {MAX_CONNECTIONS} already open",
                  file=sys.stderr, flush=True)
            return False
        return True

    def process_request(self, request, client_address):
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--network", required=True,
                        help="the dedicated Docker network's IPv4 subnet, e.g. 192.168.240.0/29")
    parser.add_argument("--listen", required=True, help="its gateway address on this host, e.g. 192.168.240.1")
    parser.add_argument("--device", required=True,
                        help="its bridge interface (docker network create -o com.docker.network.bridge.name=...)")
    parser.add_argument("--port", type=int, default=8100, help="port to listen on (default 8100)")
    parser.add_argument("--upstream-port", type=int, default=8100,
                        help="the daemon's port on 127.0.0.1 (default 8100)")
    args = parser.parse_args(argv)
    try:
        network = ipaddress.IPv4Network(args.network, strict=True)
        listen = ipaddress.IPv4Address(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    if network.prefixlen < 16 or network.overlaps(ipaddress.IPv4Network("127.0.0.0/8")):
        parser.error("--network must be one dedicated Docker network: /16 or smaller, not loopback")
    if listen not in network or listen in (network.network_address, network.broadcast_address):
        parser.error("--listen must be the gateway address inside --network")
    if not args.device or len(args.device) > 15 or "/" in args.device:
        parser.error("--device must be a network interface name")
    args.network, args.listen = network, listen
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    server = Server(str(args.listen), args.port, Peers(args.network, args.listen), args.upstream_port, args.device)
    print(f"App API proxy on {args.listen}:{args.port} ({args.device}, peers in {args.network}) "
          f"-> {UPSTREAM_HOST}:{args.upstream_port}{API}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
