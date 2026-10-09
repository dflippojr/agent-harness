"""Trust the Tailscale identity headers only from tailscaled.

`tailscale serve` forwards tailnet requests to the loopback listener and adds `Tailscale-User-Login` and the other
`Tailscale-*` identity headers. Any other program on this machine, or a container that reaches the host loopback, can
reach the same listener and send the same headers. So the daemon looks up who owns the other end of the TCP
connection: the identity headers count only when that process is tailscaled, run from the Tailscale install directory.
Otherwise they are removed before anything reads them, and the request needs the local owner token or another
credential (harness/local_owner.py).

The check is implemented for Windows. Elsewhere the headers are removed unless `listen.trust_unverified_identity_headers`
is set, which trusts any local process that sends them.
"""

from __future__ import annotations

import ipaddress
import logging
import os
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


def supported() -> bool:
    return sys.platform == "win32"


def install_dirs() -> list[Path]:
    """Where the Tailscale installer puts tailscaled on Windows."""
    roots = {os.environ.get(name) for name in ("ProgramW6432", "ProgramFiles")}
    return [Path(root) / "Tailscale" for root in sorted(r for r in roots if r)]


def _same_ip(a: str, b: str) -> bool:
    try:
        x, y = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return False
    if isinstance(x, ipaddress.IPv6Address) and x.ipv4_mapped:
        x = x.ipv4_mapped
    if isinstance(y, ipaddress.IPv6Address) and y.ipv4_mapped:
        y = y.ipv4_mapped
    return x == y


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


def is_tailscaled(exe: str, dirs: list[Path]) -> bool:
    """The name alone proves nothing: the executable must sit directly in a Tailscale install directory."""
    if not exe:
        return False
    path = Path(os.path.normcase(os.path.realpath(exe)))
    if path.name != os.path.normcase(TAILSCALED):
        return False
    return any(path.parent == Path(os.path.normcase(os.path.realpath(d))) for d in dirs)


class PeerCheck:
    """Whether a connection's peer is tailscaled. Verdicts are cached per (client host, port) for a few seconds so
    keep-alive requests do not repeat the lookup; a failed lookup is not cached and counts as not tailscaled."""

    def __init__(self, *, owner=connection_owner, exe=process_exe, dirs=install_dirs, platform_ok=supported,
                 clock=time.monotonic, size: int = CACHE_SIZE, ttl: float = CACHE_SECONDS):
        self.owner, self.exe, self.dirs, self.platform_ok, self.clock = owner, exe, dirs, platform_ok, clock
        self.size, self.ttl = size, ttl
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
            verdict = bool(pid) and is_tailscaled(self.exe(pid), self.dirs())
        except Exception as exc:  # noqa: BLE001 - any failure means the peer is not known to be tailscaled
            log.warning("could not identify the process behind a request with Tailscale identity headers: %s", exc)
            return False
        self._remember(key, verdict)
        return verdict


def default_check() -> PeerCheck:
    return PeerCheck()


def has_identity(scope) -> bool:
    return any(name.lower().startswith(HEADER_PREFIX) for name, _ in scope.get("headers") or ())


def strip_identity(scope) -> None:
    """Remove every `Tailscale-*` header so no later reader sees an identity that tailscaled did not add."""
    scope["headers"] = [(n, v) for n, v in scope.get("headers") or () if not n.lower().startswith(HEADER_PREFIX)]
