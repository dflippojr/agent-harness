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


def test_taint_clear_never_reaches_a_members_or_an_apps_session(tmp_path):
    """#785: the owner's taint/clear resolves like every other owner session route: never a member's or an App's
    session, by id or by prefix, and their taint stays; the owner's own sessions still clear by id and by prefix."""
    from test_household import ALICE, H, OWNER, create_member, household
    client, m = household(tmp_path)
    m._spawn = lambda sid: None  # created, not run: no run touches the taint under test
    taint = [{"kind": "web_fetch", "origin": "evil.example"}]
    with client:
        create_member(client, ALICE, "Alice")
        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions"]},
                          headers=H(OWNER)).json()
        member_sid = client.post("/api/v1/sessions", json={"prompt": "alice secret mango"}, headers=H(ALICE)).json()["id"]
        app_sid = client.post("/api/v1/sessions", json={"prompt": "app private papaya"},
                              headers=bearer(app["key"])).json()["id"]
        own_sid = client.post("/sessions", json={"prompt": "owner zebra"}, headers=H(OWNER)).json()["id"]
        for sid in (member_sid, app_sid, own_sid):
            m.db.update_session(sid, taint=taint)
        for sid in (member_sid, app_sid):
            hidden = client.get(f"/sessions/{sid}", headers=H(OWNER))
            assert hidden.status_code == 404
            for path in ("", PREFIX):
                for ref in (sid, sid[:6]):
                    r = client.post(f"{path}/sessions/{ref}/taint/clear", headers=H(OWNER))
                    assert (r.status_code, r.json()) == (404, hidden.json()), (path, ref, r.text)
                    assert "mango" not in r.text and "papaya" not in r.text
            assert m.db.get_session(sid)["taint"] == taint
        assert m.db.audit_page(action="session.taint_clear")["items"] == []

        cleared = client.post(f"/sessions/{own_sid[:6]}/taint/clear", headers=H(OWNER))
        assert cleared.status_code == 200 and cleared.json()["id"] == own_sid and cleared.json()["taint"] == []
        m.db.update_session(own_sid, taint=taint)
        cleared = client.post(f"{PREFIX}/sessions/{own_sid}/taint/clear", headers=H(OWNER))
        assert cleared.status_code == 200 and cleared.json()["taint"] == []


def test_a_like_wildcard_in_a_session_ref_is_matched_literally(tmp_path):
    """#785: `%` and `_` in a session ref are characters, not LIKE wildcards."""
    client, m = make_client(tmp_path)
    with client:
        for _ in range(2):
            assert client.post("/sessions", json={"prompt": "x"}).status_code == 201
        for ref in ("%25", "_"):
            r = client.get(f"/sessions/{ref}")
            assert r.status_code == 404, (ref, r.text)
            assert client.post(f"/sessions/{ref}/taint/clear").status_code == 404


def test_a_non_ascii_session_ref_on_the_admin_alias_is_a_404(tmp_path):
    """#929: a percent-encoded non-ASCII segment on an alias route answers like the unversioned route, not 500."""
    client, _ = make_client(tmp_path)
    with TestClient(client.app, raise_server_exceptions=False) as client:
        assert client.get("/sessions/%C3%A9").status_code == 404
        r = client.get(f"{PREFIX}/sessions/%C3%A9")
        assert r.status_code == 404, (r.status_code, r.text)


# Segments the alias rewrite must carry through without changing the answer: non-ASCII (two- and three-byte UTF-8),
# an encoded space, an encoded percent sign and an encoded slash.
_AWKWARD_SEGMENTS = ("%C3%A9", "%E2%9C%93", "a%20b", "%25", "x%2Fy")


def _filled(template: str, segment: str) -> str:
    import re

    return re.sub(r"\{[^}/]+\}", segment, template)


def test_every_templated_alias_route_answers_like_its_unversioned_route(tmp_path):
    """#929 class check: for each templated owner route and method, an awkward path segment gets the same status on
    /api/admin/v1 as on the unversioned route, and never a 500. Catches any alias-rewrite encoding bug on any route."""
    from fastapi.routing import APIRoute

    client, _ = make_client(tmp_path)
    with TestClient(client.app, raise_server_exceptions=False) as client:
        routes = [r for r in client.app.routes if isinstance(r, APIRoute) and r.path in ADMIN_PATHS and "{" in r.path]
        assert len(routes) > 10
        mismatches = []
        for route in routes:
            for method in sorted(route.methods - {"HEAD"}):
                for segment in _AWKWARD_SEGMENTS:
                    path = _filled(route.path, segment)
                    plain = client.request(method, path)
                    alias = client.request(method, PREFIX + path)
                    if alias.status_code >= 500 or alias.status_code != plain.status_code:
                        mismatches.append((method, path, plain.status_code, alias.status_code))
        assert not mismatches, mismatches
