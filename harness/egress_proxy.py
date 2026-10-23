"""Sandbox egress proxy (#671): the only way out for an approved `run_shell network: true` command.

A small HTTP forward proxy. It accepts `CONNECT host:443` and plain `http://` requests to port 80, resolves the
destination once, refuses it (403, no upstream connection) if any resolved address is not public, and otherwise
connects to exactly an address that passed the check, so a second lookup can't rebind the name to the host, the LAN
or the tailnet. One log line per request: destination, port and decision; never headers or bodies.

Standard library only: ops/egress/sandbox.Dockerfile copies this one file onto a slim Python image, and the policy
(ports, deny list, timeouts) comes from the owner-reviewed ops/egress/sandbox-proxy.conf.

    python egress_proxy.py --config /etc/egress/sandbox-proxy.conf
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import re
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[list[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

HEADER_LIMIT = 65536
HEADER_TIMEOUT = 30
BUFFER = 65536
HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}"
                      r"[a-z0-9_])?)*\.?$")
# Request headers that describe the client-to-proxy hop (RFC 9110 section 7.6.1). Transfer-Encoding stays: the body
# is relayed byte for byte, still chunked if it was.
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
               "upgrade"}

# IPv6 prefixes whose low 32 bits are an IPv4 address that a gateway may forward to; each is checked as that address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")              # RFC 6052 well-known NAT64 prefix
_IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")  # RFC 2765 SIIT
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")            # RFC 4291, deprecated, still parsed by stacks


@dataclass(frozen=True)
class Policy:
    """What the proxy forwards. Only public addresses ever pass; `deny` adds ranges on top and cannot allow any."""
    connect_ports: frozenset[int] = frozenset({443})
    http_ports: frozenset[int] = frozenset({80})
    deny: tuple[Network, ...] = ()
    listen: str = "0.0.0.0"
    port: int = 8888
    connect_timeout: float = 10
    idle_timeout: float = 600
    max_clients: int = 64


def embedded_ipv4(addr: Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address carried inside an IPv4-mapped, NAT64, SIIT, IPv4-compatible or 6to4 IPv6 address."""
    if addr.version != 6:
        return None
    assert isinstance(addr, ipaddress.IPv6Address)
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    if any(addr in net for net in (_NAT64, _IPV4_TRANSLATED, _IPV4_COMPATIBLE)):
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return None


def address_refusal(policy: Policy, address: str) -> str | None:
    """Why `address` may not be reached, or None if it is a public unicast address outside the deny list. The
    embedded IPv4 address of a mapped or translated form must pass too. `is_global` alone lets multicast through."""
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return f"{address!r} is not an IP address"
    inner = embedded_ipv4(addr)
    for form in (addr,) if inner is None else (addr, inner):
        if not form.is_global or form.is_multicast:
            return f"{address} is not a public address"
        for net in policy.deny:
            if form.version == net.version and form in net:
                return f"{address} is in the denied range {net}"
    return None


def port_refusal(policy: Policy, connect: bool, port: int) -> str | None:
    allowed = policy.connect_ports if connect else policy.http_ports
    if port in allowed:
        return None
    kind = "CONNECT" if connect else "plain HTTP"
    return f"port {port} is not allowed for {kind} (allowed: {', '.join(map(str, sorted(allowed))) or 'none'})"


def decide(policy: Policy, connect: bool, port: int, addresses: Iterable[str]) -> str | None:
    """The whole policy as a pure function: None to forward, otherwise why not. Every address must pass, so a name
    that resolves to one public and one private address is refused."""
    refusal = port_refusal(policy, connect, port)
    if refusal:
        return refusal
    addresses = list(addresses)
    if not addresses:
        return "the destination has no address"
    for address in addresses:
        refusal = address_refusal(policy, address)
        if refusal:
            return refusal
    return None


def load_policy(path: Path) -> Policy:
    """Read the `Key value` config file (`#` starts a comment). Unknown keys and bad values stop the proxy."""
    connect_ports: set[int] = set()
    http_ports: set[int] = set()
    deny: list[Network] = []
    scalars: dict[str, str] = {}
    keys = {"listen": "listen", "port": "port", "connecttimeout": "connect_timeout",
            "idletimeout": "idle_timeout", "maxclients": "max_clients"}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        key, value = key.lower(), value.strip()
        try:
            if key == "connectport":
                connect_ports.add(_port(value))
            elif key == "httpport":
                http_ports.add(_port(value))
            elif key == "deny":
                deny.append(ipaddress.ip_network(value, strict=True))
            elif key in keys and value:
                scalars[keys[key]] = value
            else:
                raise ValueError("unknown key or missing value")
        except ValueError as e:
            raise ValueError(f"{path}:{number}: {raw.strip()!r}: {e}") from None
    defaults = Policy()
    return Policy(
        connect_ports=frozenset(connect_ports), http_ports=frozenset(http_ports), deny=tuple(deny),
        listen=scalars.get("listen", defaults.listen), port=_port(scalars.get("port", str(defaults.port))),
        connect_timeout=float(scalars.get("connect_timeout", defaults.connect_timeout)),
        idle_timeout=float(scalars.get("idle_timeout", defaults.idle_timeout)),
        max_clients=int(scalars.get("max_clients", defaults.max_clients)))


def _port(value: str) -> int:
    port = int(value)
    if not 0 < port < 65536:
        raise ValueError(f"port {port} out of range")
    return port


class BadRequest(Exception):
    pass


@dataclass
class Request:
    connect: bool
    host: str
    port: int
    head: bytes = b""   # the request to send upstream (plain HTTP only)


def parse_request(head: bytes) -> Request:
    """Parse a proxy request head: `CONNECT host:port` or an absolute `http://` URL."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError as e:  # pragma: no cover - latin-1 decodes every byte
        raise BadRequest("undecodable request") from e
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise BadRequest("malformed request line")
    method, target, version = parts
    if method == "CONNECT":
        host, port = _host_port("//" + target, None)
        return Request(True, host, port)
    split = urlsplit(target)
    if split.scheme.lower() != "http" or not split.netloc:
        raise BadRequest("only CONNECT and absolute http:// URLs are proxied")
    host, port = _host_port(target, 80)
    path = (split.path or "/") + (f"?{split.query}" if split.query else "")
    headers = [line for line in lines[1:] if line]
    hop = set(HOP_HEADERS)
    for line in headers:
        name, _, value = line.partition(":")
        if name.strip().lower() == "connection":
            hop.update(token.strip().lower() for token in value.split(","))
    kept = [line for line in headers if line.partition(":")[0].strip().lower() not in hop]
    upstream = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""])
    return Request(False, host, port, upstream.encode("latin-1"))


def _host_port(target: str, default: int | None) -> tuple[str, int]:
    split = urlsplit(target)
    try:
        port = split.port if split.port is not None else default
    except ValueError as e:
        raise BadRequest("invalid port") from e
    host = (split.hostname or "").rstrip(".")
    if port is None or not host:
        raise BadRequest("missing host or port")
    if not (_is_ip(host) or HOSTNAME.match(host)):
        raise BadRequest("invalid host")
    return host, port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: dict[str, None] = {}
    for info in infos:
        seen[str(info[4][0])] = None
    return list(seen)


async def system_connect(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address, port, limit=BUFFER)


def _stdout_log(line: str) -> None:
    print(line, flush=True)


class EgressProxy:
    def __init__(self, policy: Policy, *, resolver: Resolver = system_resolve, connector: Connector = system_connect,
                 log: Callable[[str], None] = _stdout_log):
        self.policy = policy
        self.resolver = resolver
        self.connector = connector
        self.log = log
        self._slots = asyncio.Semaphore(policy.max_clients)

    async def serve(self, host: str | None = None, port: int | None = None) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle, self.policy.listen if host is None else host, self.policy.port if port is None else port,
            limit=HEADER_LIMIT)

    def _record(self, host: str, port: int | str, decision: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.log(f"{stamp} {host} {port} {decision}")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self._slots:
            try:
                await self._handle(reader, writer)
            finally:
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT)
            request = parse_request(head)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
            return
        except BadRequest as e:
            self._record("-", "-", f"deny ({e})")
            await _answer(writer, 400, "Bad Request", str(e))
            return
        refusal = port_refusal(self.policy, request.connect, request.port)
        addresses: list[str] = []
        if refusal is None:
            try:
                addresses = [request.host] if _is_ip(request.host) else await self.resolver(request.host, request.port)
            except OSError:
                self._record(request.host, request.port, "fail (could not resolve)")
                await _answer(writer, 502, "Bad Gateway", "could not resolve the destination")
                return
            refusal = decide(self.policy, request.connect, request.port, addresses)
        if refusal:
            self._record(request.host, request.port, f"deny ({refusal})")
            await _answer(writer, 403, "Forbidden", f"blocked by the sandbox egress policy: {refusal}")
            return
        upstream = await self._connect(addresses, request.port)
        if upstream is None:
            self._record(request.host, request.port, "fail (could not connect)")
            await _answer(writer, 502, "Bad Gateway", "could not connect to the destination")
            return
        up_reader, up_writer, address = upstream
        self._record(request.host, request.port, f"allow {address}")
        try:
            if request.connect:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                up_writer.write(request.head)
            await _relay(reader, writer, up_reader, up_writer, self.policy.idle_timeout)
        finally:
            up_writer.close()

    async def _connect(self, addresses: list[str], port: int):
        """Connect to the first reachable address, each one already checked. Nothing here resolves a name again."""
        for address in addresses:
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    self.connector(address, port), self.policy.connect_timeout)
            except (OSError, TimeoutError):
                continue
            return up_reader, up_writer, address
        return None


async def _answer(writer: asyncio.StreamWriter, code: int, reason: str, detail: str) -> None:
    body = (detail + "\n").encode("utf-8", errors="replace")
    writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1") + body)
    try:
        await writer.drain()
    except ConnectionError:
        pass


async def _relay(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter,
                 up_reader: asyncio.StreamReader, up_writer: asyncio.StreamWriter, idle: float) -> None:
    """Copy both ways until both sides finish, either side fails, or nothing moves for `idle` seconds."""
    loop = asyncio.get_running_loop()
    last = [loop.time()]

    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> bool:
        try:
            while data := await src.read(BUFFER):
                last[0] = loop.time()
                dst.write(data)
                await dst.drain()
            if dst.can_write_eof():
                dst.write_eof()
            return True
        except (ConnectionError, OSError):
            return False

    pending = {asyncio.ensure_future(pump(client_reader, up_writer)),
               asyncio.ensure_future(pump(up_reader, client_writer))}
    try:
        while pending:
            done, pending = await asyncio.wait(pending, timeout=min(idle, 30),
                                               return_when=asyncio.FIRST_COMPLETED)
            if any(not task.result() for task in done) or loop.time() - last[0] > idle:
                break
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)


async def _main(policy: Policy) -> None:
    proxy = EgressProxy(policy)
    server = await proxy.serve()
    ports = ", ".join(f"CONNECT {p}" for p in sorted(policy.connect_ports))
    ports += "".join(f", HTTP {p}" for p in sorted(policy.http_ports))
    _stdout_log(f"sandbox egress proxy on {policy.listen}:{policy.port}; {ports}; "
                f"public addresses only, {len(policy.deny)} extra denied ranges")
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sandbox egress proxy (#671)")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.config)
    except (OSError, ValueError) as e:
        print(f"egress proxy: {e}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_main(policy))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
