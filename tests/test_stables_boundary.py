"""Issue #334: Stables is a standalone client of the documented API, and the daemon does not depend on it.

Stables lives in dflippojr/stables, which checks the client's side of the boundary (`tests/boundary.mjs` there: it
loads only its own files and the API contract types, and only its transport opens connections). Here the Server
names the client's files in one place, `harness/stables_shell.py`, and runs the API alone when no shell is found.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness import stables_shell
from tests.test_api import make_client

ROOT = Path(__file__).resolve().parents[1]
SERVER_TREES = ("harness", "harness_modules", "sdk")


def _shell(path: Path) -> Path:
    path.mkdir(parents=True)
    for name in ("index.html", "sw.js", "manifest.webmanifest", "client.mjs"):
        (path / name).write_text(f"stand-in {name}\n", encoding="utf-8")
    return path


def test_shell_is_found_in_the_checkout_without_the_variable(tmp_path, monkeypatch):
    """A Stables clone at `<checkout>/stables` (ignored by git) is served when HARNESS_STABLES_DIR is unset."""
    shell = _shell(tmp_path / "stables" / "public")
    monkeypatch.delenv(stables_shell.ENV, raising=False)
    monkeypatch.setattr(stables_shell, "DEFAULT", shell)
    assert stables_shell.locate() == shell.resolve()
    assert {"/index.html", "/sw.js", "/manifest.webmanifest", "/client.mjs"} <= stables_shell.top_files(shell)


def test_checkout_default_is_the_ignored_stables_clone():
    assert stables_shell.DEFAULT == ROOT / "stables" / "public"
    assert "/stables/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_variable_wins_over_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(stables_shell, "DEFAULT", _shell(tmp_path / "checkout"))
    named = _shell(tmp_path / "named")
    monkeypatch.setenv(stables_shell.ENV, f"  {named}  ")
    assert stables_shell.locate() == named.resolve()


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
