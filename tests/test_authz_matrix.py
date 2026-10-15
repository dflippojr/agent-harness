"""One route x caller authorization matrix (#707).

A single household app answers every synthetic caller on every route family that tells callers apart: the owner
API under /api/admin/v1, the legacy unversioned owner routes, the /api/v1 app contract, the members-only routes
and the owner status paths. Each cell pins the exact status and the detail fragment that names the refusal, and a
refused mutation must leave the database as it found it. This one table replaces the per-feature "this caller is
refused" tests that each built their own app.

Synthetic data only: example logins, made-up origins, keys minted here."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness import local_owner, secret_scan
from harness.admin import ADMIN_SCOPE, OWNER_KIND, PREFIX
from harness.config import GuestAccess
from harness_modules.local_model import resources, routes
from harness_modules.local_model.warmup import READY, ModelWarmer

from test_household import ALICE, BOB, GUEST, H, OWNER, create_member, household
from test_hub_claim import HUB_ORIGIN, _bearer, _claim

STRANGER = "stranger@example.com"
ELSEWHERE = "https://other.example"
ALL_APP_SCOPES = ["sessions", "sessions:all", "approvals", "images", "inference", "remote_control"]

# The nine owner status routes of test_access_checks.py, and what a member (not the owner) gets from each.
OWNER_STATUS_PATHS = {
    "/memory": (403, "memory library"), "/pairing-codes": (403, "members cannot manage"),
    "/runner-pairing-codes": (403, "members cannot manage"), "/runners": (403, "Mac or other runners"),
    "/backends": (200, ""), "/gpu": (403, "GPU or machine settings"), "/resources": (403, "GPU or machine settings"),
    "/resources/diagnostics": (403, "GPU or machine settings"), "/models/status": (200, ""),
}

OWNERISH = ("owner", "local", "ownerkey", "hub")           # the four ways to be the owner
UNAUTHENTICATED = ("none", "wrongtok", "emptytok", "unknown")
MEMBERS = ("alice", "bob")
NOT_OWNER_KEYS = ("app", "device", "noadmin", "app+login", "device+login", "noadmin+login")
TABLES = ("api_keys", "sessions", "accounts", "google_link_invitations", "web_sessions", "pairing_requests",
          "pairing_codes", "runner_pairing_codes", "github_connections")


class Route(SimpleNamespace):
    """One route: its family (which callers it admits), the status an admitted caller gets, and exceptions."""

    def __init__(self, family, method, path, ok=200, body=None, member=(200, ""), override=None, mutates=False,
                 role=False, origin=False):
        super().__init__(family=family, method=method, path=path, ok=ok, body=body, member=member,
                         override=override or {}, mutates=mutates, role=role, origin=origin)

    def __repr__(self):
        return f"{self.method} {self.path}"


def routes_table(alice_id: str, bob_id: str) -> list[Route]:
    owner_api = [
        Route("owner", "GET", PREFIX), Route("owner", "GET", f"{PREFIX}/accounts"),
        Route("owner", "GET", f"{PREFIX}/keys"), Route("owner", "GET", f"{PREFIX}/gpu"),
        Route("owner", "GET", f"{PREFIX}/jobs", ok=400), Route("owner", "GET", f"{PREFIX}/sessions"),
        Route("owner", "GET", f"{PREFIX}/audit"), Route("owner", "GET", f"{PREFIX}/google-signin"),
        Route("owner", "GET", f"{PREFIX}/github-member-auth"),
        Route("owner", "POST", f"{PREFIX}/pairing-requests/preview", body={"scopes": ["approvals"]}, origin=True),
        # Mutations: a refused caller changes nothing; the Hub key may not mint another owner key.
        Route("owner", "POST", f"{PREFIX}/sessions", ok=201, body={"prompt": "x"}, mutates=True),
        Route("owner", "POST", f"{PREFIX}/gpu/pause", ok=400, mutates=True),
        Route("owner", "POST", f"{PREFIX}/keys", ok=201, mutates=True, override={"hub": (403, "")},
              body={"name": "k", "kind": OWNER_KIND, "scopes": [ADMIN_SCOPE]}),
        Route("owner", "PUT", f"{PREFIX}/memory/profile", ok=400, body={"content": "x"}, mutates=True),
        Route("owner", "PATCH", f"{PREFIX}/accounts/{alice_id}", body={"max_running": 2}, mutates=True),
        Route("owner", "PUT", f"{PREFIX}/github-member-auth", body={"enabled": False}, mutates=True),
        Route("owner", "POST", f"{PREFIX}/accounts/{bob_id}/google/invitation", ok=201, mutates=True),
        Route("owner", "POST", f"{PREFIX}/accounts/{bob_id}/google/revoke-sessions", mutates=True),
    ]
    legacy = [
        Route("legacy_any", "GET", "/me", role=True), Route("legacy_any", "GET", "/sessions"),
        Route("legacy_owner", "GET", "/keys", member=(403, "members cannot manage")),
        Route("legacy_owner", "POST", "/keys", ok=201, mutates=True, member=(403, "members cannot manage"),
              override={"hub": (403, "")}, body={"name": "k", "kind": OWNER_KIND, "scopes": [ADMIN_SCOPE]}),
        Route("legacy_owner", "POST", "/sessions/s1/approvals/a1", ok=404, body={"decision": "approve"},
              mutates=True, member=(404, "no session matches")),
    ]
    status = [Route("legacy_owner", "GET", path, member=member) for path, member in OWNER_STATUS_PATHS.items()]
    app_contract = [
        Route("api_v1", "GET", "/api/v1/sessions"),
        Route("member_only", "GET", "/api/v1/me/api-keys"),
        Route("member_only", "PUT", "/api/v1/me/api-keys/claude", body={"key": "short"}, mutates=True,
              member=(400, "does not look like")),
        Route("member_only", "GET", "/api/v1/me/github-connection"),
        Route("member_only", "POST", "/api/v1/me/github-connection/connect", mutates=True,
              member=(409, "GitHub sign-in")),
    ]
    # Routes a member must be refused (the loop test_household's two-member test used to carry); only members
    # are checked, since the other callers' answers on a feature that is off in this app say nothing about authz.
    members_refused = [Route("members_refused", "GET", path) for path in (
        f"{PREFIX}/maintenance", "/jobs", "/images", "/templates", "/metrics", "/smart-approvals", "/skills")]
    return owner_api + legacy + status + app_contract + members_refused


def expect(route: Route, caller: str):
    """(status, detail fragment) the route gives the caller, or None when the matrix does not pin that cell."""
    if caller in route.override:
        return route.override[caller]
    family = route.family
    if route.origin and caller in ("hub@origin", "hub@elsewhere", "owner@elsewhere"):
        return {"hub@origin": (route.ok, ""), "hub@elsewhere": (403, ""), "owner@elsewhere": (403, "")}[caller]
    if caller in ("hub@origin", "hub@elsewhere", "owner@elsewhere"):
        return None
    if family == "members_refused":
        return (403, "") if caller in MEMBERS else None
    if family == "owner":
        if caller in OWNERISH:
            return route.ok, ""
        if caller in UNAUTHENTICATED:
            return 401, "owner token"
        if caller in MEMBERS:
            return 403, "members cannot use the owner API"
        if caller == "guest":
            return 403, "demo access"
        if caller == "stranger":
            return 403, ""
        return 403, "app tokens cannot use the owner API"
    if caller in UNAUTHENTICATED:
        return 401, "owner token"
    if caller.endswith("+login"):
        return None  # a login with a stray bearer is the login: only the owner API refuses on the bearer
    if caller == "stranger":
        return 403, ""
    if caller == "guest":
        return (200, "") if family == "legacy_any" else (403, "demo access")
    if family == "legacy_any":
        return (200, "") if caller in (*OWNERISH, *MEMBERS) else (401, "local owner token")
    if family == "legacy_owner":
        if caller in OWNERISH:
            return route.ok, ""
        return route.member if caller in MEMBERS else (401, "local owner token")
    if family == "api_v1":
        if caller == "device":
            return 403, "'sessions' scope"
        return (200, "") if caller in (*OWNERISH, *MEMBERS, "app") else None
    if family == "member_only":
        if caller in MEMBERS:
            return route.member
        if caller == "device":
            return 403, "'sessions' scope"
        return (403, "signed-in household member") if caller in (*OWNERISH, "app", "noadmin") else None
    raise AssertionError(family)


@pytest.fixture(scope="module")
def matrix(tmp_path_factory, gitleaks_tools):
    patcher = pytest.MonkeyPatch()
    patcher.setattr(secret_scan, "tools_dir", lambda cfg: gitleaks_tools)

    async def awake(self, model):
        return READY

    async def status(*_args):
        return {"state": "unloaded"}

    patcher.setattr(ModelWarmer, "state", awake)
    patcher.setattr(routes, "_resources_status", status)
    patcher.setattr(resources, "diagnostics", status)
    try:
        client, m = household(tmp_path_factory.mktemp("authz"),
                              guests=[GuestAccess(login=GUEST, until="2099-01-01T00:00:00+00:00")])
        m._spawn = lambda sid: None  # sessions are created, not run, so no background run touches a snapshot
        with client:
            alice, bob = create_member(client, ALICE, "Alice"), create_member(client, BOB, "Bob")
            # The kinds and prefixes of each credential are part of the contract (#24).
            app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ALL_APP_SCOPES}).json()
            device = client.post("/keys", json={"name": "zed"}).json()
            # Legacy pre-#91 display names remain valid owner credentials.
            owner = client.post("/keys", json={"name": "control-center", "kind": OWNER_KIND,
                                               "scopes": [ADMIN_SCOPE]}).json()
            assert (app["kind"], device["kind"], owner["kind"]) == ("app", "device", OWNER_KIND)
            assert (app["key"][:3], device["key"][:3], owner["key"][:3]) == ("ha-", "hk-", "ho-")
            assert owner["scopes"] == ADMIN_SCOPE
            for kind in ("app", "device"):  # only an owner key can carry the admin scope
                assert client.post("/keys", json={"name": "nope", "kind": kind, "scopes": [ADMIN_SCOPE]}).status_code == 400
            _, noadmin = m.db.main.create_api_key("owner-without-admin", "sessions", OWNER_KIND)
            hub, _, _ = _claim(client, m)
            client.local_owner = False  # each caller sends only the headers it is given
            callers = {
                "owner": H(OWNER), "local": {local_owner.HEADER: m.local_owner_token}, "none": {},
                "wrongtok": {local_owner.HEADER: "not-the-token"}, "emptytok": {local_owner.HEADER: ""},
                "alice": H(ALICE), "bob": H(BOB), "guest": H(GUEST), "stranger": H(STRANGER),
                "app": _bearer(app["key"]), "device": _bearer(device["key"]), "ownerkey": _bearer(owner["key"]),
                "hub": _bearer(hub), "noadmin": _bearer(noadmin), "unknown": _bearer("ho-made-up"),
                # A bearer token wins over a Tailscale login the same request carries.
                "app+login": {**H(OWNER), **_bearer(app["key"])}, "device+login": {**H(OWNER), **_bearer(device["key"])},
                "noadmin+login": {**H(OWNER), **_bearer(noadmin)},
                # Browser origins: the Hub key is bound to the origin it claimed from, and a login is not valid
                # on a cross-origin request.
                "hub@origin": _bearer(hub, HUB_ORIGIN), "hub@elsewhere": _bearer(hub, ELSEWHERE),
                "owner@elsewhere": {**H(OWNER), "Origin": ELSEWHERE},
            }
            yield SimpleNamespace(client=client, m=m, callers=callers, app=app, ids=(alice["user_id"], bob["user_id"]))
    finally:
        patcher.undo()  # a failed setup must not leave patches on for the next module


def snapshot(m) -> dict:
    conn = m.db.main.conn
    state = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in TABLES}
    for row in state["api_keys"] + state["accounts"]:  # a presented credential's last-use stamp is not a change
        row.pop("last_used_at", None)
        row.pop("last_activity_at", None)
    state["session_ids"] = sorted(m.db.find_session_ids(""))  # Web's store and every App's store
    return state


ROUTE_IDS = [repr(r) for r in routes_table("u-a", "u-b")]


@pytest.mark.parametrize("index", range(len(ROUTE_IDS)), ids=ROUTE_IDS)
def test_every_caller_gets_its_exact_answer_and_a_refusal_changes_nothing(matrix, index):
    route = routes_table(*matrix.ids)[index]
    client, m = matrix.client, matrix.m
    checked = 0
    for name, headers in matrix.callers.items():
        cell = expect(route, name)
        if cell is None:
            continue
        status, fragment = cell
        before = snapshot(m) if route.mutates and status >= 400 else None
        response = client.request(route.method, route.path, headers=headers, json=route.body)
        where = f"{route} as {name}: {response.status_code} {response.text[:200]}"
        assert response.status_code == status, where
        if status >= 400 and status != 404:
            detail = response.json().get("detail") or response.json()["error"]["message"]
            assert fragment in str(detail), where
        if before is not None:
            after = snapshot(m)
            assert after == before, f"{route} as {name} changed state: " + str(
                {t: (before[t], after[t]) for t in before if before[t] != after[t]})
        if route.role and status == 200:
            assert response.json()["role"] == {**dict.fromkeys(OWNERISH, "owner"), "alice": "member", "bob": "member",
                                               "guest": "guest"}[name], where
        checked += 1
    assert checked >= 2


def test_the_owner_api_has_no_delete_for_the_audit_trail(matrix):
    assert matrix.client.delete(f"{PREFIX}/audit", headers=H(OWNER)).status_code in (404, 405)


def test_an_app_key_still_works_on_the_public_contract_and_an_owner_key_on_the_owner_api(matrix):
    client, app, owner = matrix.client, matrix.app, matrix.callers["ownerkey"]
    created = client.post("/api/v1/sessions", headers=_bearer(app["key"]), json={"prompt": "app work"})
    assert created.status_code == 201
    listed = client.get("/api/v1/sessions", headers=_bearer(app["key"])).json()
    assert created.json()["id"] in [s["id"] for s in listed]
    made = client.post(f"{PREFIX}/sessions", headers=owner, json={"prompt": "from the owner API"})
    assert made.status_code == 201
    assert client.get(f"{PREFIX}/sessions/{made.json()['id']}", headers=owner).json()["id"] == made.json()["id"]
    minted = client.post(f"{PREFIX}/keys", headers=owner,
                         json={"name": "another-owner", "kind": OWNER_KIND, "scopes": [ADMIN_SCOPE]})
    assert minted.status_code == 201 and minted.json()["kind"] == OWNER_KIND
