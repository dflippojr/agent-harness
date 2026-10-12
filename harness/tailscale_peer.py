"""Trust the Tailscale identity headers only from tailscaled.

`tailscale serve` forwards tailnet requests to the loopback listener and adds `Tailscale-User-Login` and the other
`Tailscale-*` identity headers. Any other program on this machine, or a container that reaches the host loopback, can
reach the same listener and send the same headers. So the daemon looks up who owns the other end of the TCP
connection: the identity headers count only when that process is the Tailscale Windows service, whose executable is
tailscaled in the Tailscale install directory. The path alone is not enough: any local user can start that same
tailscaled.exe in userspace mode as a proxy to the daemon and choose the headers it sends.
Otherwise they are removed before anything reads them, and the request needs the local owner token or another
credential (harness/local_owner.py).

tailscaled on its own is not enough. `tailscale serve` names the caller by looking up the connection's source address,
and this node's own Tailscale address names the node's owner, so a local program that connects to
`https://<host>.<tailnet>.ts.net/` arrives through tailscaled as the owner. tailscaled puts that source address alone in
`X-Forwarded-For` (it replaces any value the caller sent), so a verified request whose forwarded address is one of this
node's own Tailscale addresses, or whose `X-Forwarded-For` is missing or holds more than one value, loses its identity
headers too.

The check is implemented for Windows. Elsewhere the headers are removed unless `listen.trust_unverified_identity_headers`
is set, which trusts any local process that sends them.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
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


def supported() -> bool:
    return sys.platform == "win32"


def install_dirs() -> list[Path]:
    """Where the Tailscale installer puts tailscaled on Windows."""
    roots = {os.environ.get(name) for name in ("ProgramW6432", "ProgramFiles")}
    return [Path(root) / "Tailscale" for root in sorted(r for r in roots if r)]


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


def is_tailscaled(exe: str, dirs: list[Path]) -> bool:
    """The name alone proves nothing: the executable must sit directly in a Tailscale install directory."""
    if not exe:
        return False
    path = Path(os.path.normcase(os.path.realpath(exe)))
    if path.name != os.path.normcase(TAILSCALED):
        return False
    return any(path.parent == Path(os.path.normcase(os.path.realpath(d))) for d in dirs)


def node_ips(dirs: list[Path] | None = None) -> frozenset[str]:
    """This node's own Tailscale addresses, from `tailscale ip` in the install directory. Raises when it can't tell."""
    for d in dirs if dirs is not None else install_dirs():
        cli = d / TAILSCALE_CLI
        if cli.is_file():
            out = subprocess.run([str(cli), "ip"], capture_output=True, text=True, timeout=10, check=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            ips = frozenset(str(ipaddress.ip_address(line.strip())) for line in out.splitlines() if line.strip())
            if not ips:
                raise RuntimeError("tailscale ip printed no address")
            return ips
    raise FileNotFoundError(f"{TAILSCALE_CLI} not found in the Tailscale install directory")


def forwarded_for(scope) -> list[bytes]:
    return [v for n, v in scope.get("headers") or () if n.lower() == FORWARDED_FOR]


class PeerCheck:
    """Whether a connection's peer is the Tailscale service's tailscaled. Verdicts are cached per (client host, port)
    for a few seconds so keep-alive requests do not repeat the lookup; a failed lookup is not cached and counts as not
    tailscaled."""

    def __init__(self, *, owner=connection_owner, exe=process_exe, dirs=install_dirs, platform_ok=supported,
                 service=service_pid,
                 self_ips=node_ips, clock=time.monotonic, size: int = CACHE_SIZE, ttl: float = CACHE_SECONDS,
                 self_ips_ttl: float = SELF_IPS_SECONDS):
        self.owner, self.exe, self.dirs, self.platform_ok, self.clock = owner, exe, dirs, platform_ok, clock
        self.service, self.self_ips, self.size, self.ttl, self.self_ips_ttl = service, self_ips, size, ttl, self_ips_ttl
        self._own: tuple[float, frozenset[str]] | None = None
        self._cache: OrderedDict[tuple[str, int], tuple[float, bool]] = OrderedDict()
        self._lock = threading.Lock()
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
        key = (str(client[0]), int(client[1]))
        hit = self.cached(key)
        if hit is not None:
            return hit
        self.lookups += 1
        try:
            pid = self.owner(key, (str(server[0]), int(server[1])))
            verdict = bool(pid) and pid == self.service() and is_tailscaled(self.exe(pid), self.dirs())
        except Exception as exc:  # noqa: BLE001 - any failure means the peer is not known to be tailscaled
            log.warning("could not identify the process behind a request with Tailscale identity headers: %s", exc)
            return False
        self._remember(key, verdict)
        return verdict

    def own_ips_cached(self) -> bool:
        with self._lock:
            return self._own is not None and self.clock() < self._own[0]

    def _own_ips(self) -> frozenset[str] | None:
        with self._lock:
            if self._own is not None and self.clock() < self._own[0]:
                return self._own[1]
        try:
            ips = frozenset(ip for ip in map(_canonical, self.self_ips()) if ip)
        except Exception as exc:  # noqa: BLE001 - without this node's addresses no request is known to be remote
            log.warning("could not read this node's Tailscale addresses: %s", exc)
            return None
        if not ips:
            return None
        with self._lock:
            self._own = (self.clock() + self.self_ips_ttl, ips)
        return ips

    def from_other_node(self, forwarded: list[bytes]) -> bool:
        """Whether tailscaled forwarded the request from another tailnet node. Call only for a verified tailscaled peer:
        the forwarded address must be one address, and not one of this node's own."""
        if len(forwarded) != 1:
            return False
        source = _canonical(forwarded[0].decode("latin-1"))
        if source is None:
            return False
        own = self._own_ips()
        return own is not None and source not in own


def default_check() -> PeerCheck:
    return PeerCheck()


def has_identity(scope) -> bool:
    return any(name.lower().startswith(HEADER_PREFIX) for name, _ in scope.get("headers") or ())


def strip_identity(scope) -> None:
    """Remove every `Tailscale-*` header so no later reader sees an identity that tailscaled did not add."""
    scope["headers"] = [(n, v) for n, v in scope.get("headers") or () if not n.lower().startswith(HEADER_PREFIX)]
