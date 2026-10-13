"""The bash-only tool profile for hosted CLIs (#638): argv per backend and profile, validation, run and usage records,
and the comparison script. No CLI is launched and nothing is hosted."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bakeoff import tool_profile_compare as compare
from harness import tool_profiles
from harness.api import create_app
from harness.cli_backends import (BASH_ONLY_SPLIT_NOTE, CLAUDE_SPLIT_TOOLS, SPLIT_NOTE, ClaudeSession,
                                  CliBackendError, CodexSession)
from harness.config import BackendConfig, SandboxConfig
from harness.db import Database
from harness.manager import HarnessError, Manager
from harness.mcp_server import McpRelay
from harness.metrics import render as render_metrics
from test_daemon import make_cfg, wait_status

FIXTURE = Path(__file__).resolve().parent.parent / "bakeoff" / "tool_profile_fixture.json"


def _backend(**kw):
    return BackendConfig(enabled=True, proxy="http://proxy:8888", volume="auth", network="cli-net", **kw)


def _cli(cls, tmp_path, *, split=False, profile="default", mcp=True):
    relay = McpRelay(session_id="abc", backend=_backend(), server=None) if mcp else None
    return cls(session_id="abc", workspace=tmp_path, backend=_backend(permission_mode="default"),
               sandbox=SandboxConfig(), system_prompt="sys", mcp=relay, mcp_token="t" if mcp else "",
               split=split, tool_profile=profile)


def _tools(command):
    return [command[i + 1] for i, arg in enumerate(command) if arg == "--tools"]


# argv construction

def test_claude_builtin_bash_only_passes_tools_bash_and_nothing_else(tmp_path):
    assert _tools(_cli(ClaudeSession, tmp_path, profile="bash-only").command()) == ["Bash"]
    assert _tools(_cli(ClaudeSession, tmp_path, profile="bash-only", mcp=False).command()) == ["Bash"]
    assert _tools(_cli(ClaudeSession, tmp_path).command()) == []  # the default keeps every built-in


def test_claude_split_bash_only_keeps_the_split_flags_and_says_run_shell_is_the_only_tool(tmp_path):
    command = _cli(ClaudeSession, tmp_path, split=True, profile="bash-only").command()
    assert _tools(command) == [CLAUDE_SPLIT_TOOLS]  # no workspace tools either way; the MCP server narrows
    prompt = command[command.index("--append-system-prompt") + 1]
    assert prompt.endswith(BASH_ONLY_SPLIT_NOTE) and "read_file" not in prompt
    default = _cli(ClaudeSession, tmp_path, split=True).command()
    assert default[default.index("--append-system-prompt") + 1].endswith(SPLIT_NOTE)


def test_tools_only_wins_over_bash_only(tmp_path):
    relay = McpRelay(session_id="abc", backend=_backend(), server=None)
    cli = ClaudeSession(session_id="abc", workspace=tmp_path, backend=_backend(), sandbox=SandboxConfig(),
                        system_prompt="sys", mcp=relay, mcp_token="t", tools_only=True, tool_profile="bash-only")
    assert _tools(cli.command()) == [""]


def test_codex_split_bash_only_needs_no_vendor_flag(tmp_path):
    bash_only = _cli(CodexSession, tmp_path, split=True, profile="bash-only")
    default = _cli(CodexSession, tmp_path, split=True)
    assert bash_only.command() == default.command()
    assert bash_only._turn_start_params("hi")["environments"] == []


def test_codex_split_bash_only_tells_the_model_its_only_tool_is_run_shell(tmp_path):
    sent = []

    async def request(method, params):
        sent.append((method, params))
        return {"thread": {"id": "th-1"}, "turn": {"id": "tu-1"}}

    async def write(_msg):
        return None
    cli = _cli(CodexSession, tmp_path, split=True, profile="bash-only")
    cli._request, cli._write = request, write
    asyncio.run(cli.initialize("go"))
    start = dict(sent)["thread/start"]
    assert start["developerInstructions"].endswith(BASH_ONLY_SPLIT_NOTE) and start["environments"] == []


# what each backend supports

@pytest.mark.parametrize("name, backend, kind, ok", [
    ("claude", _backend(), "agent", True),
    ("claude", _backend(tool_mode="split"), "agent", True),
    ("codex", _backend(tool_mode="split"), "agent", True),
    ("codex", _backend(), "agent", False),
    ("cursor", _backend(), "agent", False),
    ("opencode", _backend(kind="cli"), "agent", False),
    ("local", None, "agent", False),
    ("claude", _backend(), "chat", False),
    ("claude", _backend(), "tools_only", False),
])
def test_bash_only_support_per_backend(name, backend, kind, ok):
    reason = tool_profiles.refusal(name, backend, "bash-only", kind)
    assert (reason == "") is ok
    assert tool_profiles.refusal(name, backend, "default", kind) == ""
    if kind == "agent":
        assert ("bash-only" in tool_profiles.supported(name, backend)) is ok


def test_unknown_profile_and_reasons_name_the_fix():
    assert "must be one of" in tool_profiles.refusal("claude", _backend(), "zsh-only")
    assert "split mode" in tool_profiles.refusal("codex", _backend(), "bash-only")
    assert "local model" in tool_profiles.refusal("local", None, "bash-only")
    assert "can't restrict" in tool_profiles.refusal("cursor", _backend(), "bash-only")


# session creation, API and run records

def _manager(tmp_path, **claude):
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", **claude)
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.5")
    cfg.backends["cursor"] = BackendConfig(enabled=True, model="auto")
    m = Manager(cfg)
    made = []

    def factory(**kw):
        made.append(kw)
        raise CliBackendError("no real CLI in tests")
    m.runner.cli_factory = m.runner.codex_factory = m.runner.cursor_factory = factory
    return m, made


def test_create_rejects_bash_only_where_tools_cannot_be_restricted(tmp_path):
    m, _ = _manager(tmp_path)
    for backend in ("local", "codex", "cursor"):
        with pytest.raises(HarnessError) as e:
            m.create("x", backend=backend, tool_profile="bash-only")
        assert e.value.status == 400 and e.value.code == tool_profiles.UNSUPPORTED
    with pytest.raises(HarnessError, match="must be one of"):
        m.create("x", backend="claude", tool_profile="nope")
    assert m.db.list_sessions(10) == []


def test_bash_only_reaches_the_claude_factory_and_the_session(tmp_path):
    async def body():
        m, made = _manager(tmp_path)
        await m.start()
        sid = m.create("x", backend="claude", tool_profile="bash-only")["id"]
        await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        assert made[-1]["tool_profile"] == "bash-only" and "split" not in made[-1]
        assert m.summary(m.db.get_session(sid))["tool_profile"] == "bash-only"
        plain = m.create("x", backend="claude")["id"]
        await wait_status(m, plain, "failed")
        await asyncio.gather(*m.tasks.values())
        assert "tool_profile" not in made[-1] and m.db.get_session(plain)["tool_profile"] == "default"
        await m.stop()
    asyncio.run(body())


def test_split_bash_only_serves_and_runs_only_run_shell(tmp_path):
    async def body():
        m, made = _manager(tmp_path, tool_mode="split")
        await m.start()
        sid = m.create("x", backend="claude", tool_profile="bash-only")["id"]
        await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        assert made[-1]["split"] is True and made[-1]["tool_profile"] == "bash-only"
        served = {t["function"]["name"] for t in m.runner.mcp_tool_schemas(sid)}
        assert "run_shell" in served and not served & {"read_file", "write_file", "edit_file", "search", "list_files"}
        s = m.db.get_session(sid)
        write = {"path": "a.txt", "content": "hi"}
        m.runner._grant_mcp(sid, "mcp__harness__write_file", write, "toolu-1")  # even a granted call is refused
        text, ok = await m.runner.mcp_call(sid, "write_file", write, "toolu-1")
        assert not ok and "unknown tool" in text and not (m.runner.split_workspace(s).root / "a.txt").exists()
        await m.stop()
    asyncio.run(body())


def test_a_run_refuses_when_codex_left_split_mode_after_creation(tmp_path):
    async def body():
        m, made = _manager(tmp_path)
        m.cfg.backends["codex"].tool_mode = "split"
        sid = m.create("x", backend="codex", tool_profile="bash-only")["id"]
        m.cfg.backends["codex"].tool_mode = "builtin"  # the owner changed the config while it queued
        await m.start()
        await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        assert not made and m.db.get_session(sid)["run"]["failure"]["code"] == tool_profiles.UNSUPPORTED
        await m.stop()
    asyncio.run(body())


def test_api_takes_tool_profile_and_lists_what_each_backend_accepts(tmp_path):
    m, _ = _manager(tmp_path)
    with TestClient(create_app(m)) as client:
        made = client.post("/sessions", json={"prompt": "x", "backend": "claude", "tool_profile": "bash-only"})
        assert made.status_code == 201 and made.json()["tool_profile"] == "bash-only"
        refused = client.post("/sessions", json={"prompt": "x", "backend": "cursor", "tool_profile": "bash-only"})
        assert refused.status_code == 400 and refused.json()["error"]["code"] == tool_profiles.UNSUPPORTED
        assert client.post("/sessions", json={"prompt": "x"}).json()["tool_profile"] == "default"
        backends = {b["name"]: b for b in client.get("/backends?auth=skip").json()}
        assert backends["claude"]["tool_profiles"] == ["default", "bash-only"]
        assert backends["cursor"]["tool_profiles"] == ["default"] == backends["codex"]["tool_profiles"]
        assert backends["local"]["tool_profiles"] == ["default"]


def test_usage_and_cost_group_by_profile(tmp_path):
    m, _ = _manager(tmp_path)
    m.db.record_usage("claude", "s1", "", 100, 10, 0.25, "subscription", tool_profile="bash-only")
    m.db.record_usage("claude", "s2", "", 300, 30, 0.75, "subscription")
    m.db.record_usage("claude", "s3", "", 100, 10, 0.5, "subscription", tool_profile="bash-only")
    by_profile = m.db.usage_by_profile("claude", 0)
    assert by_profile["bash-only"] == {"requests": 2, "prompt_tokens": 200, "completion_tokens": 20, "cost_usd": 0.75}
    assert by_profile["default"]["cost_usd"] == 0.75
    metrics = render_metrics(m)
    assert 'harness_backend_cost_usd_total{backend="claude",tool_profile="bash-only"} 0.75' in metrics
    assert 'harness_backend_cost_usd_total{backend="codex",tool_profile="default"} 0' in metrics


# the comparison script

def test_comparison_groups_by_task_type_and_counts_localisation_failures():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"]
    summary = {(g["task_type"], g["tool_profile"]): g for g in compare.summarize(rows)}
    assert list(summary)[:2] == [("shell", "default"), ("shell", "bash-only")]
    edit = summary[("edit", "bash-only")]
    assert (edit["runs"], edit["successes"], edit["localisation_failures"], edit["localisation_checked"]) == (2, 1, 1, 2)
    assert summary[("edit", "default")]["localisation_failures"] == 0
    assert summary[("shell", "bash-only")]["localisation_checked"] == 0  # shell tasks name no files
    assert summary[("all", "bash-only")]["cost_usd"] == pytest.approx(0.7332)
    assert summary[("shell", "default")]["cost_per_success"] == pytest.approx(0.3901)
    text = compare.render(compare.summarize(rows))
    assert "loc. fail" in text and "1/2" in text


def test_comparison_reads_the_harness_database(tmp_path, capsys):
    db = Database(tmp_path / "harness.sqlite3")
    for sid, profile, status, touched in (("s-def", "default", "done", ["src/a.py"]),
                                          ("s-bash", "bash-only", "failed", [])):
        db.insert_session({"id": sid, "title": "t", "project": "p", "target": "tower", "model": "m",
                           "backend": "claude", "status": status, "workspace": str(tmp_path / "gone"),
                           "created_at": 1.0, "updated_at": 1.0, "context": [], "tool_profile": profile,
                           "run": {"files_touched": touched}})
        db.record_usage("claude", sid, "", 1000, 100, 0.5, "subscription", tool_profile=profile)
    db.record_usage("claude", "s-def", "", 500, 50, 0.25, "subscription")
    db.close()
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"runs": [
        {"session_id": "s-def", "task": "fix", "task_type": "edit", "expected_files": ["src/a.py"]},
        {"session_id": "s-bash", "task": "fix", "task_type": "edit", "expected_files": ["src/a.py"]}]}),
        encoding="utf-8")
    assert compare.main(["--db", str(tmp_path / "harness.sqlite3"), "--tasks", str(tasks), "--json"]) == 0
    summary = {(g["task_type"], g["tool_profile"]): g for g in json.loads(capsys.readouterr().out)}
    default, bash = summary[("edit", "default")], summary[("edit", "bash-only")]
    assert (default["successes"], default["prompt_tokens"], default["cost_usd"]) == (1, 1500, 0.75)
    assert (default["localisation_failures"], bash["localisation_failures"], bash["successes"]) == (0, 1, 0)


def test_rerun_keeps_the_tool_profile(tmp_path):
    async def body():
        m, _ = _manager(tmp_path)
        await m.start()
        sid = m.create("x", backend="claude", tool_profile="bash-only")["id"]
        await wait_status(m, sid, "failed")
        again = m.rerun(sid)
        assert again["id"] != sid and again["tool_profile"] == "bash-only"
        await wait_status(m, again["id"], "failed")
        await asyncio.gather(*m.tasks.values())
        await m.stop()
    asyncio.run(body())


def test_comparison_reads_sessions_from_the_web_store_and_usage_from_the_main_store(tmp_path, capsys):
    main = Database(tmp_path / "harness.sqlite3")
    main.record_usage("claude", "s-web", "", 700, 70, 0.35, "subscription", tool_profile="bash-only")
    main.close()
    web = Database(tmp_path / "apps" / "app-web" / "harness.sqlite3")
    web.insert_session({"id": "s-web", "title": "t", "project": "p", "target": "tower", "model": "m",
                        "backend": "claude", "status": "done", "workspace": "", "created_at": 1.0, "updated_at": 1.0,
                        "context": [], "tool_profile": "bash-only", "run": {}})
    web.close()
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"runs": [{"session_id": "s-web", "task": "logs", "task_type": "shell"}]}),
                     encoding="utf-8")
    assert compare.main(["--db", str(tmp_path / "harness.sqlite3"), "--tasks", str(tasks), "--json"]) == 0
    [shell, _] = json.loads(capsys.readouterr().out)
    assert (shell["tool_profile"], shell["prompt_tokens"], shell["cost_usd"]) == ("bash-only", 700, 0.35)


def test_comparison_reads_a_dormant_app_store_that_has_not_migrated_yet(tmp_path, capsys):
    Database(tmp_path / "harness.sqlite3").close()
    store = tmp_path / "apps" / "app-abc" / "harness.sqlite3"
    app = Database(store)
    app.insert_session({"id": "s-app", "title": "t", "project": "p", "target": "tower", "model": "m",
                        "backend": "claude", "status": "done", "workspace": "", "created_at": 1.0, "updated_at": 1.0,
                        "context": [], "run": {}})
    app.close()
    import sqlite3
    conn = sqlite3.connect(store)
    conn.execute("ALTER TABLE sessions DROP COLUMN tool_profile")  # as before the 0059 migration ran
    conn.commit()
    conn.close()
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"runs": [{"session_id": "s-app", "task": "logs", "task_type": "shell"}]}),
                     encoding="utf-8")
    assert compare.main(["--db", str(tmp_path / "harness.sqlite3"), "--tasks", str(tasks), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["tool_profile"] == "default"


def test_workspace_files_without_git_are_the_files_written_since_the_session_began(tmp_path):
    import os
    (tmp_path / "old.txt").write_text("before", encoding="utf-8")
    os.utime(tmp_path / "old.txt", (1.0, 1.0))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "new.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "new.pyc").write_bytes(b"")
    assert compare.workspace_files(tmp_path, "", since=100.0) == {"src/new.py"}


def test_member_keys_say_which_profiles_each_backend_takes(tmp_path):
    m, _ = _manager(tmp_path)
    m.cfg.backends["codex"].tool_mode = "split"
    keys = {k["backend"]: k["tool_profiles"] for k in m.member_keys_status("u1")["keys"]}
    assert keys == {"claude": ["default", "bash-only"], "codex": ["default", "bash-only"]}


def test_idempotency_digest_is_unchanged_for_the_default_profile():
    """A retry across the upgrade still replays: the digest of a request without tool_profile matches #462's."""
    from harness import idempotency
    from harness.apps import CreateAppSession, _idempotency_record
    key = {"id": "app-1", "kind": "app"}
    body = CreateAppSession(prompt="hi", backend="claude")
    before = body.model_dump(mode="json", exclude={"tool_profile"})  # what the digest covered before #638
    record = _idempotency_record(key, body, "k1")
    assert record is not None and record["request_digest"] == idempotency.request_digest(before)
    bash = _idempotency_record(key, CreateAppSession(prompt="hi", backend="claude", tool_profile="bash-only"), "k1")
    assert bash is not None and bash["request_digest"] != record["request_digest"]
