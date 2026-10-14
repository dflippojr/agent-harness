"""Issue #334 stage (d): Stables is a standalone client of the documented API, and the daemon does not depend on it.

Stables (`stables/public`) imports nothing from outside its own tree except the API contract types, reaches the daemon
only through its transport (`client.mjs`, `lib/stream.mjs`) and the documented routes, and the server names the
client's files in one place, `harness/stables_shell.py`, so it runs the API alone when the shell is absent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness import stables_shell
from tests.test_api import make_client

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "stables" / "public"
CONTRACTS = ROOT / "stables" / "types"
OPENAPI = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
SERVER_TREES = ("harness", "harness_modules", "sdk")

RUNTIME_IMPORT = re.compile(r"""(?:^|[;\s])(?:import|export)\b[^'"`;]*?\bfrom\s*['"]([^'"]+)['"]"""
                            r"""|\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)|^\s*import\s*['"]([^'"]+)['"]""", re.M)
TYPE_IMPORT = re.compile(r"""@import\s*\{[^}]*\}\s*from\s*['"]([^'"]+)['"]""")
NETWORK = re.compile(r"\bfetch\(|\bEventSource\(|\bXMLHttpRequest\b|\bWebSocket\(|\bsendBeacon\(")
# The transport, and the service worker, which only refreshes the shell's own files.
NETWORK_FILES = {"client.mjs", "lib/stream.mjs", "sw.js"}


def _sources() -> list[Path]:
    return sorted([*SHELL.rglob("*.mjs"), *SHELL.rglob("*.js")])


def _rel(path: Path) -> str:
    return path.relative_to(SHELL).as_posix()


def _resolve(source: Path, spec: str) -> Path:
    return (SHELL / spec.lstrip("/")).resolve() if spec.startswith("/") else (source.parent / spec).resolve()


def _strip_line_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("//", "*", "/*")))


def test_shell_is_found_in_the_checkout(monkeypatch):
    monkeypatch.delenv(stables_shell.ENV, raising=False)
    assert stables_shell.locate() == SHELL.resolve()
    assert {"/index.html", "/sw.js", "/manifest.webmanifest", "/client.mjs"} <= stables_shell.top_files(SHELL)


def test_runtime_imports_stay_inside_the_shell():
    outside = []
    for source in _sources():
        for match in RUNTIME_IMPORT.finditer(_strip_line_comments(source.read_text(encoding="utf-8"))):
            spec = next(group for group in match.groups() if group)
            target = _resolve(source, spec)
            if not spec.startswith((".", "/")) or not target.is_relative_to(SHELL.resolve()) or not target.is_file():
                outside.append(f"{_rel(source)}: {spec}")
    assert not outside, f"Stables must load only its own files: {outside}"


def test_type_imports_reach_only_the_shell_or_the_api_contract():
    outside = []
    for source in _sources():
        for spec in TYPE_IMPORT.findall(source.read_text(encoding="utf-8")):
            target = _resolve(source, spec)
            allowed = target.is_relative_to(SHELL.resolve()) or target.parent == CONTRACTS.resolve()
            if not allowed:
                outside.append(f"{_rel(source)}: {spec}")
    assert not outside, f"type-only imports may name the shell or stables/types contracts: {outside}"


def test_only_the_transport_opens_connections():
    callers = sorted({_rel(source) for source in _sources()
                      if NETWORK.search(_strip_line_comments(source.read_text(encoding="utf-8")))})
    assert set(callers) <= NETWORK_FILES, f"route network calls through client.mjs: {sorted(set(callers) - NETWORK_FILES)}"


def test_transport_uses_only_the_versioned_surfaces_and_documented_routes():
    client = (SHELL / "client.mjs").read_text(encoding="utf-8")
    assert 'const prefixes = { app: "/api/v1", admin: "/api/admin/v1" };' in client
    unprefixed = set()
    for source in _sources():
        unprefixed |= set(re.findall(r"""request\(\s*['"]([^'"]+)['"]\s*,\s*\{[^}]*surface:\s*['"]{2}""",
                                     source.read_text(encoding="utf-8")))
    assert unprefixed == {"/health"}
    assert unprefixed <= set(OPENAPI["paths"])


def test_server_names_the_client_only_in_its_locator():
    named, importers = [], set()
    for tree in SERVER_TREES:
        for source in (ROOT / tree).rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            rel = source.relative_to(ROOT).as_posix()
            if rel != "harness/stables_shell.py" and re.search(r"""['"]stables['"/]|/\s*['"]web['"]""", text):
                named.append(rel)
            if re.search(r"\bstables_shell\b", text) and rel != "harness/stables_shell.py":
                importers.add(rel)
    assert not named, f"only harness/stables_shell.py may name the client's directory: {named}"
    assert importers == {"harness/api.py", "harness/google_signin.py", "harness/fake_instance.py"}


def test_daemon_runs_the_api_without_the_shell(tmp_path, monkeypatch):
    empty = tmp_path / "no-shell"
    empty.mkdir()
    monkeypatch.setenv(stables_shell.ENV, str(empty))
    assert stables_shell.locate() is None and stables_shell.top_files(None) == frozenset()
    client, manager, _ = make_client(tmp_path, [])
    with client:
        assert client.get("/health").status_code == 200
        for path in ("/", "/sw.js", "/manifest.webmanifest", "/static/app.js", "/client.mjs"):
            assert client.get(path).status_code == 404, path
        assert manager.google_signin.pre_auth_allowed("GET", "/client.mjs") is False


@pytest.mark.parametrize("path,body", [("/", "custom shell"), ("/static/extra.js", "extra"), ("/extra.js", "extra")])
def test_daemon_serves_the_shell_named_by_the_environment(tmp_path, monkeypatch, path, body):
    shell = tmp_path / "shell"
    shell.mkdir()
    (shell / "index.html").write_text("<!doctype html><p>custom shell</p>", encoding="utf-8")
    (shell / "extra.js").write_text("// extra\n", encoding="utf-8")
    monkeypatch.setenv(stables_shell.ENV, str(shell))
    client, manager, _ = make_client(tmp_path, [])
    with client:
        response = client.get(path)
        assert response.status_code == 200 and body in response.text
        assert manager.google_signin.pre_auth_allowed("GET", "/extra.js")
