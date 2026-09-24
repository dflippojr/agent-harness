"""Phase 8c: quotes in final answers must come from something the agent read, and GitHub pages read through the API."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.cli_backends import ClaudeSession, CodexSession, CursorSession
from harness.config import BackendConfig, SandboxConfig, WebConfig
from harness.grounding import quote_hrefs, ungrounded_quotes
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


def test_quote_hrefs_link_web_fetch_pages():
    page = ("https://docs.example.com/license", PAGE)
    other = ("https://example.com/other", "unrelated text about something else entirely for matching")
    hrefs = quote_hrefs('The README says "released under the GNU Affero General Public License".', [other, page])
    assert hrefs["released under the GNU Affero General Public License"].startswith("https://docs.example.com/license#:~:text=")
    assert "GNU" in hrefs["released under the GNU Affero General Public License"]
    assert quote_hrefs('The README says "released under the GNU Affero General Public License".', [other]) == {}
    assert quote_hrefs('See memory note "released under the GNU Affero General Public License".',
                       [("categories/work/memory.md", PAGE)]) == {}


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
from harness.remote_control import RemoteControl, parse_log, STOP_TIMEOUT  # noqa: E402

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


def test_remote_control_only_folders_are_additive_and_expand_environment(tmp_path, monkeypatch):
    rc, repo, _ = _rc_setup(tmp_path)
    standalone = tmp_path / "standalone"
    (standalone / ".git").mkdir(parents=True)
    monkeypatch.setenv("REMOTE_CONTROL_TEST_ROOT", str(tmp_path))
    rc.rc.projects = []
    rc.rc.folders = {"standalone": "$REMOTE_CONTROL_TEST_ROOT/standalone"}

    # A standalone folder is eligible without becoming a harness session project. An explicit project allowlist
    # still limits ordinary session projects, while Remote Control-only folders remain additive.
    assert "standalone" not in rc.cfg.projects
    assert rc.eligible() == ["standalone"]
    assert rc.folder("standalone") == standalone
    with pytest.raises(ToolError, match="isn't enabled"):
        rc.folder("repo")
    assert repo.is_dir()


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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and fresh.status()[0]["running"]:
            await asyncio.sleep(0.05)
        assert not fresh.status()[0]["running"]
    asyncio.run(body())


def test_remote_control_stop_ignores_vanished_processes(tmp_path, monkeypatch):
    import harness.remote_control as rc_module

    rc, _, _ = _rc_setup(tmp_path)
    rc._save({"repo": {"pid": 4242, "created": 1.0, "started_at": 1.0, "log": "",
                       "started_by": "test", "command": []}})
    waited = []

    class FakeChild:
        def kill(self):
            raise rc_module.psutil.NoSuchProcess(pid=4243)

    class FakeProc:
        def is_running(self):
            return True

        def create_time(self):
            return 1.0

        def children(self, recursive=False):
            return [FakeChild()]

        def kill(self):
            raise rc_module.psutil.NoSuchProcess(pid=4242)

    monkeypatch.setattr(rc_module.psutil, "Process", lambda pid: FakeProc())
    monkeypatch.setattr(rc_module.psutil, "wait_procs",
                        lambda procs, timeout=None: waited.append((list(procs), timeout)) or ([], list(procs)))

    async def body():
        result = await rc.stop("repo")
        assert result == {"project": "repo", "running": False}

    asyncio.run(body())
    assert waited and waited[0][1] == STOP_TIMEOUT and len(waited[0][0]) == 2
    assert rc._load()["repo"].get("stopped_at")


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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and rc2.status()[0]["running"]:
            await asyncio.sleep(0.05)
        assert not rc2.status()[0]["running"]
    asyncio.run(failing())


def test_remote_control_opens_trust_prompt_in_exact_folder(tmp_path, monkeypatch):
    import harness.remote_control as rc_module

    rc, repo, _ = _rc_setup(tmp_path, trusted=False)
    calls = []

    class FakeProcess:
        pid = 123
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    rc.popen = fake_popen
    monkeypatch.setattr(rc_module.sys, "platform", "win32")
    monkeypatch.setattr(rc_module.shutil, "which", lambda name: "C:\\Windows\\powershell.exe" if name == "powershell.exe" else None)

    view = rc.open_trust_prompt("repo")
    assert view["trust_prompt_open"] and not view["trusted"]
    command, options = calls[0]
    assert command[:4] == ["C:\\Windows\\powershell.exe", "-NoLogo", "-NoProfile", "-Command"]
    assert options["cwd"] == str(repo)
    assert options["env"]["HARNESS_CLAUDE_PATH"] == _sys.executable
    assert options["env"]["HARNESS_CLAUDE_TRUST_PROJECT"] == "repo"
    assert str(repo) not in command[-1]  # paths are passed without shell interpolation
    assert rc.open_trust_prompt("repo")["already_open"] and len(calls) == 1

    process.returncode = 0
    assert not rc.status()[0]["trust_prompt_open"]
    rc.claude_json.write_text(_json.dumps({"projects": {
        str(repo).replace("\\", "/"): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert rc.open_trust_prompt("repo")["already_trusted"] and len(calls) == 1


def test_remote_control_trust_web_endpoint(tmp_path, monkeypatch):
    import harness.remote_control as rc_module

    rc, _, _ = _rc_setup(tmp_path, trusted=False)

    class FakeProcess:
        pid = 123

        def poll(self):
            return None

    rc.popen = lambda command, **kwargs: FakeProcess()
    monkeypatch.setattr(rc_module.sys, "platform", "win32")
    monkeypatch.setattr(rc_module.shutil, "which", lambda name: "powershell.exe")
    manager = Manager(rc.cfg, chat=Script([Completion(content="unused")]))
    manager.remote_control = rc
    with TestClient(create_app(manager)) as client:
        response = client.post("/remote-control/repo/trust")
        assert response.status_code == 200
        assert response.json()["trust_prompt_open"]
        assert client.post("/remote-control/nope/trust").status_code == 400


def test_remote_control_tool_always_asks():
    assert Policy().decide("open_claude_remote_control", {"project": "x", "reason": "y"}).action == ASK
    assert Policy([{"tool": "*", "action": "allow"}]).decide("open_claude_remote_control", {}).action == ASK


def test_remote_control_tool_attached_only_to_owner_tower_session(tmp_path):
    rc, _, _ = _rc_setup(tmp_path)
    manager = Manager(rc.cfg, chat=Script([Completion(content="unused")]))
    manager.runner.remote_control = rc
    owner = {
        "id": "owner-tower", "project": "repo", "target": "tower", "model": "fake",
        "workspace": str(tmp_path / "workspace"), "owner_id": "owner", "app_id": "",
    }
    app = {**owner, "id": "app-tower", "app_id": "app-key"}
    runner = {**owner, "id": "owner-runner", "target": "macbook"}

    assert rc in manager.runner.daemon_toolkits(owner)
    owner_tools = {schema["function"]["name"] for schema in manager.runner.tool_schemas(owner, manager.runner.workspace(owner))}
    assert "open_claude_remote_control" in owner_tools
    assert rc not in manager.runner.daemon_toolkits(app)
    assert rc not in manager.runner.daemon_toolkits(runner)


# Claude Code stream-json backend (8a step 2)

FAKE_CLAUDE = r'''import json
import pathlib
import sys
import time

mode = sys.argv[1]
state = pathlib.Path(sys.argv[2])
call_id = sys.argv[3] if len(sys.argv) > 3 else "tool-1"
command = sys.argv[4] if len(sys.argv) > 4 else "python build.py"

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

if mode == "limit":
    send({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
          "rateLimitType": "seven_day", "utilization": 1.0, "resetsAt": time.time() + 60}})
    time.sleep(60)
elif mode == "inbox":
    followup = read()
    send({"type": "result", "subtype": "success", "result": followup["message"]["content"],
          "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1}, "num_turns": 1})
elif mode == "echo":
    send({"type": "result", "subtype": "success", "result": user["message"]["content"],
          "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1}, "num_turns": 1})
elif mode == "cancel":
    time.sleep(60)
else:
    tool = "Read" if mode == "allow" else "Bash"
    args = {"file_path": "/workspace/a.txt"} if tool == "Read" else {"command": command}
    send({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": call_id, "name": tool, "input": args}]}})
    send({"type": "control_request", "request_id": "permission-1", "request": {
        "subtype": "can_use_tool", "tool_name": tool, "display_name": tool, "input": args,
        "description": "test command", "permission_suggestions": [], "tool_use_id": call_id}})
    response = read()
    behavior = response["response"]["response"]["behavior"]
    send({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result",
          "tool_use_id": call_id, "content": behavior, "is_error": behavior == "deny"}]}})
    send({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning",
          "rateLimitType": "seven_day", "utilization": 0.8, "resetsAt": 1234567890}})
    send({"type": "result", "subtype": "success", "result": behavior,
          "total_cost_usd": 0.42, "usage": {"input_tokens": 10, "cache_creation_input_tokens": 2,
          "cache_read_input_tokens": 3, "output_tokens": 4}, "num_turns": 2})
'''


async def wait_cli_gone(manager, sid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and sid in manager.runner._cli_sessions:
        await asyncio.sleep(0.05)
    assert sid not in manager.runner._cli_sessions


async def wait_cli_alive(manager, sid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cli = manager.runner._cli_sessions.get(sid)
        if cli is not None and cli.process is not None and cli.process.poll() is None:
            return cli
        await asyncio.sleep(0.02)
    raise AssertionError("CLI process never started")


def _seed_unresolved_cancel_work(manager, sid):
    running = {"id": "tool-running", "type": "function",
               "function": {"name": "Bash", "arguments": json.dumps({"command": "sleep 1"})}}
    queued = {"id": "tool-queued", "type": "function",
              "function": {"name": "Read", "arguments": json.dumps({"file_path": "/workspace/a.txt"})}}
    session = manager.db.get_session(sid)
    manager.db.update_session(sid, context=session["context"] + [
        {"role": "assistant", "content": "", "tool_calls": [running, queued]},
    ], run={**session["run"], "executing": {"id": "tool-running", "name": "Bash"}})
    manager.db.insert_approval({"id": f"a-pending-{sid[:8]}", "session_id": sid,
                                "tool_call_id": "tool-queued", "tool": "Read",
                                "args": {"file_path": "/workspace/a.txt"}, "reason": "ask"})


def _notification_titles(manager, sid):
    from harness.notify import Notifier
    notifier = Notifier(manager.cfg, manager.db)
    titles = []
    for event in manager.db.events(sid):
        note = notifier.build(event)
        if note:
            titles.append(note["title"])
    return titles


def _claude_manager(tmp_path, mode: str, state=None, max_sessions=2, bash_command="python build.py"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    state = state or tmp_path / "fake-state.jsonl"
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", max_sessions=max_sessions)
    manager = Manager(cfg)
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        call_id = "tool-2" if kwargs.get("backend_session_id") else "tool-1"
        return ClaudeSession(**kwargs, command=[sys.executable, "-u", str(fake), mode, str(state), call_id,
                                                bash_command])

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
                 ["-e", "HTTP_PROXY=http://proxy:8888"], ["-e", "NO_PROXY=localhost,127.0.0.1"],
                 ["-e", "NODE_USE_ENV_PROXY=1"],
                 ["-v", "auth-volume:/home/agent/.claude"], ["--memory", "3g"],
                 ["--cpus", "1.5"], ["--pids-limit", "321"], ["--permission-prompt-tool", "stdio"],
                 ["--permission-mode", "default"], ["--model", "claude-test"], ["--resume", "resume-me"]):
        assert any(command[at:at + 2] == pair for at in range(len(command) - 1))
    assert not any("bypass" in arg or "skip-permissions" in arg for arg in command)
    keyed = ClaudeSession(session_id="keyed", workspace=tmp_path, backend=backend, sandbox=sandbox,
                          system_prompt="system", api_key="secret-value").command()
    assert ["-e", "ANTHROPIC_API_KEY"] == keyed[keyed.index("ANTHROPIC_API_KEY") - 1:keyed.index("ANTHROPIC_API_KEY") + 1]
    assert "secret-value" not in keyed


def test_claude_start_removes_restart_orphan_before_reusing_name(tmp_path, monkeypatch):
    import harness.cli_backends as cli_backends
    calls = []

    async def fake_run_cmd(command, timeout):
        calls.append(("rm", command))
        return 1, "", "not found"

    def fake_popen(command, **kwargs):
        calls.append(("popen", command))
        return cli_backends.subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    monkeypatch.setattr(cli_backends, "run_cmd", fake_run_cmd)

    async def body():
        cli = ClaudeSession(session_id="orphan", workspace=tmp_path, backend=BackendConfig(enabled=True),
                            sandbox=SandboxConfig(), system_prompt="system", popen=fake_popen)
        await cli.start()
        await cli.stop()

    asyncio.run(body())
    assert calls[0] == ("rm", ["docker", "rm", "-f", "harness-orphan-claude"])
    assert calls[1][0] == "popen"
    assert calls[-1] == calls[0]


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
        records = [json.loads(line[3:]) for line in (tmp_path / "fake-state.jsonl").read_text().splitlines()]
        assert all(item.get("session_id", "") == "" for item in records if item.get("type") == "user")
        await m.stop()
    asyncio.run(body())


def test_claude_cli_completed_session_followup_sends_latest_message(tmp_path):
    async def body():
        m, made, state = _claude_manager(tmp_path, "echo")
        await m.start()
        sid = m.create("the original prompt", backend="claude")["id"]
        await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        await m.send(sid, "the later follow-up")
        s = await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"] == "the later follow-up"
        assert len(made) == 2 and made[1]["backend_session_id"] == "claude-session-1"
        users = [json.loads(line[3:]) for line in state.read_text().splitlines()
                 if json.loads(line[3:]).get("type") == "user"]
        assert [item["message"]["content"] for item in users] == ["the original prompt", "the later follow-up"]
        assert all(item["session_id"] == "" for item in users)
        await m.stop()
    asyncio.run(body())


def test_claude_cli_cancel_kills_process(tmp_path):
    async def body():
        m, _, _ = _claude_manager(tmp_path, "cancel")
        await m.start()
        sid = m.create("wait", backend="claude")["id"]
        await wait_status(m, sid, "running")
        s = await m.cancel(sid)
        assert s["status"] == "cancelled"
        await wait_cli_gone(m, sid)
        await m.stop()
    asyncio.run(body())


def test_pending_user_cancel_recorded_when_cli_dies(tmp_path):
    def spy_cancel(runner):
        recorded = []
        original = runner._record_cancel

        def wrapped(sid):
            recorded.append(sid)
            original(sid)

        runner._record_cancel = wrapped
        return recorded

    async def pending_cancel_then_cli_dies():
        m, _, _ = _claude_manager(tmp_path / "pending", "cancel")
        recorded = spy_cancel(m.runner)
        await m.start()
        sid = m.create("wait", backend="claude")["id"]
        await wait_status(m, sid, "running")
        cli = await wait_cli_alive(m, sid)
        _seed_unresolved_cancel_work(m, sid)
        m.db.update_session(sid, job_id="job-pending-cancel")
        m.runner.user_cancelled.add(sid)
        cli.process.kill()
        await wait_status(m, sid, "cancelled")
        await asyncio.gather(*m.tasks.values())
        await wait_cli_gone(m, sid)
        s = m.db.get_session(sid)
        assert s["status"] == "cancelled" and s["stop_reason"] == "cancelled"
        assert recorded == [sid]
        assert s["run"].get("failure") is None
        assert m.summary(s).get("failure") is None
        assert events(m, sid, "error") == []
        finished = events(m, sid, "run_finished")
        assert len(finished) == 1 and finished[0]["status"] == "cancelled"
        assert finished[0]["stop_reason"] == "cancelled"
        titles = _notification_titles(m, sid)
        assert not any("failed" in title.lower() for title in titles)
        results = {msg["tool_call_id"]: msg["content"] for msg in s["context"] if msg.get("role") == "tool"}
        assert results["tool-running"] == "Cancelled by the user while running."
        assert results["tool-queued"] == "Not run: the user cancelled the task."
        assert m.db.pending_approvals(sid) == []
        assert m.db.approvals(sid)[0]["status"] == "cancelled"
        await m.stop()

    async def no_cancel_pending_leaves_failed():
        m, _, _ = _claude_manager(tmp_path / "failed", "cancel")
        recorded = spy_cancel(m.runner)
        await m.start()
        sid = m.create("wait", backend="claude")["id"]
        await wait_status(m, sid, "running")
        cli = await wait_cli_alive(m, sid)
        _seed_unresolved_cancel_work(m, sid)
        cli.process.kill()
        await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        await wait_cli_gone(m, sid)
        s = m.db.get_session(sid)
        assert s["status"] == "failed" and recorded == []
        assert s["stop_reason"].startswith("provider_unavailable") or s["stop_reason"].startswith("provider_error")
        assert s["run"].get("failure")
        assert events(m, sid, "error") == [s["run"]["failure"]]
        finished = events(m, sid, "run_finished")
        assert finished and finished[-1]["status"] == "failed"
        assert m.summary(s).get("failure") == s["run"]["failure"]
        results = {msg["tool_call_id"]: msg["content"] for msg in s["context"] if msg.get("role") == "tool"}
        assert results == {}
        assert m.db.pending_approvals(sid)
        await m.stop()

    async def already_cancelled_does_not_record_again():
        m, _, _ = _claude_manager(tmp_path / "cancelled", "cancel")
        recorded = spy_cancel(m.runner)
        await m.start()
        sid = m.create("wait", backend="claude")["id"]
        await wait_status(m, sid, "running")
        s = await m.cancel(sid)
        await wait_cli_gone(m, sid)
        assert s["status"] == "cancelled" and recorded == [sid]
        m.runner.user_cancelled.add(sid)
        assert await m.runner._take_pending_cancel(sid) is False
        assert recorded == [sid]
        await m.stop()

    async def already_done_skips_record_cancel():
        m, _, _ = _claude_manager(tmp_path / "done", "echo")
        recorded = spy_cancel(m.runner)
        inner = m.runner.cli_factory

        def factory(**kwargs):
            cli = inner(**kwargs)
            start = cli.start

            async def mark_cancel_then_start():
                await start()
                m.runner.user_cancelled.add(kwargs["session_id"])

            cli.start = mark_cancel_then_start
            return cli

        m.runner.cli_factory = factory
        await m.start()
        sid = m.create("the original prompt", backend="claude")["id"]
        s = await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        assert s["status"] == "done" and s["stop_reason"] == "final_message" and recorded == []
        m.runner.user_cancelled.add(sid)
        assert await m.runner._take_pending_cancel(sid) is False
        assert recorded == []
        await m.stop()

    async def generic_exception_during_pending_cancel():
        m, _, _ = _claude_manager(tmp_path / "crash-cancel", "cancel")
        recorded = spy_cancel(m.runner)
        await m.start()
        sid = m.create("wait", backend="claude")["id"]
        await wait_status(m, sid, "running")
        cli = await wait_cli_alive(m, sid)
        _seed_unresolved_cancel_work(m, sid)
        m.runner.user_cancelled.add(sid)

        async def boom(timeout=0):
            raise RuntimeError("injected crash")

        cli.receive = boom
        await wait_status(m, sid, "cancelled")
        await asyncio.gather(*m.tasks.values())
        await wait_cli_gone(m, sid)
        s = m.db.get_session(sid)
        assert s["status"] == "cancelled" and s["stop_reason"] == "cancelled"
        assert recorded == [sid]
        assert s["run"].get("failure") is None
        assert events(m, sid, "error") == []
        finished = events(m, sid, "run_finished")
        assert len(finished) == 1 and finished[0]["status"] == "cancelled"
        await m.stop()

    async def generic_exception_without_cancel_still_fails():
        m, _, _ = _claude_manager(tmp_path / "crash-failed", "cancel")
        recorded = spy_cancel(m.runner)
        await m.start()
        sid = m.create("wait", backend="claude")["id"]
        await wait_status(m, sid, "running")
        cli = await wait_cli_alive(m, sid)

        async def boom(timeout=0):
            raise RuntimeError("injected crash")

        cli.receive = boom
        await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        await wait_cli_gone(m, sid)
        s = m.db.get_session(sid)
        assert s["status"] == "failed" and recorded == []
        assert s["stop_reason"].startswith("internal_error")
        assert s["run"].get("failure", {}).get("code") == "internal_error"
        assert events(m, sid, "error")
        finished = events(m, sid, "run_finished")
        assert finished and finished[-1]["status"] == "failed"
        await m.stop()

    asyncio.run(pending_cancel_then_cli_dies())
    asyncio.run(no_cancel_pending_leaves_failed())
    asyncio.run(already_cancelled_does_not_record_again())
    asyncio.run(already_done_skips_record_cancel())
    asyncio.run(generic_exception_during_pending_cancel())
    asyncio.run(generic_exception_without_cancel_still_fails())


def test_claude_backend_semaphore_limits_live_processes(tmp_path):
    async def body():
        m, made, _ = _claude_manager(tmp_path, "cancel", max_sessions=1)
        await m.start()
        first = m.create("first", backend="claude")["id"]
        second = m.create("second", backend="claude")["id"]
        await wait_status(m, first, "running")
        await asyncio.sleep(0.1)
        assert len(made) == 1 and m.get(second)["status"] == "queued"
        await m.cancel(first)
        await wait_status(m, second, "running")
        assert len(made) == 2
        await m.cancel(second)
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
        approvals = m2.db.approvals(sid)
        assert len(approvals) == 1 and approvals[0]["tool_call_id"] == "tool-1"
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


def test_backend_usage_tally_metrics_and_api(tmp_path, monkeypatch):
    from harness import backend_state
    from harness.metrics import render

    monkeypatch.setattr(backend_state, "_subscription_status", lambda name, cfg: True)

    async def body():
        m, _, _ = _claude_manager(tmp_path, "allow")
        await m.start()
        s = await wait_status(m, m.create("read it", backend="claude")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert m.db.get_backend_usage("claude")["data"]["utilization"] == 0.8
        tally = m.db.usage_tally("claude", 0)
        assert tally == {"requests": 1, "prompt_tokens": 15, "completion_tokens": 4, "cost_usd": 0.42}
        metrics = render(m)
        assert 'harness_backend_utilization{backend="claude",window="seven_day"} 0.8' in metrics
        assert 'harness_backend_cost_usd_total{backend="claude"} 0.42' in metrics
        await m.stop()
        with TestClient(create_app(m)) as client:
            public = client.get("/backends").json()[1]
            assert public["logged_in"] and public["model"] == "claude-opus-5"
            assert public["today"]["requests"] == 1
            token = client.post("/keys", json={"name": "app", "kind": "app", "scopes": ["sessions"]}).json()["key"]
            headers = {"Authorization": f"Bearer {token}"}
            # App status is attributed to that app; owner and other-app usage is not exposed.
            assert client.get("/api/v1/backends", headers=headers).json()[0]["week"]["cost_usd"] == 0
            assert client.get("/api/v1").json()["backends"][0]["notice"]
    asyncio.run(body())


def test_backend_prefs_persist_model_and_effort(tmp_path, monkeypatch):
    from harness import backend_state
    monkeypatch.setattr(backend_state, "_subscription_status", lambda name, cfg: True)
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", effort="high")
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    with TestClient(create_app(m)) as client:
        listed = {row["name"]: row for row in client.get("/backends").json()}
        assert listed["local"]["model"] == cfg.default_model
        assert listed["claude"]["model"] == "claude-opus-5" and listed["claude"]["effort"] == "high"
        updated = client.put("/backends/claude", json={"model": "claude-sonnet-4", "effort": "low"})
        assert updated.status_code == 200
        again = {row["name"]: row for row in client.get("/backends").json()}
        assert again["claude"]["model"] == "claude-sonnet-4" and again["claude"]["effort"] == "low"
        assert [m["id"] for m in listed["claude"]["popular_models"]] == [
            "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
        from harness.backend_state import POPULAR_MODELS
        assert [m[0] for m in POPULAR_MODELS["cursor"]] == [
            "cursor-grok-4.6-high", "composer-2.5-fast", "muse-spark-1.3-high"]
        assert not any(mid.startswith("gpt-") for mid, _ in POPULAR_MODELS["cursor"])
        assert client.put("/backends/claude", json={"effort": "banana"}).status_code == 400
        assert client.put("/backends/nope", json={"model": "x"}).status_code == 404
        assert client.put("/backends/local", json={"model": "missing"}).status_code == 400
    cfg2 = make_cfg(tmp_path)
    cfg2.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", effort="high")
    reloaded = Manager(cfg2)
    assert reloaded.cfg.backends["claude"].model == "claude-sonnet-4"
    assert reloaded.cfg.backends["claude"].effort == "low"


def test_backends_skip_auth_skips_docker_login_probe(tmp_path, monkeypatch):
    from harness import backend_state
    calls = []
    monkeypatch.setattr(backend_state, "_subscription_status", lambda name, cfg: calls.append(name) or True)
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", effort="high")
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    with TestClient(create_app(m)) as client:
        skip = {row["name"]: row for row in client.get("/backends", params={"auth": "skip"}).json()}
        assert calls == []
        assert skip["claude"]["model"] == "claude-opus-5" and skip["claude"]["logged_in"] is False
        listed = {row["name"]: row for row in client.get("/backends").json()}
        assert calls == ["claude"] and listed["claude"]["logged_in"] is True


def test_backend_billing_warning_waiting_limit_and_api_key_fallback(tmp_path):
    async def waiting():
        m, _, _ = _claude_manager(tmp_path / "waiting", "limit")
        m.cfg.backends["claude"].billing = "credits"
        await m.start()
        sid = m.create("hit the limit", backend="claude")["id"]
        s = await wait_status(m, sid, "waiting_limit")
        assert events(m, sid, "billing_warning")
        assert events(m, sid, "limit_waiting")[0]["resets_at"] > time.time()
        await m.cancel(sid)
        await m.stop()

    async def fallback():
        root = tmp_path / "fallback"
        m, made, state = _claude_manager(root, "limit")
        key_file = root / "claude.key"
        key_file.write_text("api-secret", encoding="utf-8")
        backend = m.cfg.backends["claude"]
        backend.auth = "subscription_then_api_key"
        backend.api_key_file = str(key_file)

        def factory(**kwargs):
            made.append(kwargs)
            mode = "echo" if kwargs.get("api_key") else "limit"
            return ClaudeSession(**kwargs, command=[sys.executable, "-u", str(root / "fake_claude.py"),
                                                    mode, str(state), "tool-1"])

        m.runner.cli_factory = factory
        await m.start()
        sid = m.create("fall back", backend="claude")["id"]
        s = await wait_status(m, sid, "done")
        assert len(made) == 2 and made[1]["api_key"] == "api-secret"
        assert events(m, sid, "backend_fallback") and events(m, sid, "billing_warning")
        rows = m.db.conn.execute("SELECT billing, credential_source FROM usage WHERE session_id = ?", (sid,)).fetchall()
        assert [tuple(row) for row in rows] == [("api_key", "user_file")]
        await m.stop()

    asyncio.run(waiting())
    asyncio.run(fallback())


# Codex app-server JSON-RPC backend (8a step 4)

FAKE_CODEX = r'''import json
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

init = read()
send({"id": init["id"], "result": {"serverInfo": {"name": "fake-codex", "version": "1"}}})
read()  # initialized notification
thread_request = read()
resumed = thread_request["method"] == "thread/resume"
thread_id = thread_request["params"].get("threadId") or "codex-thread-1"
send({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
send({"id": thread_request["id"], "result": {"thread": {"id": thread_id}}})
turn = read()
send({"id": turn["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}})

if mode == "inbox":
    followup = read()
    assert followup["method"] == "turn/steer"
    send({"id": followup["id"], "result": {"turn": {"id": "turn-2", "status": "inProgress", "items": []}}})
    answer = followup["params"]["input"][0]["text"]
elif mode == "cancel":
    time.sleep(60)
    raise SystemExit(0)
else:
    item_id = "item-2" if resumed else "item-1"
    if mode == "file":
        item = {"id": item_id, "type": "fileChange", "status": "inProgress",
                "changes": [{"path": "src/a.py", "kind": "update", "diff": "+ok"}]}
        method = "item/fileChange/requestApproval"
        params = {"itemId": item_id, "threadId": thread_id, "turnId": "turn-1",
                  "startedAtMs": 1, "reason": "edit source"}
    else:
        item = {"id": item_id, "type": "commandExecution", "status": "inProgress",
                "command": "pytest -q", "commandActions": [], "cwd": "/workspace"}
        method = "item/commandExecution/requestApproval"
        params = {"itemId": item_id, "threadId": thread_id, "turnId": "turn-1",
                  "startedAtMs": 1, "command": "pytest -q", "cwd": "/workspace"}
    send({"method": "item/started", "params": {"threadId": thread_id, "turnId": "turn-1",
          "startedAtMs": 1, "item": item}})
    send({"id": 100, "method": method, "params": params})
    approval = read()
    answer = approval["result"]["decision"]
    item["status"] = "completed" if answer == "accept" else "declined"
    if item["type"] == "commandExecution":
        item["aggregatedOutput"] = "4 passed"
        item["exitCode"] = 0 if answer == "accept" else None
        item["durationMs"] = 50
    send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "turn-1",
          "completedAtMs": 2, "item": item}})

send({"method": "account/rateLimits/updated", "params": {"rateLimits": {
      "planType": "plus", "primary": {"usedPercent": 28, "windowDurationMins": 10080,
      "resetsAt": 1234567890}, "secondary": None}}})
send({"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": "turn-1",
      "itemId": "msg-1", "delta": answer}})
send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "turn-1",
      "completedAtMs": 3, "item": {"id": "msg-1", "type": "agentMessage", "text": answer}}})
send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": "turn-1",
      "tokenUsage": {"last": {"inputTokens": 12, "cachedInputTokens": 3, "outputTokens": 4,
      "reasoningOutputTokens": 2, "totalTokens": 16}, "total": {"inputTokens": 12,
      "cachedInputTokens": 3, "outputTokens": 4, "reasoningOutputTokens": 2, "totalTokens": 16}}}})
send({"method": "turn/completed", "params": {"threadId": thread_id,
      "turn": {"id": "turn-1", "status": "completed", "items": []}}})
'''


def _codex_manager(tmp_path, mode: str, state=None, max_sessions=2):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_codex.py"
    fake.write_text(FAKE_CODEX, encoding="utf-8")
    state = state or tmp_path / "fake-codex-state.jsonl"
    cfg = make_cfg(tmp_path)
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.6-sol", effort="high",
                                           permission_mode="on-request",
                                           max_sessions=max_sessions)
    if mode == "command":
        cfg.projects["scratch"].rules = [{"tool": "exec_command", "action": "ask",
                                          "reason": "exercise Codex approval bridge"}]
    manager = Manager(cfg)
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return CodexSession(**kwargs, command=[sys.executable, "-u", str(fake), mode, str(state)])

    manager.runner.codex_factory = factory
    return manager, made, state


def test_codex_docker_command_is_sandboxed(tmp_path):
    backend = BackendConfig(enabled=True, proxy="http://proxy:8888", volume="codex-auth", network="codex-net",
                            model="gpt-5.6-sol", effort="high")
    cli = CodexSession(session_id="abc", workspace=tmp_path, backend=backend,
                       sandbox=SandboxConfig(memory="3g", cpus="1.5", pids=321), system_prompt="system",
                       backend_session_id="resume-me")
    command = cli.command()
    assert command[:6] == ["docker", "run", "--rm", "-i", "--name", "harness-abc-codex"]
    for pair in (["--network", "codex-net"], ["-e", "HTTPS_PROXY=http://proxy:8888"],
                 ["-e", "CODEX_HOME=/home/agent/.codex"], ["-v", "codex-auth:/home/agent/.codex"],
                 ["--memory", "3g"], ["--cpus", "1.5"], ["--pids-limit", "321"]):
        assert any(command[at:at + 2] == pair for at in range(len(command) - 1))
    assert command[-3:] == ["codex", "app-server", "--stdio"]
    assert ["--security-opt", "seccomp=unconfined"] == command[
        command.index("seccomp=unconfined") - 1:command.index("seccomp=unconfined") + 1]
    assert not any("bypass" in arg or "danger-full-access" in arg for arg in command)
    keyed = CodexSession(session_id="keyed", workspace=tmp_path, backend=backend, sandbox=SandboxConfig(),
                         system_prompt="system", api_key="secret-value").command()
    assert ["-e", "OPENAI_API_KEY"] == keyed[keyed.index("OPENAI_API_KEY") - 1:keyed.index("OPENAI_API_KEY") + 1]
    assert "secret-value" not in keyed


def test_codex_file_approval_auto_allows_and_maps_events(tmp_path):
    async def body():
        m, made, state = _codex_manager(tmp_path, "file")
        await m.start()
        s = await wait_status(m, m.create("edit it", backend="codex")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"] == "accept" and s["run"]["backend_session_id"] == "codex-thread-1"
        assert s["run"]["rate_limits"]["utilization"] == 0.28
        assert s["run"]["rate_limits"]["rateLimitType"] == "seven_day"
        assert s["totals"] == {"turns": 1, "prompt_tokens": 12, "completion_tokens": 4,
                                "total_cost_usd": 0.0}
        assert events(m, s["id"], "tool_result")[0]["name"] == "apply_patch"
        sent = state.read_text(encoding="utf-8")
        assert '"method": "thread/start"' in sent and '"decision": "accept"' in sent
        assert '"sandbox": "workspace-write"' in sent and '"effort": "high"' in sent
        assert '"approvalPolicy": "on-request"' in sent
        assert made[0]["model"] == "gpt-5.6-sol"
        await m.stop()
    asyncio.run(body())


def test_codex_command_approval_and_restart_recovery(tmp_path):
    async def body():
        m1, _, state = _codex_manager(tmp_path, "command")
        await m1.start()
        sid = m1.create("test it", backend="codex")["id"]
        await wait_status(m1, sid, "waiting_approval")
        pending = m1.db.pending_approvals(sid)
        assert len(pending) == 1 and pending[0]["tool"] == "exec_command"
        aid = pending[0]["id"]
        await m1.stop()
        m1.db.close()

        m2, made, _ = _codex_manager(tmp_path, "command", state=state)
        await m2.start()
        for _ in range(500):
            if made:
                break
            await asyncio.sleep(0.02)
        assert made
        await wait_status(m2, sid, "waiting_approval")
        assert made[0]["backend_session_id"] == "codex-thread-1"
        assert m2.db.pending_approvals(sid)[0]["id"] == aid
        m2.decide(sid, aid, approve=True)
        s = await wait_status(m2, sid, "done")
        assert s["answer"] == "accept" and len(m2.db.approvals(sid)) == 1
        sent = state.read_text(encoding="utf-8")
        assert '"method": "thread/resume"' in sent
        assert "The harness restarted; continue the task." in sent
        await m2.stop()
    asyncio.run(body())


def test_codex_command_denial_reaches_app_server(tmp_path):
    async def body():
        m, _, state = _codex_manager(tmp_path, "command")
        await m.start()
        sid = m.create("test it", backend="codex")["id"]
        await wait_status(m, sid, "waiting_approval")
        approval = m.db.pending_approvals(sid)[0]
        m.decide(sid, approval["id"], approve=False, note="skip tests")
        s = await wait_status(m, sid, "done")
        assert s["answer"] == "decline"
        assert '"decision": "decline"' in state.read_text(encoding="utf-8")
        assert m.db.approvals(sid)[0]["note"] == "skip tests"
        await m.stop()
    asyncio.run(body())


def test_codex_inbox_cancel_and_policy(tmp_path):
    async def inbox():
        m, _, _ = _codex_manager(tmp_path / "inbox", "inbox")
        await m.start()
        sid = m.create("wait", backend="codex")["id"]
        await wait_status(m, sid, "running")
        await m.send(sid, "follow up")
        s = await wait_status(m, sid, "done")
        assert s["answer"] == "follow up" and s["inbox"] == []
        await m.stop()

    async def cancel():
        m, _, _ = _codex_manager(tmp_path / "cancel", "cancel")
        await m.start()
        sid = m.create("wait", backend="codex")["id"]
        await wait_status(m, sid, "running")
        assert (await m.cancel(sid))["status"] == "cancelled"
        await wait_cli_gone(m, sid)
        await m.stop()

    asyncio.run(inbox())
    asyncio.run(cancel())
    assert Policy().decide("exec_command", {"command": "pytest -q"}).action == ALLOW
    assert Policy().decide("exec_command", {"command": "curl https://example.com", "network": True}).action == ASK
    assert Policy(repo=True).decide("exec_command", {"command": "git push origin main"}).action != ALLOW
    assert Policy().decide("apply_patch", {"file_paths": ["/workspace/a.py"]}).action == ALLOW
    assert Policy().decide("apply_patch", {"file_paths": ["/workspace/a.py", "/etc/passwd"]}).action == ASK


# Cursor Agent stream-json backend (8a step 5)

FAKE_CURSOR = r'''import json
import pathlib
import sys
import time

mode = sys.argv[1]
state = pathlib.Path(sys.argv[2])
prompt = sys.argv[-1]
resumed = "--resume" in sys.argv
with state.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[3:], "prompt": prompt, "resumed": resumed}) + "\n")

def send(item):
    print(json.dumps(item), flush=True)

send({"type": "system", "subtype": "init", "apiKeySource": "login", "cwd": "/workspace",
      "session_id": "cursor-session-1", "model": "Cursor Grok 4.6", "permissionMode": "force"})

if mode == "cancel" or (mode == "recover" and not resumed):
    time.sleep(60)
    raise SystemExit(0)

if mode == "inbox" and not resumed:
    time.sleep(0.25)
    answer = "first turn"
elif mode == "inbox":
    answer = "followup received" if "follow up" in prompt else prompt
else:
    answer = "cursor done"

send({"type": "assistant", "message": {"role": "assistant", "content": [
      {"type": "text", "text": answer[:7]}]}, "session_id": "cursor-session-1", "timestamp_ms": 1})
send({"type": "assistant", "message": {"role": "assistant", "content": [
      {"type": "text", "text": answer[7:]}]}, "session_id": "cursor-session-1", "timestamp_ms": 2})
send({"type": "assistant", "message": {"role": "assistant", "content": [
      {"type": "text", "text": answer}]}, "session_id": "cursor-session-1"})
send({"type": "tool_call", "subtype": "started", "call_id": "tool-1", "tool_call": {
      "readToolCall": {"args": {"path": "README.md"}}}, "session_id": "cursor-session-1"})
send({"type": "tool_call", "subtype": "completed", "call_id": "tool-1", "tool_call": {
      "readToolCall": {"args": {"path": "README.md"}, "result": {"success": {
      "content": "demo", "isEmpty": False, "totalLines": 1}}}}, "session_id": "cursor-session-1"})
send({"type": "result", "subtype": "success", "is_error": False, "result": answer,
      "session_id": "cursor-session-1", "usage": {"inputTokens": 5, "outputTokens": 2,
      "cacheReadTokens": 2, "cacheWriteTokens": 0},
      "num_turns": 1, "total_cost_usd": 0})
'''


def _cursor_manager(tmp_path, mode: str, state=None, max_sessions=2):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_cursor.py"
    fake.write_text(FAKE_CURSOR, encoding="utf-8")
    state = state or tmp_path / "fake-cursor-state.jsonl"
    cfg = make_cfg(tmp_path)
    cfg.backends["cursor"] = BackendConfig(enabled=True, model="cursor-grok-4.6-high", effort="high",
                                            permission_mode="force", max_sessions=max_sessions)
    manager = Manager(cfg)
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return CursorSession(**kwargs, command=[sys.executable, "-u", str(fake), mode, str(state)])

    manager.runner.cursor_factory = factory
    return manager, made, state


def test_cursor_docker_command_is_sandboxed_forced_and_resumable(tmp_path):
    backend = BackendConfig(enabled=True, proxy="http://proxy:8888", volume="cursor-auth", network="cursor-net",
                            model="cursor-grok-4.6-high", effort="high", permission_mode="force")
    cli = CursorSession(session_id="abc", workspace=tmp_path, backend=backend,
                        sandbox=SandboxConfig(memory="3g", cpus="1.5", pids=321), system_prompt="system",
                        backend_session_id="resume-me")
    command = cli.command("do it")
    assert command[:6] == ["docker", "run", "--rm", "-i", "--name", "harness-abc-cursor"]
    for pair in (["--network", "cursor-net"], ["-e", "HTTPS_PROXY=http://proxy:8888"],
                 ["-e", "HOME=/home/agent/.cursor/home"],
                 ["-e", "CURSOR_CONFIG_DIR=/home/agent/.cursor/config"],
                 ["-v", "cursor-auth:/home/agent/.cursor"], ["--memory", "3g"], ["--cpus", "1.5"],
                 ["--pids-limit", "321"], ["--sandbox", "enabled"], ["--workspace", "/workspace"],
                 ["--model", "cursor-grok-4.6-high"], ["--resume", "resume-me"]):
        assert any(command[at:at + 2] == pair for at in range(len(command) - 1))
    assert ["--security-opt", "seccomp=unconfined"] == command[
        command.index("seccomp=unconfined") - 1:command.index("seccomp=unconfined") + 1]
    assert ["--security-opt", "apparmor=unconfined"] == command[
        command.index("apparmor=unconfined") - 1:command.index("apparmor=unconfined") + 1]
    assert "--force" in command and "--trust" in command and "--auto-review" not in command and "--yolo" not in command
    assert command[-1] == "do it"
    keyed = CursorSession(session_id="keyed", workspace=tmp_path, backend=backend, sandbox=SandboxConfig(),
                          system_prompt="system", api_key="secret-value").command("task")
    assert ["-e", "CURSOR_API_KEY"] == keyed[keyed.index("CURSOR_API_KEY") - 1:keyed.index("CURSOR_API_KEY") + 1]
    assert "secret-value" not in keyed


def test_cursor_maps_stream_events_and_usage(tmp_path):
    async def body():
        m, made, state = _cursor_manager(tmp_path, "normal")
        await m.start()
        s = await wait_status(m, m.create("inspect it", backend="cursor")["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"] == "cursor done" and s["run"]["backend_session_id"] == "cursor-session-1"
        assert s["run"]["tool_calls"] == 1 and s["run"]["tool_errors"] == 0
        assert s["totals"] == {"turns": 1, "prompt_tokens": 5, "completion_tokens": 2,
                                "total_cost_usd": 0.0}
        assert events(m, s["id"], "assistant")[-1]["content"] == "cursor done"
        tool = events(m, s["id"], "tool_result")[0]
        assert tool["name"] == "readToolCall" and tool["ok"] and "demo" in tool["output"]
        assert "User task:\\ninspect it" in state.read_text(encoding="utf-8")
        assert made[0]["model"] == "cursor-grok-4.6-high"
        assert m.db.approvals(s["id"]) == []
        await m.stop()
    asyncio.run(body())


def test_cursor_queues_live_inbox_as_resumed_turn(tmp_path):
    async def body():
        m, _, state = _cursor_manager(tmp_path, "inbox")
        await m.start()
        sid = m.create("first", backend="cursor")["id"]
        await wait_status(m, sid, "running")
        await m.send(sid, "follow up")
        s = await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"] == "followup received" and s["inbox"] == []
        assert s["totals"]["turns"] == 2 and s["totals"]["prompt_tokens"] == 10
        records = [json.loads(line) for line in state.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 2 and records[1]["resumed"]
        assert records[1]["argv"][-3:-1] == ["--resume", "cursor-session-1"]
        await m.stop()
    asyncio.run(body())


def test_cursor_cancel_and_restart_recovery(tmp_path):
    async def cancel():
        m, _, _ = _cursor_manager(tmp_path / "cancel", "cancel")
        await m.start()
        sid = m.create("wait", backend="cursor")["id"]
        await wait_status(m, sid, "running")
        assert (await m.cancel(sid))["status"] == "cancelled"
        await wait_cli_gone(m, sid)
        await m.stop()

    async def recover():
        root = tmp_path / "recover"
        m1, _, state = _cursor_manager(root, "recover")
        await m1.start()
        sid = m1.create("wait", backend="cursor")["id"]
        for _ in range(500):
            s = m1.db.get_session(sid)
            if s["run"].get("backend_session_id"):
                break
            await asyncio.sleep(0.02)
        assert s["run"]["backend_session_id"] == "cursor-session-1"
        await m1.stop()
        m1.db.close()

        m2, made, _ = _cursor_manager(root, "recover", state=state)
        await m2.start()
        s = await wait_status(m2, sid, "done")
        await asyncio.gather(*m2.tasks.values())
        assert s["answer"] == "cursor done" and made[0]["backend_session_id"] == "cursor-session-1"
        records = [json.loads(line) for line in state.read_text(encoding="utf-8").splitlines()]
        assert records[-1]["resumed"] and "harness restarted" in records[-1]["prompt"].lower()
        await m2.stop()

    asyncio.run(cancel())
    asyncio.run(recover())
