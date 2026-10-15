"""Issue #545: Stables is a client of the daemon, so it calls none of the daemon-administration routes.

Daemon administration is anything that changes how the daemon is configured or who can use it: daemon settings,
module switches, Apps, keys and pairing, accounts, policy, installed skills, maintenance, and runner or folder trust.
It lives in the Hub and the `harness` CLI; every route below keeps its CLI command in docs/management-parity.md.
A call made over the App API (`surface: "app"`) acts on the caller's own account, so it isn't administration.

Stables lives in dflippojr/stables (#334). These checks read the pinned release's calls and path literals
(tools/stables/api-calls.json, generated from its tag) through tests/stables_pin.py."""

from __future__ import annotations

import re

import pytest

from stables_pin import FACTS, tool
from test_management_parity import _doc_rows, _template_re

REMOVED = [
    # Settings: daemon settings.
    ("GET", "/config"), ("PATCH", "/config"), ("POST", "/config/rollback"), ("POST", "/config/restart"),
    # Settings: smart approvals mode, backend defaults, the memory library's agent profile, the test notification.
    ("PUT", "/smart-approvals"), ("PUT", "/backends/{name}"), ("PUT", "/memory/profile"), ("POST", "/notify/test"),
    # Settings: Apps, keys and pairing.
    ("GET", "/keys"), ("POST", "/keys"), ("DELETE", "/keys/{kid}"), ("POST", "/apps/{app_id}/restore"),
    ("GET", "/pairing-codes"), ("POST", "/pairing-codes"), ("DELETE", "/pairing-codes/{pid}"),
    ("GET", "/runner-pairing-codes"), ("POST", "/runner-pairing-codes"), ("DELETE", "/runner-pairing-codes/{pid}"),
    # Settings: skills management (New task still reads /skills/enabled).
    ("GET", "/skills"), ("GET", "/skills/proposals/{pid}"), ("POST", "/skills/proposals/{pid}/install"),
    ("POST", "/skills/proposals/{pid}/reject"), ("POST", "/skills/proposals/{pid}/review"),
    ("DELETE", "/skills/proposals/{pid}"), ("POST", "/skills/{slug}/enable"), ("POST", "/skills/{slug}/disable"),
    ("PUT", "/skills/{slug}/projects"), ("POST", "/skills/{slug}/rollback"), ("POST", "/skills/{slug}/uninstall"),
    # Actions: household members.
    ("GET", "/accounts"), ("POST", "/accounts"), ("PATCH", "/accounts/{user_id}"),
    ("POST", "/accounts/{user_id}/github-connection/reset"), ("GET", "/github-member-auth"),
    ("PUT", "/github-member-auth"), ("GET", "/google-signin"), ("POST", "/accounts/{user_id}/google/invitation"),
    ("DELETE", "/accounts/{user_id}/google/invitation"), ("POST", "/accounts/{user_id}/google/revoke-sessions"),
    ("DELETE", "/accounts/{user_id}/google"),
    # Actions: maintenance, runner update, adding a project to the server's catalog.
    ("POST", "/maintenance/cleanup"), ("POST", "/maintenance/image-archive/retention/preview"),
    ("POST", "/maintenance/image-archive/retention/apply"), ("POST", "/runners/{name}/update"), ("POST", "/projects"),
    # Actions: Remote Control folder trust, forget and discovery scans (start and stop stay).
    ("POST", "/remote-control/{project}/trust"), ("DELETE", "/remote-control/folders/{slug}"),
    ("POST", "/remote-control/discovery/scans"), ("GET", "/remote-control/discovery/scans/{scan_id}"),
    ("DELETE", "/remote-control/discovery/scans/{scan_id}"),
    ("POST", "/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote"),
]


def _admin_calls(calls: list[dict]) -> list[str]:
    """Each call that reaches a REMOVED route, unless it goes over the App API."""
    found = []
    for call in calls:
        if call["app_surface"]:
            continue
        pattern = re.compile(call["pattern"])
        for method, template in REMOVED:
            concrete = re.sub(r"\{[^}/]+\}", "x", template)
            if (call["methods"] is None or method in call["methods"]) and (
                    pattern.fullmatch(concrete) or _template_re(template).fullmatch(call["sample"])):
                found.append(f"{call['where']} {method} {template}")
    return found


# Paths Stables still reads with another method; every other REMOVED path must not appear in Stables at all.
STILL_READ = {"/projects", "/smart-approvals"}


def _admin_paths(paths: list[dict]) -> list[str]:
    """Path literals naming a REMOVED route, wherever they are: a page can pass a path to a helper that calls api()."""
    return [f"{path['where']} {template}" for path in paths
            for template in sorted({t for _, t in REMOVED} - STILL_READ)
            if _template_re(template).fullmatch(path["sample"])]


def test_the_pinned_release_was_read():
    assert FACTS["sources"] > 20 and len(FACTS["calls"]) > 100 and FACTS["path_literals"]


def test_web_calls_no_daemon_administration_route():
    found = _admin_calls(FACTS["calls"])
    assert not found, "Stables still calls daemon administration:\n" + "\n".join(found)


def test_web_names_no_daemon_administration_path():
    found = _admin_paths(FACTS["path_literals"])
    assert not found, "Stables still names daemon administration routes:\n" + "\n".join(found)
    assert _admin_paths(tool.paths_in("act(`/skills/proposals/${p.id}/install`, body)"))


@pytest.mark.parametrize("snippet", [
    'api("/config", { method: "PATCH", body })',
    'api(`/skills/${sk.slug}/${toggle}`, { method: "POST" })',
    'api(`/keys/${k.id}`, { method: "DELETE" })',
    'api("/accounts", { surface: "admin" })',
    'api("/projects", { method: "POST", body })',
    'api(`/remote-control/${name}/trust`, { method })',
])
def test_the_check_catches_an_admin_call(snippet):
    assert _admin_calls(tool.calls_in(snippet))


@pytest.mark.parametrize("snippet", [
    'api("/projects", { method: "POST", surface: "app", body })',  # a member's own project
    'api("/projects")', 'api("/skills/enabled")', 'api("/backends?auth=skip")', 'api("/memory")',
    'api(`/remote-control/${name}`, { method: "POST" })', 'api(`/remote-control/${name}/stop`, { method: "POST" })',
])
def test_the_check_allows_client_calls(snippet):
    assert not _admin_calls(tool.calls_in(snippet))


def test_removed_routes_keep_their_cli_commands():
    rows = _doc_rows()
    for method, template in REMOVED:
        listed = [row for row in rows if method in row["methods"] and row["endpoint"] == template]
        assert listed and all(row["cli"] for row in listed), f"{method} {template} has no CLI row"
