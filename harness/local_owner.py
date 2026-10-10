"""The local owner credential for callers on this machine.

`tailscale serve` adds the caller's identity to every request it forwards; the daemon keeps it only when tailscaled is
the connection's peer (harness/tailscale_peer.py). A request without that identity reached the loopback listener some
other way, so it must say who it is: it carries the local owner token (a random secret the daemon keeps in data_dir),
or a credential its route checks itself (an API token, an inference key, a runner token, or a stream ticket). Only the
health and metrics endpoints answer without one.
"""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path

from .atomic_io import owner_only_acl, write_atomic

TOKEN_FILE = "local-owner.token"
HEADER = "X-Agent-Harness-Local-Token"
OPEN_PATHS = frozenset({"/health", "/metrics"})
API_PREFIXES = ("/api/v1", "/api/admin/v1")  # routes that check a bearer token's kind and scopes themselves
# Zero-touch pairing (#519): an App has no token yet; the owner's approval and its PKCE verifier decide.
PAIRING_REQUESTS = re.compile(r"/api/v1/pair/requests(/[^/]+/(claim|token))?")
REFUSED = "requests from this machine need the local owner token (see docs/INSTALL.md)"
# The host-only Hub approval secret (#543, harness/hub_claim.py): new at every daemon start, read by `harness hub`.
HUB_SECRET_FILE = "hub-approval.secret"
HUB_HEADER = "X-Agent-Harness-Hub-Approval"


def token_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / TOKEN_FILE


def read_token(data_dir: Path | str) -> str:
    try:
        return token_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_token(data_dir: Path | str) -> str:
    """The daemon's local owner token, created owner-only on first start and kept across restarts."""
    token = read_token(data_dir)
    if token:
        return token
    token = secrets.token_urlsafe(32)
    path = token_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, token + "\n", private=True)
    return token


def read_hub_secret(data_dir: Path | str) -> str:
    try:
        return (Path(data_dir) / HUB_SECRET_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def rotate_hub_secret(data_dir: Path | str) -> str:
    """A new host-only Hub approval secret, written owner-only. Called once per daemon start; the old one stops
    working."""
    secret = secrets.token_urlsafe(32)
    path = Path(data_dir) / HUB_SECRET_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only on Windows too (an ACL, set before the secret goes in), not only by POSIX mode bits.
    write_atomic(path, secret + "\n", private=True, prepare=owner_only_acl)
    return secret


def _bearer(request) -> str:
    header = request.headers.get("authorization") or ""
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def admitted(request, m, path: str) -> bool:
    """Whether a request that carries no Tailscale identity may continue to its route."""
    if path in OPEN_PATHS:
        return True
    api = path.startswith(API_PREFIXES)
    if (request.method == "OPTIONS" and api and request.headers.get("origin")
            and request.headers.get("access-control-request-method")):
        return True  # a browser's CORS preflight never carries credentials; it returns no data
    if request.method == "POST" and PAIRING_REQUESTS.fullmatch(path):
        return True  # an App on this machine pairs like one on the tailnet: rate-limited, owner-approved, PKCE-bound
    expected = getattr(m, "local_owner_token", "")
    sent = request.headers.get(HEADER, "")
    if expected and sent and hmac.compare_digest(sent.encode(), expected.encode()):
        return True
    token = _bearer(request)
    if path.startswith("/v1/") and (token or request.headers.get("x-api-key")):
        return True  # the inference endpoint checks its keys itself
    key = m.db.api_key_by_secret(token) if token else None
    if key is not None:
        # The owner's admin token is the owner anywhere. Other tokens only reach the API routes that check them.
        return api or (key.get("kind") == "owner" and "admin" in (key.get("scopes") or "").split())
    if token and path.startswith("/runners/"):
        return True  # runner routes check the runner token themselves
    return bool(request.query_params.get("ticket") and path.startswith("/api/v1/sessions/")
                and path.endswith("/events"))
