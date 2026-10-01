"""Git Credential Manager GitHub UI helper for member device sign-in (issue #63). Stdlib only.

GCM starts this program as `<helper> device --code <user code> --url <verification url>` and polls GitHub
itself. The helper relays only the URL and code to the daemon over a one-time loopback connection named by
AGENT_HARNESS_GCM_RELAY (host:port) and AGENT_HARNESS_GCM_NONCE, then stays alive until GCM kills it
(sign-in finished) or the daemon closes the connection (cancel, timeout, shutdown). Exiting early tells GCM
the user cancelled. Every other prompt (username/password, PAT, account picker) is refused.

The helper never sees a token and writes nothing to disk, stdout, or stderr.
"""

import os
import re
import socket
import sys
import time

DEADLINE_SECONDS = 15 * 60
_CODE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
_URLS = ("https://github.com/login/device",)


def _args(argv):
    if len(argv) < 1 or argv[0] != "device":
        return None
    out = {}
    i = 1
    while i + 1 < len(argv):
        if argv[i] in ("--code", "--url"):
            out[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    code, url = out.get("code", ""), out.get("url", "").rstrip("/")
    if not _CODE.match(code) or url not in _URLS:
        return None
    return code, url


def main(argv) -> int:
    parsed = _args(argv)
    relay = os.environ.get("AGENT_HARNESS_GCM_RELAY", "")
    nonce = os.environ.get("AGENT_HARNESS_GCM_NONCE", "")
    if parsed is None or not relay or not nonce:
        return 1
    host, _, port = relay.rpartition(":")
    if host != "127.0.0.1" or not port.isdigit():
        return 1
    code, url = parsed
    try:
        sock = socket.create_connection((host, int(port)), timeout=10)
    except OSError:
        return 1
    try:
        sock.sendall(f"{nonce}\t{url}\t{code}\n".encode("ascii"))
        sock.settimeout(1.0)
        end = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < end:
            try:
                if not sock.recv(64):
                    return 1  # the daemon closed the relay: cancel
            except socket.timeout:
                continue
            except OSError:
                return 1
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
