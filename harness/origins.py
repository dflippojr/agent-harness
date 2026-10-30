"""Browser origins: the canonical form, the daemon's own origins, and the Origin check on bearer tokens (#842).

The owner API (harness/admin.py), the App API (harness/apps.py) and browser pairing (harness/pairing_requests.py)
share these. Nothing here imports those modules, so each imports this one at top level.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .compat import LOOPBACK_HOSTS  # a plain string set: ::ffff:127.0.0.1 is not in it (peer_is_loopback, #842)
from .manager import HarnessError


def normalize_origin(value: str) -> str:
    """Return a canonical web origin, or raise ValueError for URLs that are not origins."""
    value = (value or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as e:
        raise ValueError("origin must be a valid http(s) origin") from e
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("origin must contain only an http(s) scheme, host, and optional port")
    host = parsed.hostname.lower()
    if parsed.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ValueError("browser origins must use https (http is allowed only for loopback development)")
    if ":" in host:
        host = f"[{host}]"
    default_port = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    return f"{parsed.scheme}://{host}{f':{port}' if port is not None and not default_port else ''}"


def daemon_origins(cfg) -> set[str]:
    values = [cfg.public_url, f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}"]
    out = set()
    for value in values:
        try:
            out.add(normalize_origin(value))
        except ValueError:
            pass
    return out


def normalize_origin_or_403(raw_origin: str) -> str:
    """`normalize_origin` for a caller's Origin header: a value that isn't an origin is a 403."""
    try:
        return normalize_origin(raw_origin)
    except ValueError as e:
        raise HarnessError(403, str(e)) from e


def request_origin(request) -> str:
    """The caller's normalized Origin, "" for a native caller, or 403 for something that isn't an origin."""
    raw = request.headers.get("origin", "")
    return normalize_origin_or_403(raw) if raw else ""


def check_key_origin(request, mgr, key: dict, *, noun: str) -> None:
    """A browser may use a bearer token only from the daemon's own origins or the ones the key is bound to.

    `noun` names the token in the 403 ("owner" or "app"). A request without an Origin header is a native caller."""
    origin = request_origin(request)
    if origin and origin not in daemon_origins(mgr.cfg) and origin not in (key.get("origins") or []):
        raise HarnessError(403, f"this {noun} token is not approved for this origin")
