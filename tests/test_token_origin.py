"""The browser-origin check on owner and app bearer tokens (#842), pinned through the two public auth entry points
so the check can move between modules without these tests changing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from harness import admin, apps
from harness.manager import HarnessError

PORT = 8100
PUBLIC = "https://harness.example"
BOUND = "https://bound.example"
TOKEN = "secret-token"
MALFORMED = "ftp://example.com/path"

# Origin header -> (status, detail) for a refusal, or None when the token is accepted.
MATRIX = {
    "no Origin header": (None, None),
    "daemon public URL": (PUBLIC, None),
    "daemon loopback": (f"http://127.0.0.1:{PORT}", None),
    "daemon localhost": (f"http://localhost:{PORT}", None),
    "key-bound origin": (BOUND, None),
    "key-bound origin, default port": (BOUND + ":443", None),
    "unknown origin": ("https://other.example", "approved"),
    "plain-http remote origin": ("http://other.example", "https"),
    "malformed origin": (MALFORMED, "only"),
}
MESSAGES = {
    "approved": {
        "owner": "this owner token is not approved for this origin",
        "app": "this app token is not approved for this origin",
    },
    "https": "browser origins must use https (http is allowed only for loopback development)",
    "only": "origin must contain only an http(s) scheme, host, and optional port",
}


def _manager(kind: str):
    key = {"id": "k1", "kind": kind, "scopes": "admin" if kind == "owner" else "sessions", "origins": [BOUND]}
    db = SimpleNamespace(api_key_by_secret=lambda token: dict(key) if token == TOKEN else None)
    return SimpleNamespace(cfg=SimpleNamespace(public_url=PUBLIC, port=PORT), db=db)


def _request(m, origin: str | None) -> Request:
    headers = [(b"authorization", f"Bearer {TOKEN}".encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers,
                    "app": SimpleNamespace(state=SimpleNamespace(manager=m))})


def _authenticate(kind: str, origin: str | None) -> dict:
    m = _manager(kind)
    request = _request(m, origin)
    if kind == "owner":
        return admin.require_admin(request, lambda r: r.app.state.manager)
    return apps.auth(request, "sessions")


@pytest.mark.parametrize("kind", ["owner", "app"])
@pytest.mark.parametrize("case", MATRIX)
def test_token_origin_matrix(kind, case):
    origin, refusal = MATRIX[case]
    if refusal is None:
        assert _authenticate(kind, origin)["id"] == "k1"
        return
    with pytest.raises(HarnessError) as caught:
        _authenticate(kind, origin)
    message = MESSAGES[refusal]
    assert caught.value.status == 403
    assert str(caught.value) == (message[kind] if isinstance(message, dict) else message)
    if refusal != "approved":
        assert isinstance(caught.value.__cause__, ValueError)


def test_an_owner_token_refused_on_the_app_api_gets_the_app_message():
    """`apps.auth` accepts owner keys too; an owner key refused there still gets the app message."""
    m = _manager("owner")
    with pytest.raises(HarnessError) as caught:
        apps.auth(_request(m, "https://other.example"), "sessions")
    assert str(caught.value) == MESSAGES["approved"]["app"]


def test_the_origin_helpers_live_in_a_leaf_module_with_no_lazy_apps_import():
    """harness/origins.py imports none of its callers, and no caller reaches the helpers through `.apps` lazily."""
    import ast
    import subprocess
    import sys
    from pathlib import Path

    loaded = subprocess.run([sys.executable, "-c", "import sys, harness.origins; print(sorted(m for m in sys.modules "
                             "if m in ('harness.admin', 'harness.apps', 'harness.api', 'harness.pairing_requests')))"],
                            capture_output=True, text=True, check=True, cwd=Path(__file__).resolve().parents[1])
    assert loaded.stdout.strip() == "[]"
    helpers = {"normalize_origin", "daemon_origins", "check_key_origin", "request_origin"}
    offenders = []
    for path in (Path(__file__).resolve().parents[1] / "harness").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.ImportFrom) and inner.module == "apps" and inner.level == 1
                            and helpers & {alias.name for alias in inner.names}):
                        offenders.append(f"{path.name}:{inner.lineno}")
    assert offenders == []


def test_one_loopback_constant():
    """Defined once, in compat.py, which the Mac bundle ships beside the CLI."""
    from harness import api, cli, compat, google_signin, origins

    assert compat.LOOPBACK_HOSTS == {"127.0.0.1", "localhost", "::1"}
    for module in (origins, google_signin, api, cli):
        assert module.LOOPBACK_HOSTS is compat.LOOPBACK_HOSTS


@pytest.mark.parametrize("host,loopback", [("127.0.0.1", True), ("::1", True), ("localhost", True),
                                           ("::ffff:127.0.0.1", False), ("100.64.0.1", False)])
def test_google_sign_in_peer_check_fails_closed_on_a_mapped_address(host, loopback):
    """Owner decision on #842: an IPv4-mapped loopback peer is not trusted as loopback."""
    from harness.google_signin import peer_is_loopback

    assert peer_is_loopback(SimpleNamespace(client=SimpleNamespace(host=host))) is loopback
