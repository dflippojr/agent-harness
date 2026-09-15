"""Phase 8c: quotes in final answers must come from something the agent read, and GitHub pages read through the API."""

from __future__ import annotations

import asyncio
import base64

import httpx

from harness.config import WebConfig
from harness.grounding import ungrounded_quotes
from harness.llm import Completion
from harness.manager import Manager
from harness.web_tools import WebTools, github_sources
from test_daemon import Script, call, events, make_cfg, wait_status
from test_phase6 import resolver

PAGE = "SearXNG is free software released under the GNU Affero General Public License, version 3 or later."


def test_ungrounded_quotes_matching():
    sources = [PAGE, "d f f = 2048 and the model uses **six layers** in the encoder stack"]
    assert ungrounded_quotes('It says "released under the GNU Affero General Public License".', sources) == []
    # spacing, case and Markdown inside the quote don't matter; ellipses need every part
    assert ungrounded_quotes('"the model uses six layers in the encoder stack"', sources) == []
    assert ungrounded_quotes('"SearXNG is free software … General Public License, version 3"', sources) == []
    assert ungrounded_quotes('"SearXNG is free software … licensed under the MIT license terms"', sources) == [
        "SearXNG is free software … licensed under the MIT license terms"]
    # short quotes (names, terms) aren't checked
    assert ungrounded_quotes('Licensed as "AGPL-3.0".', sources) == []
    assert ungrounded_quotes("no quotes at all", []) == []


def test_final_answer_quote_gets_one_fix(tmp_path):
    script = Script([
        Completion(content='The license is AGPL. The README says "SearXNG is licensed under the strong AGPL terms".'),
        lambda msgs: Completion(content="Fixed: " + ("yes" if "don't appear word for word" in msgs[-1]["content"]
                                                     else "no") + ' "released under the GNU Affero General Public License"'),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = m.create(f"What license? Source text: {PAGE}")
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"].startswith("Fixed: yes")
        assert events(m, s["id"], "quote_check")[0]["quotes"] == ["SearXNG is licensed under the strong AGPL terms"]
        assert events(m, s["id"], "ungrounded_quotes") == []
        assert "ungrounded_quotes" not in events(m, s["id"], "run_finished")[-1]
        await m.stop()
    asyncio.run(body())


def test_finish_tool_quote_is_flagged_after_second_try(tmp_path):
    made_up = "our tests show a 40 percent speedup on every workload"
    script = Script([
        Completion(tool_calls=[call("finish", 0, answer=f'Done. The docs say "{made_up}".')]),
        Completion(tool_calls=[call("finish", 1, answer=f'Done. The docs say "{made_up}".')]),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = m.create("summarize the docs")
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())
        results = events(m, s["id"], "tool_result")
        assert results[0]["ok"] is False and "Not finished yet" in results[0]["output"]
        assert results[1]["ok"] is True
        assert len(events(m, s["id"], "quote_check")) == 1
        assert events(m, s["id"], "ungrounded_quotes")[0]["quotes"] == [made_up]
        assert events(m, s["id"], "run_finished")[-1]["ungrounded_quotes"] == [made_up]
        assert m.db.get_session(s["id"])["run"]["ungrounded_quotes"] == [made_up]
        await m.stop()
    asyncio.run(body())


def test_quote_check_can_be_turned_off(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.web.quote_check = False
    script = Script([Completion(content='It says "this sentence appears nowhere in any source text".')])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("anything")
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert events(m, s["id"], "quote_check") == [] and events(m, s["id"], "ungrounded_quotes") == []
        await m.stop()
    asyncio.run(body())


# GitHub pages through the API


def test_github_sources_urls():
    assert github_sources("https://github.com/searxng/searxng")["urls"] == {
        "meta": "https://api.github.com/repos/searxng/searxng",
        "readme": "https://api.github.com/repos/searxng/searxng/readme",
        "contents": "https://api.github.com/repos/searxng/searxng/contents"}
    tree = github_sources("https://github.com/o/r/tree/main/tools/server")
    assert tree["path"] == "tools/server" and tree["urls"]["contents"].endswith("/contents/tools/server?ref=main")
    blob = github_sources("https://github.com/o/r.git/blob/v1.2/src/app.py")
    assert blob["kind"] == "file" and blob["urls"]["raw"] == "https://raw.githubusercontent.com/o/r/v1.2/src/app.py"
    for other in ("https://github.com/o", "https://github.com/topics/python", "https://github.com/o/r/issues/5",
                  "https://gitlab.com/o/r", "https://example.com/o/r"):
        assert github_sources(other) is None


def _github_handler(meta_status: int = 200):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.headers["host"], request.url.path
        seen.append(f"{host}{path}")
        if host == "api.github.com" and path == "/repos/o/r":
            return httpx.Response(meta_status, json={
                "full_name": "o/r", "description": "A demo tool.", "license": {"name": "MIT License", "spdx_id": "MIT"},
                "language": "Python", "stargazers_count": 12, "default_branch": "main", "topics": ["demo"]})
        if host == "api.github.com" and path == "/repos/o/r/readme":
            return httpx.Response(200, json={"path": "README.md", "encoding": "base64",
                                             "content": base64.b64encode(b"# Demo\n\nInstall with pip.").decode()})
        if host == "api.github.com" and path == "/repos/o/r/contents":
            return httpx.Response(200, json=[{"name": "src", "type": "dir"}, {"name": "setup.py", "type": "file"}])
        if host == "github.com":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text="<html><title>o/r</title><body><nav>Sign in</nav><p>" + "HTML page. " * 40
                                       + "</p></body></html>")
        return httpx.Response(404)
    return handler, seen


def test_github_repo_page_uses_api():
    handler, seen = _github_handler()
    web = WebTools(WebConfig(enabled=True), resolver=resolver({}), transport=httpx.MockTransport(handler))
    out = asyncio.run(web.web_fetch("https://github.com/o/r"))
    assert "License: MIT License (MIT)" in out and "Install with pip." in out and "src/  setup.py" in out
    assert "A demo tool." in out and "github.com/o/r" not in seen  # the HTML page wasn't needed


def test_github_falls_back_to_html_when_api_fails():
    handler, seen = _github_handler(meta_status=403)  # e.g. the unauthenticated API rate limit
    web = WebTools(WebConfig(enabled=True), resolver=resolver({}), transport=httpx.MockTransport(handler))
    out = asyncio.run(web.web_fetch("https://github.com/o/r"))
    assert "HTML page." in out and "github.com/o/r" in seen


# web app: a syntax error in app.js blanks the whole phone app, and nothing else would catch it
def test_web_app_js_parses():
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    app_js = Path(__file__).resolve().parent.parent / "harness" / "web" / "app.js"
    result = subprocess.run([node, "--check", "--input-type=module"], input=app_js.read_text(encoding="utf-8"),
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


# Claude Code Remote Control launches (8b)
import json as _json  # noqa: E402
import subprocess as _subprocess  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from harness.config import Project, RemoteControlConfig  # noqa: E402
from harness.fileops import ToolError  # noqa: E402
from harness.policy import ASK, Policy  # noqa: E402
from harness.remote_control import RemoteControl, parse_log  # noqa: E402

FAKE_LOG = ("· Connecting · repo · HEAD\n\x1b[1A\x1b[J· Connected · repo · HEAD\n    Capacity: 1/4 · New sessions\n"
            "    \x1b]8;;https://claude.ai/code/session_01AbC?from=cli\x07demo\x1b]8;;\x07\n"
            "Continue coding in the Claude mobile app or https://claude.ai/code?environment=env_01XyZ\n")


def test_parse_log():
    info = parse_log(FAKE_LOG)
    assert info["pairing_url"] == "https://claude.ai/code?environment=env_01XyZ"
    assert info["session_urls"] == ["https://claude.ai/code/session_01AbC"] and info["active_sessions"] == 1
    assert parse_log("Error: Workspace not trusted. Please run `claude` in X first")["error"].startswith("Error: Workspace")


def _rc_setup(tmp_path, trusted=True, script=None):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(_json.dumps({"projects": {str(repo).replace("\\", "/"): {"hasTrustDialogAccepted": trusted}}}))
    fake = tmp_path / "fake_claude.py"
    fake.write_text("import sys, time\nsys.stdout.reconfigure(encoding='utf-8')\n" + (script or (
        f"sys.stdout.write({FAKE_LOG!r}); sys.stdout.flush()\n"
        "time.sleep(60)\n")), encoding="utf-8")
    cfg = make_cfg(tmp_path)
    cfg.projects["repo"] = Project(name="repo", repo=str(repo))
    cfg.projects["remote"] = Project(name="remote", repo="https://github.com/o/r")
    notes = []
    rc = RemoteControl(cfg, RemoteControlConfig(enabled=True, claude_path=_sys.executable), notify=notes.append,
                       claude_json=claude_json)
    rc._command = lambda: [_sys.executable, str(fake)]  # stands in for `claude remote-control ...`
    return rc, repo, notes


def test_remote_control_launch_status_stop(tmp_path):
    rc, repo, notes = _rc_setup(tmp_path)

    async def body():
        assert rc.eligible() == ["repo"]  # scratch has no folder, remote is a URL
        view = await rc.launch("repo", started_by="test")
        assert view["running"] and view["pairing_url"].endswith("env_01XyZ") and view["active_sessions"] == 1
        assert notes and notes[0]["click"] == view["pairing_url"]
        again = await rc.launch("repo")
        assert again["already_running"] and again["pid"] == view["pid"]
        # a new RemoteControl (the daemon restarted) still finds and stops it
        fresh = RemoteControl(rc.cfg, rc.rc, claude_json=rc.claude_json)
        assert fresh.status()[0]["running"]
        await fresh.stop("repo")
        assert not fresh.status()[0]["running"]
    asyncio.run(body())


def test_remote_control_refuses_untrusted_and_reports_failures(tmp_path):
    rc, repo, _ = _rc_setup(tmp_path, trusted=False)

    async def body():
        try:
            await rc.launch("repo")
            raise AssertionError("launched an untrusted folder")
        except ToolError as e:
            assert "run `claude` once" in str(e)
        for name in ("remote", "nope"):
            try:
                await rc.launch(name)
                raise AssertionError(name)
            except ToolError:
                pass
    asyncio.run(body())

    rc2, _, _ = _rc_setup(tmp_path / "b", script="print('Error: something broke'); raise SystemExit(1)\n")

    async def failing():
        try:
            await rc2.launch("repo")
            raise AssertionError("should fail")
        except ToolError as e:
            assert "something broke" in str(e)
        assert not rc2.status()[0]["running"]
    asyncio.run(failing())


def test_remote_control_tool_always_asks():
    assert Policy().decide("open_claude_remote_control", {"project": "x", "reason": "y"}).action == ASK
    assert Policy([{"tool": "*", "action": "allow"}]).decide("open_claude_remote_control", {}).action == ASK
