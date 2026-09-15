"""Phase 8c: quotes in final answers must come from something the agent read, and GitHub pages read through the API."""

from __future__ import annotations

import asyncio
import base64
import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.cli_backends import ClaudeSession
from harness.config import BackendConfig, SandboxConfig, WebConfig
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
from harness.policy import ALLOW, ASK, Policy  # noqa: E402
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


# Claude Code stream-json backend (8a step 2)

FAKE_CLAUDE = r'''import json
import pathlib
import sys
import time

mode = sys.argv[1]
state = pathlib.Path(sys.argv[2])

def read():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    item = json.loads(line)
    with state.open("a", encoding="utf-8") as f:
        f.write("IN " + json.dumps(item, sort_keys=True) + "\n")
    return item

def send(item):
    print(json.dumps(item), flush=True)

read()  # host initialize request
send({"type": "control_response", "response": {"subtype": "success", "request_id": "init-1",
      "response": {"commands": [], "models": ["claude-opus-5"]}}})
user = read()
send({"type": "system", "subtype": "init", "session_id": "claude-session-1", "model": "claude-opus-5"})

if mode == "inbox":
    followup = read()
    send({"type": "result", "subtype": "success", "result": followup["message"]["content"],
          "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1}, "num_turns": 1})
elif mode == "cancel":
    time.sleep(60)
else:
    tool = "Read" if mode == "allow" else "Bash"
    args = {"file_path": "/workspace/a.txt"} if tool == "Read" else {"command": "python build.py"}
    send({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "tool-1", "name": tool, "input": args}]}})
    send({"type": "control_request", "request_id": "permission-1", "request": {
        "subtype": "can_use_tool", "tool_name": tool, "display_name": tool, "input": args,
        "description": "test command", "permission_suggestions": [], "tool_use_id": "tool-1"}})
    response = read()
    behavior = response["response"]["response"]["behavior"]
    send({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result",
          "tool_use_id": "tool-1", "content": behavior, "is_error": behavior == "deny"}]}})
    send({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning",
          "rateLimitType": "seven_day", "utilization": 0.8, "resetsAt": 1234567890}})
    send({"type": "result", "subtype": "success", "result": behavior,
          "total_cost_usd": 0.42, "usage": {"input_tokens": 10, "cache_creation_input_tokens": 2,
          "cache_read_input_tokens": 3, "output_tokens": 4}, "num_turns": 2})
'''


def _claude_manager(tmp_path, mode: str, state=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    state = state or tmp_path / "fake-state.jsonl"
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    manager = Manager(cfg)
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return ClaudeSession(**kwargs, command=[sys.executable, "-u", str(fake), mode, str(state)])

    manager.runner.cli_factory = factory
    return manager, made, state


def test_claude_docker_command_is_sandboxed_and_resumable(tmp_path):
    backend = BackendConfig(enabled=True, proxy="http://proxy:8888", volume="auth-volume", network="cli-net")
    sandbox = SandboxConfig(memory="3g", cpus="1.5", pids=321)
    cli = ClaudeSession(session_id="abc", workspace=tmp_path, backend=backend, sandbox=sandbox,
                        system_prompt="system", model="claude-test", backend_session_id="resume-me")
    command = cli.command()
    assert command[:6] == ["docker", "run", "--rm", "-i", "--name", "harness-abc-claude"]
    for pair in (["--network", "cli-net"], ["-e", "HTTPS_PROXY=http://proxy:8888"],
                 ["-v", "auth-volume:/home/agent/.claude"], ["--memory", "3g"],
                 ["--cpus", "1.5"], ["--pids-limit", "321"], ["--permission-prompt-tool", "stdio"],
                 ["--permission-mode", "default"], ["--model", "claude-test"], ["--resume", "resume-me"]):
        at = command.index(pair[0])
        assert command[at:at + 2] == pair
    assert not any("bypass" in arg or "skip-permissions" in arg for arg in command)


def test_claude_cli_allow_maps_events_usage_and_limits(tmp_path):
    async def body():
        m, made, state = _claude_manager(tmp_path, "allow")
        await m.start()
        s = await wait_status(m, m.create("read it", backend="claude")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert s["backend"] == "claude" and s["model"] == "claude-opus-5"
        assert s["answer"] == "allow" and s["run"]["backend_session_id"] == "claude-session-1"
        assert s["run"]["rate_limits"]["utilization"] == 0.8
        assert s["run"]["tool_calls"] == 1 and s["run"]["tool_errors"] == 0
        assert s["totals"] == {"turns": 2, "prompt_tokens": 15, "completion_tokens": 4,
                                "total_cost_usd": 0.42}
        assistant = events(m, s["id"], "assistant")[0]
        assert assistant["content"] == "Checking."
        assert assistant["tool_calls"][0]["function"] == {
            "name": "Read", "arguments": json.dumps({"file_path": "/workspace/a.txt"})}
        assert events(m, s["id"], "tool_result")[0]["output"] == "allow"
        assert events(m, s["id"], "rate_limit")[0]["rateLimitType"] == "seven_day"
        sent = state.read_text(encoding="utf-8")
        assert '"subtype": "initialize"' in sent and '"behavior": "allow"' in sent
        assert made[0]["system_prompt"] == s["context"][0]["content"]
        assert made[0]["model"] == "claude-opus-5"
        await m.stop()
    asyncio.run(body())


def test_claude_cli_ask_approve_and_deny(tmp_path):
    async def one(root, approve, note=""):
        m, _, state = _claude_manager(root, "ask")
        await m.start()
        sid = m.create("run it", backend="claude")["id"]
        await wait_status(m, sid, "waiting_approval")
        pending = m.db.pending_approvals(sid)
        assert len(pending) == 1 and pending[0]["tool"] == "Bash"
        m.decide(sid, pending[0]["id"], approve=approve, note=note)
        s = await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"] == ("allow" if approve else "deny")
        if not approve:
            assert note in state.read_text(encoding="utf-8")
        await m.stop()

    asyncio.run(one(tmp_path / "approved", True))
    asyncio.run(one(tmp_path / "denied", False, "not today"))


def test_claude_cli_inbox_message(tmp_path):
    async def body():
        m, _, _ = _claude_manager(tmp_path, "inbox")
        await m.start()
        sid = m.create("wait for more", backend="claude")["id"]
        await wait_status(m, sid, "running")
        await m.send(sid, "the follow-up")
        s = await wait_status(m, sid, "done")
        assert s["answer"] == "the follow-up" and s["inbox"] == []
        await m.stop()
    asyncio.run(body())


def test_claude_cli_cancel_kills_process(tmp_path):
    async def body():
        m, _, _ = _claude_manager(tmp_path, "cancel")
        await m.start()
        sid = m.create("wait", backend="claude")["id"]
        await wait_status(m, sid, "running")
        s = await m.cancel(sid)
        assert s["status"] == "cancelled" and sid not in m.runner._cli_sessions
        await m.stop()
    asyncio.run(body())


def test_claude_cli_restart_resumes_and_keeps_pending_approval(tmp_path):
    async def body():
        m1, _, state = _claude_manager(tmp_path, "ask")
        await m1.start()
        sid = m1.create("run it", backend="claude")["id"]
        await wait_status(m1, sid, "waiting_approval")
        aid = m1.db.pending_approvals(sid)[0]["id"]
        await m1.stop()
        m1.db.close()

        m2, made, _ = _claude_manager(tmp_path, "ask", state=state)
        await m2.start()
        for _ in range(500):
            if made:
                break
            await asyncio.sleep(0.02)
        assert made
        await wait_status(m2, sid, "waiting_approval")
        assert m2.db.pending_approvals(sid)[0]["id"] == aid
        assert made[0]["backend_session_id"] == "claude-session-1"
        m2.decide(sid, aid, approve=True)
        s = await wait_status(m2, sid, "done")
        assert s["answer"] == "allow"
        sent = state.read_text(encoding="utf-8")
        assert "The harness restarted; continue the task." in sent
        await m2.stop()
    asyncio.run(body())


def test_claude_backend_validation_and_policy(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=False)
    m = Manager(cfg)
    with pytest.raises(Exception, match="disabled"):
        m.create("hello", backend="claude")
    with pytest.raises(Exception, match="unknown backend"):
        m.create("hello", backend="codex")
    assert Policy().decide("Write", {"file_path": "/workspace/a.txt"}).action == ALLOW
    assert Policy().decide("Write", {"file_path": "/etc/passwd"}).action == ASK
    assert Policy().decide("Write", {"file_path": "/workspace/../etc/passwd"}).action == ASK
    assert Policy().decide("Bash", {"command": "pytest -q"}).action == ASK
    assert Policy([{"tool": "run_shell", "args": {"command": "^pytest"}, "action": "allow"}]).decide(
        "Bash", {"command": "pytest -q"}).action == ALLOW
    assert Policy(repo=True).decide("Bash", {"command": "git push origin main"}).action != ALLOW
    assert Policy().decide("WebFetch", {"url": "https://example.com"}).action == ASK


def test_web_and_app_session_apis_accept_backend(tmp_path):
    m, _, _ = _claude_manager(tmp_path, "allow")
    with TestClient(create_app(m)) as client:
        own = client.post("/sessions", json={"prompt": "web", "backend": "claude"})
        assert own.status_code == 201 and own.json()["backend"] == "claude"
        token = client.post("/keys", json={"name": "app", "kind": "app", "scopes": ["sessions"]}).json()["key"]
        app = client.post("/api/v1/sessions", headers={"Authorization": f"Bearer {token}"},
                          json={"prompt": "app", "backend": "claude"})
        assert app.status_code == 201 and app.json()["backend"] == "claude"
