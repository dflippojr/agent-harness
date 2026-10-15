"""tools/stables/pin.py records the Stables release the Server deploys and tests against (#334)."""

from __future__ import annotations

import json
import subprocess

import pytest

from stables_pin import FACTS, PIN, tool

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
    return repo


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
