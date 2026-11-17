"""tools/stables/pin.py records the Stables release the Server deploys and tests against (#334)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stables_pin import FACTS, PIN, tool

COMPAT = Path(__file__).resolve().parents[1] / "harness" / "compat.py"

CLIENT = 'export const WEB_BUILD_ID = "2026.1.1.1";\nconst k = "harness.daemonUrl";\n'
APP = '''import { api } from "./client.mjs";
api("/sessions");
api(`/sessions/${id}/cancel`, { method: "POST" });
api("/projects", { method: "POST", surface: "app", body });
api(path, { method: verb });
go("/skills/enabled?x=1");
'''
SNIPPETS = '''export const SNIPPET_LANGUAGES = {
  python: { label: "Python", aliases: ["py", "python3"] },
  bash: { label: "Bash", aliases: ["sh"] },
};
'''


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    repo = tmp_path / "stables"
    (repo / "public" / "lib").mkdir(parents=True)
    (repo / "public" / "client.mjs").write_text(CLIENT, encoding="utf-8")
    (repo / "public" / "app.js").write_text(APP, encoding="utf-8")
    (repo / "public" / "lib" / "snippets.mjs").write_text(SNIPPETS, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "release")
    monkeypatch.setattr(tool, "PIN", tmp_path / "pin.json")
    monkeypatch.setattr(tool, "CALLS", tmp_path / "api-calls.json")
    # pin() writes the build id into compat.py: point it at a copy, never at the real file other tests import.
    (tmp_path / "compat.py").write_bytes(COMPAT.read_bytes())
    monkeypatch.setattr(tool, "COMPAT", tmp_path / "compat.py", raising=False)
    return repo


def _release(repo, client: str, tag: str):
    (repo / "public" / "client.mjs").write_text(client, encoding="utf-8")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-am", tag)
    _git(repo, "tag", tag)


def test_call_list_records_calls_paths_keys_build_and_languages(checkout):
    facts = tool.call_list(checkout)
    assert facts["build_id"] == "2026.1.1.1"
    assert facts["browser_keys"] == ["harness.daemonUrl"]
    assert facts["snippet_languages"] == {"python": {"label": "Python", "aliases": ["py", "python3"]},
                                          "bash": {"label": "Bash", "aliases": ["sh"]}}
    calls = {(c["sample"], tuple(c["methods"] or ()), c["app_surface"]) for c in facts["calls"]}
    assert calls == {("/sessions", ("GET",), False), ("/sessions/x/cancel", ("POST",), False),
                     ("/projects", ("POST",), True)}
    assert all(c["where"].startswith("public/") for c in facts["calls"])
    assert {p["sample"] for p in facts["path_literals"]} >= {"/sessions", "/skills/enabled", "/projects"}
    assert tool.calls_in('api(p, { method })') == []  # no path literal, nothing to record


def test_pin_needs_a_release_tag_then_check_passes(checkout):
    with pytest.raises(SystemExit, match="release tag"):
        tool.pin(checkout)
    _git(checkout, "tag", "v1.2.3")
    record = tool.pin(checkout)
    assert record["tag"] == "v1.2.3" and record["repository"] == "dflippojr/stables"
    assert json.loads(tool.PIN.read_text(encoding="utf-8")) == record
    assert tool.check(checkout) == []
    assert tool.main(["--check", str(checkout)]) == 0


def test_check_reports_another_commit_and_a_stale_call_list(checkout, capsys):
    _git(checkout, "tag", "v1.0.0")
    assert tool.main([str(checkout)]) == 0
    (checkout / "public" / "app.js").write_text(APP + 'api("/new-route");\n', encoding="utf-8")
    _git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-am", "next")
    problems = tool.check(checkout)
    assert len(problems) == 2 and "pins v1.0.0" in problems[0] and "api-calls.json" in problems[1]
    assert tool.main(["--check", str(checkout)]) == 1
    assert "pins v1.0.0" in capsys.readouterr().err
    assert tool.main([]) == 2


def test_the_committed_pin_is_a_release():
    assert PIN["repository"] == "dflippojr/stables"
    assert len(PIN["commit"]) == 40 and PIN["tag"].startswith("v")
    assert FACTS["calls"] and FACTS["build_id"]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_pin_writes_the_release_build_id_into_compat(checkout, tmp_path, newline):
    """A re-pin carries the release's build id into compat.py (#996): one source, no hand edit. Only the id's value
    changes, line endings included (a Windows checkout has CRLF)."""
    original = COMPAT.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline)
    compat = tmp_path / "compat.py"
    compat.write_bytes(original)
    old = b'WEB_BUILD_ID = "' + FACTS["build_id"].encode() + b'"'
    assert original.count(old) == 1
    _release(checkout, 'export const WEB_BUILD_ID = "0.1.3";\n', "v0.1.3")
    assert tool.main([str(checkout)]) == 0
    assert json.loads(tool.CALLS.read_text(encoding="utf-8"))["build_id"] == "0.1.3"
    assert compat.read_bytes() == original.replace(old, b'WEB_BUILD_ID = "0.1.3"')
    assert tool.check(checkout) == []


@pytest.mark.parametrize("client, why", [
    ("const k = 1;\n", "no WEB_BUILD_ID"),
    ('export const WEB_BUILD_ID = "0.1.3 beta";\n', "not a build id"),
    ('export const WEB_BUILD_ID = "0.1.3\\\\";\n', "not a build id"),
    ('export const WEB_BUILD_ID = "";\n', "no WEB_BUILD_ID"),
])
def test_pin_refuses_a_missing_or_unsafe_build_id_and_writes_nothing(checkout, tmp_path, client, why):
    """compat.py is Python: a release whose id is missing or could break out of the string literal is refused
    before pin.json, api-calls.json or compat.py changes."""
    _release(checkout, client, "v0.1.3")
    with pytest.raises(SystemExit, match=why):
        tool.pin(checkout)
    assert not tool.PIN.exists() and not tool.CALLS.exists()
    assert (tmp_path / "compat.py").read_bytes() == COMPAT.read_bytes()


def test_pin_refuses_a_compat_without_one_build_id_line(checkout, tmp_path):
    compat = tmp_path / "compat.py"
    compat.write_text('RELEASE = "0.9.0"\n', encoding="utf-8")
    _git(checkout, "tag", "v0.1.3")
    with pytest.raises(SystemExit, match="WEB_BUILD_ID"):
        tool.pin(checkout)
    assert not tool.PIN.exists() and compat.read_text(encoding="utf-8") == 'RELEASE = "0.9.0"\n'


def test_check_reports_a_compat_build_id_that_differs_from_the_release(checkout, tmp_path, capsys):
    _git(checkout, "tag", "v1.0.0")
    assert tool.main([str(checkout)]) == 0
    compat = tmp_path / "compat.py"
    compat.write_bytes(compat.read_bytes().replace(b'WEB_BUILD_ID = "2026.1.1.1"', b'WEB_BUILD_ID = "2026.1.1.2"'))
    assert tool.check(checkout) == ["compat.py's WEB_BUILD_ID is 2026.1.1.2, but the release's is 2026.1.1.1: "
                                    "run python tools/stables/pin.py <checkout>"]
    assert tool.main(["--check", str(checkout)]) == 1
    assert "WEB_BUILD_ID is 2026.1.1.2" in capsys.readouterr().err
