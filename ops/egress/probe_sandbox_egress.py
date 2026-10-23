"""Connect-only probe of the sandbox egress path (#671). Run it in a container on a proxy network, as docs/INSTALL.md
("Sandbox network commands") describes. Standard library only, so any image with Python runs it.

For each HOST:PORT it reports two results, and sends nothing beyond a TCP handshake or one CONNECT line:
  direct  a plain TCP connect from the container, closed at once. Expected: failed (the network is internal).
  proxy   a CONNECT through the sandbox egress proxy, closed after its status line. Expected: 403 for the host, the
          LAN and the tailnet; 200 for a --public control, which the proxy opens and the probe closes unused.
Exit status 0 only when every result is the expected one.
"""

from __future__ import annotations

import argparse
import socket
import sys

PROXY = ("harness-egress-sandbox", 8888)   # the sandbox egress proxy; fixed, so the probe can't be pointed elsewhere


def direct(host: str, port: int, timeout: float) -> str:
    try:
        with socket.create_connection((host, port), timeout):
            return "connected"
    except OSError as e:
        return f"failed ({type(e).__name__})"


def via_proxy(target: str, timeout: float) -> str:
    try:
        with socket.create_connection(PROXY, timeout) as conn:
            conn.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
            status = conn.makefile("rb").readline(200).decode("latin-1").split()
    except OSError as e:
        return f"failed ({type(e).__name__})"
    return status[1] if len(status) > 1 else "no answer"


def split_target(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    return host.strip("[]"), int(port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect-only probe of the sandbox egress path (#671)")
    parser.add_argument("blocked", nargs="*", metavar="HOST:PORT", help="must be unreachable both ways")
    parser.add_argument("--public", action="append", default=[], metavar="HOST:PORT",
                        help="a public control the proxy must allow (CONNECT, port 443)")
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args(argv)
    ok = True
    for target, allowed in [(t, False) for t in args.blocked] + [(t, True) for t in args.public]:
        host, port = split_target(target)
        reach = direct(host, port, args.timeout)
        answer = via_proxy(target, args.timeout)
        good = reach.startswith("failed") and answer == ("200" if allowed else "403")
        ok = ok and good
        print(f"{'ok ' if good else 'BAD'} {target:<40} direct: {reach:<28} proxy: {answer}"
              f"  (expected {'200' if allowed else '403'})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
