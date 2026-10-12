"""Trust the Tailscale identity headers only from tailscaled.

`tailscale serve` forwards tailnet requests to the loopback listener and adds `Tailscale-User-Login` and the other
`Tailscale-*` identity headers. Any other program on this machine, or a container that reaches the host loopback, can
reach the same listener and send the same headers. So the daemon looks up who owns the other end of the TCP
connection: the identity headers count only when that process is the Tailscale Windows service, or the tailscaled
child it starts to run the network (`tailscaled.exe /subproc`, which owns the socket), and its executable is tailscaled
in the Tailscale install directory. The path alone is not enough: any local user can start that same
tailscaled.exe in userspace mode as a proxy to the daemon and choose the headers it sends.
Otherwise they are removed before anything reads them, and the request needs the local owner token or another
credential (harness/local_owner.py).

tailscaled on its own is not enough. `tailscale serve` names the caller by looking up the connection's source address,
and this node's own Tailscale address names the node's owner, so a local program that connects to
`https://<host>.<tailnet>.ts.net/` arrives through tailscaled as the owner. tailscaled puts that source address alone in
`X-Forwarded-For` (it replaces any value the caller sent), so a verified request whose forwarded address is one of this
node's own Tailscale addresses, or whose `X-Forwarded-For` is missing or holds more than one value, loses its identity
headers too.

Nor does tailscaled add those headers on a raw TCP forward (`tailscale serve --tcp` or `--tls-terminated-tcp`): it
passes the caller's bytes through, so a tailnet peer would write `Tailscale-User-Login` itself. This module does not
prevent that; publish the daemon only through the HTTPS proxy. It has a best-effort check: each new connection's lookup
reads the serve config, and refuses tailnet identity when the config can't be read or has a raw TCP forward to the
daemon's port, and from then until the daemon restarts. It catches only a forward still in place when a lookup runs.
tailscaled keeps a forwarded stream open after its mapping is removed, so anything able to change this node's serve
config can add a raw forward, open a connection through it, remove the forward, and only then send forged headers; that
connection's lookup sees a clean config and admits them. The owner accepted that risk (#623).

A raw forward is not the only way tailscaled passes a peer's own bytes on. When this node advertises a subnet route,
tailscaled ends a routed TCP connection itself and opens a new one from its own socket to the target, so a tailnet
device that reaches the daemon at a routed address (with `listen.host` set to `0.0.0.0` or a LAN address) would pass
every check above. `tailscale serve` proxies to the loopback address the daemon is published on, so the identity
headers count only on a connection whose server address is loopback; a missing or non-loopback one refuses them.

The check is implemented for Windows. Elsewhere the headers are removed unless `listen.trust_unverified_identity_headers`
is set, which trusts any local process that sends them.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

HEADER_PREFIX = b"tailscale-"
CACHE_SIZE = 256
CACHE_SECONDS = 5.0  # absolute, from the lookup: a closed connection's port can be reused by another process
TAILSCALED = "tailscaled.exe"
SERVICE = "Tailscale"
TAILSCALE_CLI = "tailscale.exe"
FORWARDED_FOR = b"x-forwarded-for"
SELF_IPS_SECONDS = 60.0
# The lookups block (the connection table, the CLI), so they run on their own few threads: a burst of tailnet
# connections waits here instead of filling the thread pool the rest of the daemon shares.
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tailscale-peer")


def supported() -> bool:
    return sys.platform == "win32"


def install_dirs() -> list[Path]:
    """Where the Tailscale installer puts tailscaled on Windows."""
    roots = {os.environ.get(name) for name in ("ProgramW6432", "ProgramFiles")}
    return [Path(root) / "Tailscale" for root in sorted(r for r in roots if r)]


def interface_ips() -> frozenset[str]:
    """Every address on this machine's network interfaces now, the Tailscale adapter's among them. Read on each request,
    so an address that changed after `tailscale ip` was cached still counts as this node's own."""
    return frozenset(a.address.split("%", 1)[0] for addrs in psutil.net_if_addrs().values() for a in addrs
                     if a.family in (socket.AF_INET, socket.AF_INET6))


def _canonical(ip: str) -> str | None:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def _same_ip(a: str, b: str) -> bool:
    x = _canonical(a)
    return x is not None and x == _canonical(b)


def is_loopback(ip) -> bool:
    """Whether `ip` is a loopback address. A host name or anything else that isn't an address is not."""
    canonical = _canonical(str(ip)) if ip else None
    return canonical is not None and ipaddress.ip_address(canonical).is_loopback


def connection_owner(client: tuple[str, int], server: tuple[str, int]) -> int | None:
    """The PID of the process whose socket is `client` and connects to `server`, from the OS connection table."""
    for conn in psutil.net_connections(kind="tcp"):
        laddr, raddr = conn.laddr, conn.raddr
        if (laddr and raddr and laddr.port == client[1] and raddr.port == server[1]
                and _same_ip(laddr.ip, client[0]) and _same_ip(raddr.ip, server[0])):
            return conn.pid
    return None


def process_exe(pid: int) -> str:
    return psutil.Process(pid).exe()


def service_pid() -> int | None:
    """The PID of the running Tailscale Windows service. Raises when the service is missing or can't be queried."""
    return psutil.win_service_get(SERVICE).pid()


def started_by(pid: int, parent: int) -> bool:
    """Whether `parent` started `pid`. The child must be newer than the parent, so a recycled parent PID can't match.
    Only an administrator can create a process with the SYSTEM service as its parent."""
    child = psutil.Process(pid)
    return child.ppid() == parent and child.create_time() >= psutil.Process(parent).create_time()


def is_tailscaled(exe: str, dirs: list[Path]) -> bool:
    """The name alone proves nothing: the executable must sit directly in a Tailscale install directory."""
    if not exe:
        return False
    path = Path(os.path.normcase(os.path.realpath(exe)))
    if path.name != os.path.normcase(TAILSCALED):
        return False
    return any(path.parent == Path(os.path.normcase(os.path.realpath(d))) for d in dirs)


def _tailscale(args: list[str], dirs: list[Path] | None) -> str:
    """Run the Tailscale CLI from the install directory, which asks tailscaled's LocalAPI. Raises when it fails."""
    for d in dirs if dirs is not None else install_dirs():
        cli = d / TAILSCALE_CLI
        if cli.is_file():
            return subprocess.run([str(cli), *args], capture_output=True, text=True, timeout=10, check=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    raise FileNotFoundError(f"{TAILSCALE_CLI} not found in the Tailscale install directory")


def node_ips(dirs: list[Path] | None = None) -> frozenset[str]:
    """This node's own Tailscale addresses, from `tailscale ip`. Raises when it can't tell."""
    out = _tailscale(["ip"], dirs)
    ips = frozenset(str(ipaddress.ip_address(line.strip())) for line in out.splitlines() if line.strip())
    if not ips:
        raise RuntimeError("tailscale ip printed no address")
    return ips


def serve_config(dirs: list[Path] | None = None) -> dict:
    """The node's serve config, from `tailscale serve status --json`. Raises when it can't be read."""
    config = json.loads(_tailscale(["serve", "status", "--json"], dirs))
    if not isinstance(config, dict):
        raise ValueError("tailscale serve status --json did not print an object")
    return config


def _tcp_forwards(node) -> list:
    """Every `TCPForward` target in a serve config, wherever it sits: the node's ports, foreground sessions, services."""
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k == "TCPForward" and v]
        return found + [t for k, v in node.items() if k != "TCPForward" for t in _tcp_forwards(v)]
    if isinstance(node, list):
        return [t for v in node for t in _tcp_forwards(v)]
    return []


def raw_forward_to(config: dict, port: int) -> bool:
    """Whether the serve config forwards raw TCP to `port` on any host. A target without a readable port counts."""
    for target in _tcp_forwards(config):
        try:
            if int(str(target).rsplit(":", 1)[-1]) == port:
                return True
        except ValueError:
            return True
    return False


def forwarded_for(scope) -> list[bytes]:
    return [v for n, v in scope.get("headers") or () if n.lower() == FORWARDED_FOR]


class PeerCheck:
    """Whether a connection's peer is the Tailscale service's tailscaled, on the daemon's loopback address, on a node
    whose serve config showed no raw TCP forward to the daemon at any lookup (a best-effort check; see the module
    docstring). Verdicts are cached per (client host, port) for a few seconds so keep-alive requests do not repeat the
    lookup; a failed lookup is not cached and counts as not tailscaled."""

    def __init__(self, *, owner=connection_owner, exe=process_exe, dirs=install_dirs, platform_ok=supported,
                 service=service_pid, child_of=started_by, tailscale_ips=node_ips, interface_addrs=interface_ips,
                 serve=serve_config, server_ok=is_loopback, clock=time.monotonic, size: int = CACHE_SIZE, ttl: float = CACHE_SECONDS,
                 tailscale_ips_ttl: float = SELF_IPS_SECONDS):
        self.owner, self.exe, self.dirs, self.platform_ok, self.clock = owner, exe, dirs, platform_ok, clock
        self.service, self.child_of, self.serve, self.server_ok = service, child_of, serve, server_ok
        # The node's Tailscale addresses are cached; its interface addresses are read on every request.
        self.tailscale_ips, self.interface_addrs = tailscale_ips, interface_addrs
        self.size, self.ttl, self.tailscale_ips_ttl = size, ttl, tailscale_ips_ttl
        self._own: tuple[float, frozenset[str]] | None = None
        self._cache: OrderedDict[tuple[str, int], tuple[float, bool]] = OrderedDict()
        self._lock = threading.Lock()
        self.raw_forward_seen = False  # stays set until the daemon restarts and drops every connection
        self.lookups = 0

    def cached(self, client: tuple[str, int]) -> bool | None:
        with self._lock:
            hit = self._cache.get(client)
            if hit is None:
                return None
            if self.clock() >= hit[0]:
                del self._cache[client]
                return None
            self._cache.move_to_end(client)
            return hit[1]

    def _remember(self, client: tuple[str, int], verdict: bool) -> None:
        with self._lock:
            self._cache[client] = (self.clock() + self.ttl, verdict)
            self._cache.move_to_end(client)
            while len(self._cache) > self.size:
                self._cache.popitem(last=False)

    def verify(self, client: tuple[str, int] | None, server: tuple[str, int] | None) -> bool:
        if not client or not server or not self.platform_ok():
            return False
        if not self.server_ok(server[0]):
            # A subnet-routed connection also comes from tailscaled's own socket, on the routed address.
            return False
        key = (str(client[0]), int(client[1]))
        hit = self.cached(key)
        if hit is not None:
            return hit
        self.lookups += 1
        try:
            pid = self.owner(key, (str(server[0]), int(server[1])))
            verdict = bool(pid) and self._from_service(pid) and is_tailscaled(self.exe(pid), self.dirs())
        except Exception as exc:  # noqa: BLE001 - any failure means the peer is not known to be tailscaled
            log.warning("could not identify the process behind a request with Tailscale identity headers: %s", exc)
            return False
        if verdict:
            # Read now, with this connection. This sees a raw forward only while it is still in the config: a stream
            # opened through one that has since been removed looks clean here (see the module docstring).
            try:
                verdict = not self._raw_forward(int(server[1]))
            except Exception as exc:  # noqa: BLE001 - an unread serve config could hold a raw TCP forward
                log.warning("could not read this node's tailscale serve config: %s", exc)
                return False
        self._remember(key, verdict)
        return verdict

    def _raw_forward(self, port: int) -> bool:
        """Whether a raw TCP forward to `port` is in the serve config now, or was at any lookup since the daemon started.
        A forward added and removed between lookups is never seen."""
        if self.raw_forward_seen:
            return True
        if raw_forward_to(self.serve(), port):
            self.raw_forward_seen = True
            log.warning("tailscale serve forwards raw TCP to the daemon's port %s, so tailnet identity is refused until "
                        "that forward is removed and the daemon restarts", port)
        return self.raw_forward_seen

    def _from_service(self, pid: int) -> bool:
        service = self.service()
        return bool(service) and (pid == service or self.child_of(pid, service))

    def own_ips_cached(self) -> bool:
        with self._lock:
            return self._own is not None and self.clock() < self._own[0]

    def _own_ips(self) -> frozenset[str] | None:
        with self._lock:
            if self._own is not None and self.clock() < self._own[0]:
                return self._own[1]
        try:
            ips = frozenset(ip for ip in map(_canonical, self.tailscale_ips()) if ip)
        except Exception as exc:  # noqa: BLE001 - without this node's addresses no request is known to be remote
            log.warning("could not read this node's Tailscale addresses: %s", exc)
            return None
        if not ips:
            return None
        with self._lock:
            self._own = (self.clock() + self.tailscale_ips_ttl, ips)
        return ips

    def admits(self, client: tuple[str, int] | None, server: tuple[str, int] | None, forwarded: list[bytes]) -> bool:
        """Whether a request's Tailscale identity headers count: tailscaled sent them, wrote them itself, and named
        another tailnet node. It may run a lookup that blocks."""
        return self.verify(client, server) and self.from_other_node(forwarded)

    def from_other_node(self, forwarded: list[bytes]) -> bool:
        """Whether tailscaled forwarded the request from another tailnet node. Call only for a verified tailscaled peer:
        the forwarded address must be one address, and not one of this node's own: not a cached `tailscale ip` address
        and not an address on one of its interfaces now."""
        if len(forwarded) != 1:
            return False
        source = _canonical(forwarded[0].decode("latin-1"))
        if source is None:
            return False
        own = self._own_ips()
        if own is None or source in own:
            return False
        try:
            current = {ip for ip in map(_canonical, self.interface_addrs()) if ip}
        except Exception as exc:  # noqa: BLE001 - without this node's addresses no request is known to be remote
            log.warning("could not read this machine's interface addresses: %s", exc)
            return False
        return source not in current


def default_check() -> PeerCheck:
    return PeerCheck()


def has_identity(scope) -> bool:
    return any(name.lower().startswith(HEADER_PREFIX) for name, _ in scope.get("headers") or ())


def strip_identity(scope) -> None:
    """Remove every `Tailscale-*` header so no later reader sees an identity that tailscaled did not add."""
    scope["headers"] = [(n, v) for n, v in scope.get("headers") or () if not n.lower().startswith(HEADER_PREFIX)]
