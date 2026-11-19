"""#1015 (agent-ops#637 stage 3): documentation-only response models for the routes Stables and the Hub read.

A documented route names its reply model with `responses=documented(Model)` (harness/response_docs.py), so the
OpenAPI document describes the reply while FastAPI leaves the payload alone. tests/response_model_capture.py checks
every real reply of a documented route, in every test, against its model. Here:

- the check itself: a reply that contradicts its model fails, and a documented route's payload is not filtered;
- one scenario per area drives each documented route to a real success reply on a disposable Server, and fails
  unless every route it claims was checked against its model;
- the scenarios together claim every documented route, so a new documented route needs a scenario that reaches it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from pathlib import Path
from typing import NotRequired
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import response_model_capture as capture
from harness import pairing_requests as pr
from harness.admin import PREFIX
from harness.api import create_app
from harness.config import BackendConfig, BackupConfig, RunnerConfig
from harness.llm import Completion
from harness.manager import Manager
from harness.response_docs import Open, documented

from test_daemon import Script, call, make_cfg
from test_household import ALICE, OWNER, H, create_member, household
from waits import scaled

ADMIN = PREFIX


def _wait(predicate, timeout: float = 30):
    deadline = time.monotonic() + scaled(timeout)
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, (response.request.method, response.request.url, response.text)
    return response.json()


class _Seen:
    """The documented replies checked while a scenario runs, and the problems found."""

    def __enter__(self):
        self.before = set(capture.seen)
        capture.seen.clear()
        return self

    def __exit__(self, *exc):
        self.checked = set(capture.seen)
        capture.seen |= self.before
        return False


def _documented_routes(app: FastAPI) -> set[tuple[str, str, int]]:
    return {(method, route.path, status) for route in app.routes if isinstance(route, APIRoute)
            for status in capture.documented_models(route) for method in route.methods or ()}


# The check itself ------------------------------------------------------------------------------------------------------
class Reply(Open):
    login: str
    count: int
    note: NotRequired[str]


def _probe_app(payload: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/probe", responses=documented(Reply))
    async def probe():
        return payload
    return app


def test_a_reply_that_contradicts_its_model_is_reported():
    capture.problems.clear()
    with TestClient(_probe_app({"login": "me", "count": "3"})) as client:
        assert client.get("/probe").json() == {"login": "me", "count": "3"}  # sent as the route returned it
    found, capture.problems[:] = list(capture.problems), []
    assert len(found) == 1 and "GET /probe 200 does not match Reply" in found[0] and "count" in found[0]


def test_a_missing_required_field_is_reported_and_an_optional_or_extra_one_is_not():
    capture.problems.clear()
    with TestClient(_probe_app({"login": "me"})) as client:
        client.get("/probe")
    found, capture.problems[:] = list(capture.problems), []
    assert len(found) == 1 and "count" in found[0] and "Field required" in found[0]
    with TestClient(_probe_app({"login": "me", "count": 3, "extra": [1]})) as client:
        assert client.get("/probe").json() == {"login": "me", "count": 3, "extra": [1]}
    assert capture.problems == []


def test_documented_routes_are_documentation_only(tmp_path):
    """A documented route has no response_model, so FastAPI neither filters nor validates its payload; and every
    documented model allows the extra fields a reply may carry."""
    app, _ = _all_modules_app(tmp_path)
    routes = [r for r in app.routes if isinstance(r, APIRoute) and capture.documented_models(r)]
    assert len(routes) > 60
    for route in routes:
        assert route.response_model is None and route.response_field is None, route.path
    schemas = app.openapi()["components"]["schemas"]
    # Two models with one name make FastAPI rename both to module-qualified names, which renames a component
    # clients already use: every documented model needs a name of its own.
    assert not [name for name in schemas if name.startswith(("harness__", "harness_modules__", "tests__"))]
    for route in routes:
        for status in capture.documented_models(route):
            content = app.openapi()["paths"][route.path][next(iter(route.methods)).lower()]["responses"][str(status)]
            ref = json.dumps(content)
            for name in re.findall(r"#/components/schemas/([A-Za-z0-9_]+)", ref):
                assert schemas[name].get("additionalProperties") is True or schemas[name].get("type") != "object" \
                    or "allOf" in schemas[name], name


# Scenarios -------------------------------------------------------------------------------------------------------------
def _all_modules_app(tmp: Path):
    cfg = make_cfg(tmp)
    cfg.allowed_logins = [OWNER]
    for field in dataclasses.fields(cfg.installed):
        setattr(cfg.installed, field.name, True)
        setattr(cfg.modules, field.name, True)
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    return create_app(m), m


def _owner(client, method: str, path: str, status: int = 200, **kwargs) -> dict | list:
    return _ok(client.request(method, path, headers={**H(OWNER), **kwargs.pop("headers", {})}, **kwargs), status)


def scenario_identity(tmp: Path, monkeypatch):
    client, m = household(tmp)
    with client:
        _ok(client.get("/health"))
        member = create_member(client, ALICE, "Alice")
        assert _owner(client, "GET", "/me")["role"] == "owner"
        assert _owner(client, "GET", ADMIN + "/me")["capabilities"]["admin"] is True
        assert _ok(client.get("/me", headers=H(ALICE)))["usage"]["account_hint"]
        assert _ok(client.get("/api/v1/me"))["user_id"] == "owner"
        assert "google" in _ok(client.get("/api/v1/auth/session"))
        assert member["user_id"]
    m.db.close()


def scenario_catalog(tmp: Path, monkeypatch):
    client, m = household(tmp)
    m.cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    with client:
        _owner(client, "GET", ADMIN + "/projects")
        added = _owner(client, "POST", ADMIN + "/projects", 201, json={"name": "contract", "description": "x"})
        assert added["managed"] is True
        assert _owner(client, "GET", ADMIN + "/backends", params={"auth": "skip"})[0]["name"] == "local"
        _owner(client, "PUT", ADMIN + "/backends/local", json={"model": "fake"})
        _owner(client, "GET", ADMIN + "/smart-approvals")
        _owner(client, "PUT", ADMIN + "/smart-approvals", json={"mode": "off"})
        _owner(client, "GET", ADMIN + "/queue")
        _owner(client, "GET", ADMIN + "/maintenance")
        _owner(client, "POST", ADMIN + "/maintenance/cleanup", json={})
    m.db.close()


def scenario_apps(tmp: Path, monkeypatch):
    client, m = household(tmp)
    secret = tmp / "grant.key"
    secret.write_text("grant-secret\n", encoding="utf-8")
    m.cfg.provider_secret_files = {"grant-ref": str(secret)}
    m.cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    with client:
        app = _owner(client, "POST", "/keys", 201, json={"name": "a", "kind": "app", "scopes": ["sessions"]})
        assert any(k["id"] == app["id"] for k in _owner(client, "GET", ADMIN + "/keys"))
        _owner(client, "GET", f"{ADMIN}/apps/{app['id']}/limits")
        _owner(client, "PUT", f"{ADMIN}/apps/{app['id']}/limits", json={"max_running": 2})
        _owner(client, "PUT", f"{ADMIN}/apps/{app['id']}/retention", json={"retention_days": 7})
        _owner(client, "GET", ADMIN + "/provider-credentials")
        _owner(client, "POST", ADMIN + "/provider-credentials", 201, json={
            "app_id": app["id"], "backend": "claude", "secret_ref": "grant-ref", "policy": "api_key"})
        assert client.delete(f"/keys/{app['id']}", headers=H(OWNER)).status_code == 204
        assert _owner(client, "GET", ADMIN + "/apps/erasures")[0]["id"] == app["id"]
        assert _owner(client, "GET", ADMIN + "/hub-claim")["claimed"] is False
    m.db.close()


def scenario_accounts(tmp: Path, monkeypatch):
    client, m = household(tmp)
    with client:
        added = _owner(client, "POST", ADMIN + "/accounts", 201, json={"login": ALICE, "display_name": "Alice"})
        uid = added["user_id"]
        _owner(client, "GET", ADMIN + "/accounts")
        _owner(client, "GET", f"{ADMIN}/accounts/{uid}")
        _owner(client, "PATCH", f"{ADMIN}/accounts/{uid}", json={"max_running": 2, "display_name": "Al"})
        assert _owner(client, "GET", ADMIN + "/accounts/audit", params={"limit": 50})
        assert _owner(client, "GET", ADMIN + "/audit", params={"limit": 2})["items"]
    m.db.close()


def scenario_sessions(tmp: Path, monkeypatch):
    rules = [{"tool": "write_file", "path": "secret/*", "action": "ask", "reason": "sensitive"}]
    cfg = make_cfg(tmp, rules=rules)
    cfg.allowed_logins = [OWNER]
    ask = Completion(tool_calls=[call("write_file", 0, path="secret/a.txt", content="x")])
    m = Manager(cfg, chat=Script([ask, Completion(content="done"), Completion(content="again"),
                                  Completion(content="more")]))

    def status(client, sid, *wanted):
        return _wait(lambda: client.get(f"/sessions/{sid}").json()["status"] in wanted)

    with TestClient(create_app(m)) as client:
        sid = _ok(client.post("/sessions", json={"prompt": "write secret", "project": "guarded"}), 201)["id"]
        status(client, sid, "waiting_approval")
        pending = _owner(client, "GET", f"{ADMIN}/sessions/{sid}/approvals")
        assert len(pending) == 1
        _owner(client, "POST", f"{ADMIN}/sessions/{sid}/approvals/{pending[0]['id']}", json={"decision": "approve"})
        status(client, sid, "done")
        _owner(client, "PATCH", f"{ADMIN}/sessions/{sid}", json={"title": "Renamed"})
        _owner(client, "PUT", f"{ADMIN}/sessions/{sid}", json={"title": "Renamed again"})
        _owner(client, "POST", f"{ADMIN}/sessions/{sid}/messages", json={"content": "and again"})
        status(client, sid, "done", "failed")
        other = _owner(client, "POST", f"{ADMIN}/sessions/{sid}/rerun", 201)["id"]
        status(client, other, "waiting_approval", "done", "failed")
        if client.get(f"/sessions/{other}").json()["status"] == "waiting_approval":
            _owner(client, "POST", f"{ADMIN}/sessions/{other}/cancel")
        else:  # the rerun finished first: cancel one that waits instead
            third = _ok(client.post("/sessions", json={"prompt": "write secret", "project": "guarded"}), 201)["id"]
            status(client, third, "waiting_approval")
            _owner(client, "POST", f"{ADMIN}/sessions/{third}/cancel")
    m.db.close()


def scenario_pairing(tmp: Path, monkeypatch):
    client, m = household(tmp)
    base = ADMIN + "/pairing-requests"
    with client:
        armed = _owner(client, "POST", base, 201, json={"catalog_app_id": "com.example.list", "scopes": ["sessions"]})
        _owner(client, "GET", base)
        verifier, challenge = _pkce()
        claimed = _ok(client.post(f"/api/v1/pair/requests/{armed['id']}/claim", json={"code_challenge": challenge}))
        _owner(client, "POST", f"{base}/{armed['id']}/confirm", json={"match": claimed["match_code"]})
        verifier, challenge = _pkce()
        asked = _ok(client.post("/api/v1/pair/requests", json={"name": "List", "scopes": ["sessions"],
                                                               "code_challenge": challenge}), 201)
        _owner(client, "POST", f"{base}/{asked['id']}/approve", json={"match": asked["match_code"]})
        slot = _owner(client, "POST", base, 201, json={"catalog_app_id": "com.example.other", "scopes": ["sessions"]})
        assert _owner(client, "POST", f"{base}/{slot['id']}/deny")["state"] == "denied"
    m.db.close()


def _pkce() -> tuple[str, str]:
    import secrets
    verifier = secrets.token_urlsafe(48)
    return verifier, pr.challenge_of(verifier)


def scenario_settings(tmp: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    monkeypatch.setattr("harness.config_api.schedule_exit", lambda: None)
    cfg = make_cfg(tmp)
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    with TestClient(create_app(m)) as client:
        _ok(client.get(ADMIN + "/config/schema"))
        revision = _ok(client.get(ADMIN + "/config"))["revision"]
        _ok(client.post(ADMIN + "/config/validate", json={"revision": revision, "changes": {"sessions.max_turns": 40}}))
        first = _ok(client.patch(ADMIN + "/config", json={"revision": revision, "changes": {"sessions.max_turns": 40}}))
        rolled = _ok(client.post(ADMIN + "/config/rollback", json={"confirm": True, "revision": first["revision"]}))
        _ok(client.post(ADMIN + "/config/restart", json={"confirm": True, "revision": rolled["revision"]}), 202)
    m.db.close()


def scenario_google(tmp: Path, monkeypatch):
    from test_google_signin import Env
    env = Env(tmp, monkeypatch)
    try:
        uid = env.ids[ALICE]
        _ok(env.owner("GET", f"{ADMIN}/google-signin"))
        _ok(env.owner("POST", f"{ADMIN}/accounts/{uid}/google/invitation"), 201)
        _ok(env.owner("DELETE", f"{ADMIN}/accounts/{uid}/google/invitation"))
        _ok(env.owner("POST", f"{ADMIN}/accounts/{uid}/google/revoke-sessions"))
        _ok(env.owner("DELETE", f"{ADMIN}/accounts/{uid}/google", json={"confirm": True}))
    finally:
        env.close()


def scenario_github(tmp: Path, monkeypatch):
    from github_fakes import FakeGitHub
    from harness import github_auth as ga
    if ga.platform_key() == "linux":
        monkeypatch.setenv("DISPLAY", ":99")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")
    fake = FakeGitHub(tmp / "fakes")
    client, m = household(tmp)
    fake.configure(m.cfg)
    with client:
        uid = create_member(client, ALICE, "Alice")["user_id"]
        _owner(client, "GET", ADMIN + "/github-member-auth")
        _owner(client, "PUT", ADMIN + "/github-member-auth", json={"enabled": True})
        _owner(client, "POST", f"{ADMIN}/accounts/{uid}/github-connection/reset", json={"confirm": True})
    m.db.close()


def scenario_memory(tmp: Path, monkeypatch):
    from test_phase7 import library_remote
    _, lib_cfg = library_remote(tmp)
    cfg = make_cfg(tmp)
    cfg.memory_library = lib_cfg
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    with TestClient(create_app(m)) as client:
        _wait(lambda: client.get(ADMIN + "/memory").json().get("profile"))
        _ok(client.put(ADMIN + "/memory/profile", json={"content": "# Agent profile\nShort answers.\n"}))
    m.db.close()


def scenario_skills(tmp: Path, monkeypatch):
    import httpx
    from harness_modules.skills.service import in_process_sandbox
    from test_skills import EXAMPLES, SKILL_MD
    cfg = make_cfg(tmp)
    cfg.skills.enabled = True
    cfg.skills.local_review = False
    cfg.skills.reviewer_base_url = "https://review.invalid/"  # the hosted review is queued, never sent
    cfg.skills.reviewer_model = "reviewer"
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    store = m.skills
    store._run_sandbox = in_process_sandbox

    async def post(client, url, **kwargs):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    session = {"id": "s", "owner_id": "owner", "app_id": "", "job_id": ""}
    asyncio.run(store.propose_from_tool({"slug": "commit-style", "title": "Commit style",
                                         "purpose": "Keep git commit messages conventional and short.",
                                         "skill_md": SKILL_MD, "examples": json.dumps(EXAMPLES)}, session))
    row = store.db.list_skill_proposals()[0]
    base = ADMIN + "/skills"
    proposal = f"{base}/proposals/{row['id']}"
    slug = base + "/commit-style"
    with TestClient(create_app(m)) as client:
        _ok(client.get(base))
        _ok(client.get(proposal))
        _ok(client.post(proposal + "/review"))
        _ok(client.post(proposal + "/reject", json={"reason": "revise"}))
        _ok(client.post(proposal + "/reopen"))
        _ok(client.post(proposal + "/install", json={"content_hash": row["content_hash"]}))
        _ok(client.post(slug + "/disable"))
        _ok(client.post(slug + "/enable"))
        _ok(client.put(slug + "/projects", json={"projects": ["scratch"]}))
        assert _ok(client.get(base + "/enabled"))[0]["slug"] == "commit-style"
        _ok(client.get(slug + "/export"))
        _rollback_target(client, store, proposal, row, slug)
        _ok(client.post(slug + "/uninstall"))
    m.db.close()


def _rollback_target(client, store, proposal, row, slug):
    """A rollback needs an older version: install a second one, then roll back to the first."""
    from test_skills import EXAMPLES, SKILL_MD
    session = {"id": "s", "owner_id": "owner", "app_id": "", "job_id": ""}
    asyncio.run(store.propose_from_tool({"slug": "commit-style", "title": "Commit style",
                                         "purpose": "Keep git commit messages conventional and shorter.",
                                         "skill_md": SKILL_MD + "\nKeep the subject under 50 characters.\n",
                                         "examples": json.dumps(EXAMPLES)}, session))
    newer = next(p for p in store.db.list_skill_proposals() if p["id"] != row["id"])
    _ok(client.post(f"{ADMIN}/skills/proposals/{newer['id']}/install", json={"content_hash": newer["content_hash"]}))
    _ok(client.post(slug + "/rollback"))


def scenario_maintenance_modules(tmp: Path, monkeypatch):
    cfg = make_cfg(tmp)
    cfg.backup = BackupConfig(enabled=True, dir=str(tmp / "backups"))
    cfg.notify.enabled = True
    cfg.notify.topic = "t"
    token = tmp / "runner.token"
    token.write_text("already-paired", encoding="utf-8")
    cfg.runners = {"mac": RunnerConfig(name="mac", token_file=str(token), min_free_gb=0)}
    m = Manager(cfg, chat=Script([Completion(content="hi")]))

    async def publish(client, payload):
        return None
    m.notifier.publish = publish
    m.hub.call = AsyncMock(return_value={"ok": True, "version": "1.2.3", "build_id": "b", "at": 1.0,
                                         "message": "updated"})
    with TestClient(create_app(m)) as client:
        _ok(client.post(ADMIN + "/notify/test", json={}))
        _ok(client.post(ADMIN + "/maintenance/backup", json={}))
        assert _ok(client.get(ADMIN + "/runners"))[0]["name"] == "mac"
        state = m.hub.state["mac"]
        state.last_seen = time.monotonic()
        state.info = {"protocol": 2}
        _ok(client.post(ADMIN + "/runners/mac/update", json={}))
        _ok(client.get(ADMIN + "/remote-control"))
    m.db.close()


def scenario_image_archive(tmp: Path, monkeypatch):
    from test_image_archive import archive_manager, completed_image
    m = archive_manager(tmp)
    m.cfg.backup.image_archive_keep_days = 30
    m.modules.get("images").archive.archive(completed_image(m, "old", created_at=1000))  # long past the 30 days
    with TestClient(create_app(m)) as client:
        phrase = _ok(client.post(ADMIN + "/maintenance/image-archive/retention/preview", json={}))["confirmation"]
        _ok(client.post(ADMIN + "/maintenance/image-archive/retention/apply", json={"confirmation": phrase}))
    m.db.close()


def _routes(*pairs: str) -> set[tuple[str, str]]:
    return {(pair.split(" ", 1)[0], pair.split(" ", 1)[1]) for pair in pairs}


SCENARIOS = {
    scenario_identity: _routes("GET /health", "GET /me", "GET /api/v1/me", "GET /api/v1/auth/session"),
    scenario_catalog: _routes("GET /projects", "POST /projects", "GET /backends", "PUT /backends/{name}",
                              "GET /smart-approvals", "PUT /smart-approvals", "GET /queue", "GET /maintenance",
                              "POST /maintenance/cleanup"),
    scenario_apps: _routes("GET /keys", f"GET {ADMIN}/apps/{{app_id}}/limits", f"PUT {ADMIN}/apps/{{app_id}}/limits",
                           f"PUT {ADMIN}/apps/{{app_id}}/retention", f"GET {ADMIN}/apps/erasures",
                           f"GET {ADMIN}/provider-credentials", f"POST {ADMIN}/provider-credentials",
                           f"GET {ADMIN}/hub-claim"),
    scenario_accounts: _routes(f"GET {ADMIN}/accounts", f"POST {ADMIN}/accounts", f"GET {ADMIN}/accounts/{{user_id}}",
                               f"PATCH {ADMIN}/accounts/{{user_id}}", f"GET {ADMIN}/accounts/audit",
                               f"GET {ADMIN}/audit"),
    scenario_sessions: _routes("PATCH /sessions/{ref}", "PUT /sessions/{ref}", "POST /sessions/{ref}/messages",
                               "POST /sessions/{ref}/rerun", "GET /sessions/{ref}/approvals",
                               "POST /sessions/{ref}/approvals/{approval_id}", "POST /sessions/{ref}/cancel"),
    scenario_pairing: _routes(f"GET {ADMIN}/pairing-requests", f"POST {ADMIN}/pairing-requests",
                              f"POST {ADMIN}/pairing-requests/{{rid}}/approve",
                              f"POST {ADMIN}/pairing-requests/{{rid}}/confirm",
                              f"POST {ADMIN}/pairing-requests/{{rid}}/deny"),
    scenario_settings: _routes(f"GET {ADMIN}/config/schema", f"GET {ADMIN}/config", f"POST {ADMIN}/config/validate",
                               f"PATCH {ADMIN}/config", f"POST {ADMIN}/config/rollback",
                               f"POST {ADMIN}/config/restart"),
    scenario_google: _routes(f"GET {ADMIN}/google-signin", f"POST {ADMIN}/accounts/{{user_id}}/google/invitation",
                             f"DELETE {ADMIN}/accounts/{{user_id}}/google/invitation",
                             f"POST {ADMIN}/accounts/{{user_id}}/google/revoke-sessions",
                             f"DELETE {ADMIN}/accounts/{{user_id}}/google"),
    scenario_github: _routes(f"GET {ADMIN}/github-member-auth", f"PUT {ADMIN}/github-member-auth",
                             f"POST {ADMIN}/accounts/{{user_id}}/github-connection/reset"),
    scenario_memory: _routes("GET /memory", "PUT /memory/profile"),
    scenario_skills: _routes("GET /skills", "GET /skills/enabled", "GET /skills/proposals/{pid}",
                             "POST /skills/proposals/{pid}/install", "POST /skills/proposals/{pid}/reject",
                             "POST /skills/proposals/{pid}/reopen", "POST /skills/proposals/{pid}/review",
                             "POST /skills/{slug}/enable", "POST /skills/{slug}/disable",
                             "PUT /skills/{slug}/projects", "POST /skills/{slug}/rollback",
                             "POST /skills/{slug}/uninstall", "GET /skills/{slug}/export"),
    scenario_maintenance_modules: _routes("POST /notify/test", "POST /maintenance/backup", "GET /runners",
                                          "POST /runners/{name}/update", "GET /remote-control"),
    scenario_image_archive: _routes("POST /maintenance/image-archive/retention/preview",
                                    "POST /maintenance/image-archive/retention/apply"),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS), ids=lambda s: s.__name__.removeprefix("scenario_"))
def test_each_documented_reply_matches_its_model(scenario, tmp_path, monkeypatch):
    with _Seen() as seen:
        scenario(tmp_path, monkeypatch)
    problems, capture.problems[:] = list(capture.problems), []
    assert not problems, "\n".join(problems)
    checked = {(method, path) for method, path, _ in seen.checked}
    missing = SCENARIOS[scenario] - checked
    assert not missing, f"documented routes this scenario never got a checked success reply from: {sorted(missing)}"


def test_the_scenarios_cover_every_documented_route(tmp_path):
    app, m = _all_modules_app(tmp_path)
    documented_routes = {(method, path) for method, path, _ in _documented_routes(app)}
    claimed = set().union(*SCENARIOS.values())
    assert documented_routes - claimed == set(), "documented routes no scenario drives"
    assert claimed - documented_routes == set(), "scenario claims for routes that are not documented"
    m.db.close()
