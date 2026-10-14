"""Issue #24: versioned owner API at /api/admin/v1 cannot be reached with app tokens."""

from __future__ import annotations

from fastapi.testclient import TestClient

from harness.admin import ADMIN_PATHS, ADMIN_SCOPE, API_VERSION, PREFIX
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager

from test_daemon import Script, make_cfg

LOGIN = "me@example.com"


def make_client(tmp_path, steps=None):
    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = [LOGIN]
    m = Manager(cfg, chat=Script(steps or [Completion(content="hi")]))
    return TestClient(create_app(m)), m


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_root_and_unversioned_compat(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        root = client.get(PREFIX).json()
        assert root["api_version"] == API_VERSION
        assert root["server"] == "agent-harness"
        assert ADMIN_SCOPE in root["scopes"]
        assert root["scopes"][ADMIN_SCOPE] == "owner-only Server administration under /api/admin/v1 (Stables, the Hub and the CLI)"
        assert root["auth"]["tailscale_owner"] is True
        paths = {op["path"] for op in root["operations"]}
        assert f"{PREFIX}/sessions" in paths
        assert f"{PREFIX}/sessions/{{ref}}/review/{{action}}" in paths
        assert f"{PREFIX}/keys" in paths
        assert f"{PREFIX}/gpu" in paths
        assert f"{PREFIX}/jobs" in paths
        assert f"{PREFIX}/maintenance" in paths
        assert f"{PREFIX}/remote-control/{{project}}/trust" in paths
        assert f"{PREFIX}/search" in paths
        assert f"{PREFIX}/templates" in paths
        assert f"{PREFIX}/runners/{{name}}/poll" not in paths
        assert ADMIN_SCOPE not in client.get("/api/v1").json()["scopes"]
        sid = client.post("/sessions", json={"prompt": "hello"}).json()["id"]
        listed = client.get(f"{PREFIX}/sessions").json()
        assert listed[0]["id"] == sid
        assert client.get(f"/sessions/{sid}").json()["id"] == sid


def test_admin_owner_operations_match_unversioned_catalog(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        catalog = {op["path"] for op in client.get(PREFIX).json()["operations"]}
        for path in ADMIN_PATHS:
            assert PREFIX + path in catalog, path
        assert PREFIX + "/runners/{name}/poll" not in catalog
        openapi = client.get("/openapi.json").json()["paths"]
        assert "/sessions" in openapi
        assert PREFIX + "/sessions" in openapi
        assert openapi[PREFIX + "/sessions"]["get"]["responses"] == openapi["/sessions"]["get"]["responses"]
        assert PREFIX in openapi
        assert "/a/{token}/{decision}" in openapi
        assert PREFIX + "/a/{token}/{decision}" not in openapi


def test_web_session_and_github_actions_are_on_the_owner_api(tmp_path):
    """#334: Web's checkpoint, secret-finding, taint and GitHub calls reach their handlers under /api/admin/v1."""
    client, _ = make_client(tmp_path)
    with client:
        for method, path in [("GET", "/sessions/nope/checkpoints"), ("POST", "/sessions/nope/checkpoints/1/rewind"),
                             ("POST", "/sessions/nope/checkpoints/1/fork"), ("POST", "/sessions/nope/taint/clear"),
                             ("POST", "/sessions/nope/secret-findings/fix"),
                             ("POST", "/sessions/nope/secret-findings/abc/dismiss")]:
            body = {"prompt": "x", "reason": "x"}
            r = client.request(method, PREFIX + path, json=body)
            assert r.status_code == 404 and "no session matches" in r.json()["detail"], (path, r.text)
        r = client.get(PREFIX + "/github/projects/scratch/items")
        assert r.status_code == 400 and "GitHub" in r.json()["detail"]
        r = client.post(PREFIX + "/github/sessions", json={"prompt": "x", "project": "scratch", "number": 1})
        assert r.status_code == 400 and "GitHub" in r.json()["detail"]
