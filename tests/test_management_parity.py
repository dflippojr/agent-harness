"""Issue #334 stage (a): every owner action Stables offers has an owner API endpoint and a CLI command.
Issue #544: every owner API route (and so every Hub action) has a CLI command, whether or not Web calls it.

docs/management-parity.md is the inventory. These tests tie it to the `api(...)` calls of the pinned Stables release
(tools/stables/api-calls.json, generated from its tag by tools/stables/pin.py), to the routes mounted under the owner
API and to the CLI parser, so a new Stables call, endpoint or command can't drift out of parity silently."""

from __future__ import annotations

import dataclasses
import functools
import re
import shlex
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from harness import cli, modules
from harness.admin import ADMIN_PATHS, PREFIX
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager

from test_admin import LOGIN
from test_daemon import Script, make_cfg
from stables_pin import FACTS as CALLS

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "management-parity.md"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# The CLI's hand-written commands (the rest come from cli.admin_commands()) and the owner API route each one calls.
HAND_WRITTEN = {
    "list": ("GET", "/sessions"), "new": ("POST", "/sessions"), "show": ("GET", "/sessions/{ref}"),
    "transcript": ("GET", "/sessions/{ref}/transcript"), "cancel": ("POST", "/sessions/{ref}/cancel"),
    "send": ("POST", "/sessions/{ref}/messages"), "approve": ("POST", "/sessions/{ref}/approvals/{approval_id}"),
    "deny": ("POST", "/sessions/{ref}/approvals/{approval_id}"), "queue": ("GET", "/queue"),
    "watch": ("GET", "/sessions/{ref}/events"),
}

# Owner API routes deliberately not on the CLI, and why. Keep it short: anything else gets a command.
NOT_ON_CLI = {
    ("GET", "/events"): "Web's global live feed; it only tells Web to refresh lists the CLI reads on demand",
    ("GET", "/chats/{ref}/events"): "a live chat reply stream; `harness chats show <ref>` reads the reply once it ends",
    ("PUT", "/sessions/{ref}"): "older Web builds' alias of PATCH, which `harness sessions rename` calls",
    ("PUT", "/chats/{ref}"): "older Web builds' alias of PATCH, which `harness chats rename` calls",
}


def _template_re(template: str) -> re.Pattern[str]:
    return re.compile(re.sub(r"\\\{[^}/]+\\\}", "[^/]+", re.escape(template)))


def _doc_rows() -> list[dict]:
    rows = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not cells[1] or cells[1].split(",")[0].strip() not in METHODS:
            continue
        cli_cell = re.fullmatch(r"`(harness [^`]+)`", cells[3])
        rows.append({"action": cells[0], "methods": {m.strip() for m in cells[1].split(",")},
                     "endpoint": cells[2].strip("`"), "cli": cli_cell.group(1) if cli_cell else None,
                     "reason": None if cli_cell else cells[3]})
    return rows


def _client_only_keys() -> set[str]:
    text = DOC.read_text(encoding="utf-8").split("## Client-only", 1)[1]
    return set(re.findall(r"^\| `(harness\.[A-Za-z]+)` \|", text, re.M))


def _web_calls() -> list[tuple[str, re.Pattern[str], str, set[str] | None]]:
    """The pinned Stables release's api() calls (tools/stables/api-calls.json, generated from its tag)."""
    return [(call["where"], re.compile(call["pattern"]), call["sample"],
             None if call["methods"] is None else set(call["methods"])) for call in CALLS["calls"]]


def _owner_app(tmp: Path):
    """The daemon's app with every add-on module present, so every module's owner routes are mounted."""
    cfg = make_cfg(tmp)
    cfg.allowed_logins = [LOGIN]
    for field in dataclasses.fields(cfg.installed):
        setattr(cfg.installed, field.name, True)
        setattr(cfg.modules, field.name, True)
    return create_app(Manager(cfg, chat=Script([Completion(content="hi")]))), cfg


def _mounted_owner_routes(app, cfg) -> set[tuple[str, str]]:
    """(method, path) of every route the live router serves under /api/admin/v1: the ones mounted there, and the
    unversioned core and module routes admin.register mirrors there (ADMIN_PATHS, Module.admin_paths)."""
    mirrored = ADMIN_PATHS | modules.admin_paths(cfg)
    routes = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path.startswith(PREFIX):
            path = route.path[len(PREFIX):] or "/"
        elif route.path in mirrored:
            path = route.path
        else:
            continue
        routes |= {(method, path) for method in route.methods or () if method != "HEAD"}
    return routes


@pytest.fixture(scope="module")
def owner_app(tmp_path_factory):
    return _owner_app(tmp_path_factory.mktemp("parity"))


@pytest.fixture(scope="module")
def operations(owner_app) -> set[tuple[str, str]]:
    with TestClient(owner_app[0]) as client:
        ops = client.get(PREFIX).json()["operations"]
    return {(op["method"], op["path"][len(PREFIX):] or "/") for op in ops} | {("GET", "/")}


def _is_operation(operations, method: str, endpoint: str) -> bool:
    return any(m == method and _template_re(path).fullmatch(endpoint) for m, path in operations)


@functools.cache
def _parser():
    return cli._build_parser()


@functools.cache
def _cli_route(command: str) -> tuple[str, str]:
    argv = shlex.split(re.sub(r"<[^>]+>", "1", command))[1:]
    args = _parser().parse_args(argv)
    if getattr(args, "admin", None):
        return args.admin[1], args.admin[2] or "/"
    return HAND_WRITTEN[args.cmd]


def _routes_without_cli(routes: set[tuple[str, str]], allowed=NOT_ON_CLI.keys()) -> list[str]:
    """Owner API routes with no inventory row, or whose rows have no CLI command calling them."""
    rows = _doc_rows()
    missing = []
    for method, template in sorted(routes - set(allowed), key=lambda r: (r[1], r[0])):
        matches = _template_re(template)
        listed = [row for row in rows if method in row["methods"] and matches.fullmatch(row["endpoint"])]
        if not listed:
            missing.append(f"{method} {template}: no row in docs/management-parity.md")
        elif not any(row["cli"] and _cli_route(row["cli"]) == (method, row["endpoint"]) for row in listed):
            missing.append(f"{method} {template}: no CLI command calls it")
    return missing


def test_every_owner_route_has_a_cli_command(owner_app):
    routes = _mounted_owner_routes(*owner_app)
    assert len(routes) > 100
    assert routes >= NOT_ON_CLI.keys(), sorted(NOT_ON_CLI.keys() - routes)  # the allowlist names live routes only
    missing = _routes_without_cli(routes)
    assert not missing, "Owner API routes with no CLI command:\n" + "\n".join(missing)


def test_owner_routes_include_module_admin_paths(owner_app):
    routes = {path for _, path in _mounted_owner_routes(*owner_app)}
    assert modules.admin_paths(owner_app[1]) <= routes
    assert {"/gpu/{action}", "/jobs", "/images"} <= routes


def test_discovery_lists_the_mounted_owner_routes(owner_app, operations):
    assert operations == _mounted_owner_routes(*owner_app)


def test_an_owner_route_with_no_cli_command_fails(tmp_path):
    app, cfg = _owner_app(tmp_path)

    async def probe():
        return {}

    app.add_api_route(PREFIX + "/parity-probe/{pid}", probe, methods=["POST"])
    app.add_api_route(PREFIX + "/sessions/{ref}/parity-probe", probe, methods=["GET"])
    assert _routes_without_cli(_mounted_owner_routes(app, cfg)) == [
        "POST /parity-probe/{pid}: no row in docs/management-parity.md",
        "GET /sessions/{ref}/parity-probe: no row in docs/management-parity.md",
    ]
    # A row alone isn't enough: its CLI command has to call the route (the rename row lists PUT, the command PATCHes).
    assert _routes_without_cli({("PUT", "/sessions/{ref}"), ("PATCH", "/sessions/{ref}")}, allowed=()) == [
        "PUT /sessions/{ref}: no CLI command calls it"]


def test_inventory_is_well_formed():
    rows = _doc_rows()
    assert len(rows) > 100
    for row in rows:
        assert row["endpoint"].startswith("/"), row
        assert row["cli"] or row["reason"].startswith("—"), row


def test_every_web_call_is_in_the_inventory():
    rows = _doc_rows()
    calls = _web_calls()
    assert len(calls) > 100
    missing = [f"{where} {sorted(methods or ['any'])} {pattern.pattern}" for where, pattern, sample, methods in calls
               if not any((pattern.fullmatch(row["endpoint"]) or _template_re(row["endpoint"]).fullmatch(sample))
                          and (methods is None or methods & row["methods"]) for row in rows)]
    assert not missing, "Web calls not in docs/management-parity.md:\n" + "\n".join(missing)


def test_owner_endpoints_have_a_cli_command_that_calls_them(operations):
    for row in _doc_rows():
        on_owner_api = [m for m in row["methods"] if _is_operation(operations, m, row["endpoint"])]
        if row["cli"] is None:
            assert not on_owner_api, f"{row['endpoint']} is on the owner API but has no CLI command"
            continue
        assert set(on_owner_api) == row["methods"], f"{sorted(row['methods'])} {row['endpoint']} isn't all on the owner API"
        method, path = _cli_route(row["cli"])
        assert method in row["methods"] and path == row["endpoint"], (row["cli"], method, path)


def test_every_cli_admin_command_calls_an_owner_operation(operations):
    for words, method, path, _, _ in cli.admin_commands():  # the core's rows and the add-on modules'
        assert _is_operation(operations, method, path or "/"), (words, method, path)


def test_web_browser_keys_are_documented_client_only():
    keys = set(CALLS["browser_keys"])
    assert keys, "no Web browser keys found"
    assert keys <= _client_only_keys(), sorted(keys - _client_only_keys())


def test_admin_command_builds_query_body_and_upload(tmp_path):
    parser = cli._build_parser()
    assert cli.admin_request(parser.parse_args(["apps", "usage", "--days", "7"])) == (
        "GET", "/apps/usage", {"params": {"days": 7}})
    assert cli.admin_request(parser.parse_args(["jobs", "preview", "*/5 * * * *"])) == (
        "GET", "/jobs/preview", {"params": {"cron": "*/5 * * * *"}})
    assert cli.admin_request(parser.parse_args(["config", "set", "a.b=3", "c=x", "--dry-run"])) == (
        "PATCH", "/config", {"json": {"changes": {"a.b": 3, "c": "x"}, "dry_run": True}})
    assert cli.admin_request(parser.parse_args(["remote-control", "launch", "my repo/x"])) == (
        "POST", "/remote-control/my%20repo%2Fx", {"json": {}})
    assert cli.admin_request(parser.parse_args(
        ["accounts", "update", "u1", "--enabled", "false", "--set", "max_running=2"])) == (
        "PATCH", "/accounts/u1", {"json": {"enabled": False, "max_running": 2}})
    mask = tmp_path / "mask.png"
    mask.write_bytes(b"png")
    method, path, kwargs = cli.admin_request(parser.parse_args(["images", "edit", "i1", "a cat", str(mask),
                                                                "--feather", "4"]))
    assert (method, path) == ("POST", "/images/i1/edit")
    assert kwargs == {"files": {"mask": ("mask.png", b"png")}, "data": {"prompt": "a cat", "feather": "4"}}
    with pytest.raises(SystemExit):
        parser.parse_args(["github-member-auth", "set", "maybe"])


def test_admin_command_prints_the_api_response(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".agent-harness"
    monkeypatch.setattr(cli, "HARNESS_HOME", home)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", home / "client" / "config.json")
    monkeypatch.delenv("HARNESS_URL", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    seen = []

    def fake(method, path, **kwargs):
        seen.append((method, path, kwargs))
        return {"state": "held"} if method == "POST" else ""

    monkeypatch.setattr(cli, "api", fake)
    monkeypatch.setattr("sys.argv", ["harness", "gpu", "pause", "--duration-seconds", "1800"])
    assert cli.main() == 0
    assert seen == [("POST", "/gpu/pause", {"json": {"duration_seconds": 1800}})]
    assert '"state": "held"' in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["harness", "keys", "list"])
    assert cli.main() == 0
    assert capsys.readouterr().out.strip() == "ok"
