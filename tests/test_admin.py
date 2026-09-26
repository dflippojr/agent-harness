"""Issue #24: versioned owner API at /api/admin/v1 cannot be reached with app tokens."""

from __future__ import annotations

from fastapi.testclient import TestClient

from harness.admin import ADMIN_PATHS, ADMIN_SCOPE, API_VERSION, OWNER_KIND, PREFIX
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
        assert root["scopes"][ADMIN_SCOPE] == "owner-only Agent Harness Web operations under /api/admin/v1"
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


def test_app_tokens_cannot_use_admin_api(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        all_app_scopes = ["sessions", "sessions:all", "approvals", "images", "inference", "remote_control"]
        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": all_app_scopes}).json()
        device = client.post("/keys", json={"name": "zed"}).json()
        # Legacy pre-#91 display names remain valid owner credentials.
        owner = client.post("/keys", json={"name": "control-center", "kind": OWNER_KIND,
                                           "scopes": [ADMIN_SCOPE]}).json()
        assert app["kind"] == "app"
        assert app["key"].startswith("ha-")
        assert device["kind"] == "device"
        assert device["key"].startswith("hk-")
        assert owner["kind"] == OWNER_KIND
        assert owner["key"].startswith("ho-")
        assert owner["scopes"] == ADMIN_SCOPE

        assert client.post("/keys", json={"name": "nope", "kind": "app",
                                          "scopes": [ADMIN_SCOPE]}).status_code == 400
        assert client.post("/keys", json={"name": "nope", "kind": "device",
                                          "scopes": [ADMIN_SCOPE]}).status_code == 400

        refused = [
            client.get(PREFIX, headers=bearer(app["key"])),
            client.get(f"{PREFIX}/sessions", headers=bearer(app["key"])),
            client.get(f"{PREFIX}/jobs", headers=bearer(app["key"])),
            client.get(f"{PREFIX}/keys", headers=bearer(app["key"])),
            client.get(f"{PREFIX}/gpu", headers=bearer(device["key"])),
            client.post(f"{PREFIX}/sessions", headers=bearer(app["key"]), json={"prompt": "x"}),
            client.post(f"{PREFIX}/gpu/pause", headers=bearer(app["key"])),
        ]
        for response in refused:
            assert response.status_code == 403
            assert "app tokens" in response.json()["detail"]

        assert client.get(PREFIX, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get(f"{PREFIX}/sessions", headers=bearer(owner["key"])).status_code == 200
        created = client.post(f"{PREFIX}/sessions", headers=bearer(owner["key"]),
                              json={"prompt": "from the owner API"})
        assert created.status_code == 201
        sid = created.json()["id"]
        assert client.get(f"{PREFIX}/sessions/{sid}", headers=bearer(owner["key"])).json()["id"] == sid
        minted = client.post(f"{PREFIX}/keys", headers=bearer(owner["key"]),
                             json={"name": "another-owner", "kind": OWNER_KIND, "scopes": [ADMIN_SCOPE]})
        assert minted.status_code == 201
        assert minted.json()["kind"] == OWNER_KIND

        # App tokens still work on the public app contract.
        app_session = client.post("/api/v1/sessions", headers=bearer(app["key"]), json={"prompt": "app work"})
        assert app_session.status_code == 201
        assert client.get("/api/v1/sessions", headers=bearer(app["key"])).json()[0]["id"] == app_session.json()["id"]


def test_admin_owner_operations_match_unversioned_catalog(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        catalog = {op["path"] for op in client.get(PREFIX).json()["operations"]}
        for path in ADMIN_PATHS:
            assert PREFIX + path in catalog, path
        assert PREFIX + "/runners/{name}/poll" not in catalog
        openapi = client.get("/openapi.json").json()["paths"]
        assert "/sessions" in openapi
        assert PREFIX + "/sessions" not in openapi
        assert PREFIX in openapi
        assert "/a/{token}/{decision}" in openapi
        assert PREFIX + "/a/{token}/{decision}" not in openapi
