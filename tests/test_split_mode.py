"""Split mode for hosted CLIs (#427): the provider token stays in the CLI container, the shell and files are the
harness MCP server's, running on the session sandbox."""

from __future__ import annotations

import asyncio

import pytest

from harness import config as config_module
from harness import sandbox as sandbox_module
from harness.cli_backends import (CLAUDE_SPLIT_TOOLS, CODEX_SPLIT_OVERRIDES, ClaudeSession, CliBackendError,
                                  CodexSession)
from harness.config import BackendConfig, SandboxConfig
from harness.manager import Manager
from harness.mcp_server import SPLIT_TOOLS, TOKEN_ENV, McpRelay
from harness.policy import ALLOW
from harness.policy import Policy
from test_daemon import make_cfg, wait_status


def _backend(**kw):
    return BackendConfig(enabled=True, proxy="http://proxy:8888", volume="auth", network="cli-net", **kw)


def _session(cls, tmp_path, split, **kw):
    relay = McpRelay(session_id="abc", backend=_backend(), server=None)
    return cls(session_id="abc", workspace=tmp_path, backend=_backend(permission_mode="default"),
               sandbox=SandboxConfig(), system_prompt="sys", mcp=relay, mcp_token="t", split=split, **kw)


MODELS = "models:\n  fake:\n    base_url: http://unused\n    context_tokens: 4096\n"


def test_tool_mode_defaults_to_builtin_and_is_validated(tmp_path):
    assert BackendConfig().tool_mode == "builtin"
    path = tmp_path / "harness.yaml"
    path.write_text(MODELS + "backends:\n  claude:\n    tool_mode: split\n", encoding="utf-8")
    assert config_module.load(tmp_path, data_dir=tmp_path / "d").backends["claude"].tool_mode == "split"
    for body in ("backends:\n  claude:\n    tool_mode: sideways\n", "backends:\n  cursor:\n    tool_mode: split\n",
                 "backends:\n  claude:\n    tool_mode: split\n    mcp: false\n"):
        path.write_text(MODELS + body, encoding="utf-8")
        with pytest.raises(ValueError, match="tool_mode"):
            config_module.load(tmp_path, data_dir=tmp_path / "d")


def test_claude_split_container_has_the_token_but_no_workspace_or_builtin_shell(tmp_path):
    command = _session(ClaudeSession, tmp_path, True, api_key="sk-secret").command()
    assert not any("target=/workspace" in arg or str(tmp_path) in arg for arg in command)
    assert command[command.index("--tools") + 1] == CLAUDE_SPLIT_TOOLS == "Task,TodoWrite"
    assert "--strict-mcp-config" in command
    assert "mcp__harness__run_shell" in command[command.index("--append-system-prompt") + 1]
    assert "ANTHROPIC_API_KEY" in command  # the brain keeps its credential, by name only
    assert "sk-secret" not in " ".join(command)
    builtin = _session(ClaudeSession, tmp_path, False).command()
    assert any("target=/workspace" in arg for arg in builtin) and "--tools" not in builtin


def test_codex_split_container_has_no_workspace_no_environment_and_keeps_the_plan_tool(tmp_path):
    cli = _session(CodexSession, tmp_path, True, api_key="sk-secret")
    command = cli.command()
    assert not any("target=/workspace" in arg or str(tmp_path) in arg for arg in command)
    overrides = {command[i + 1] for i, arg in enumerate(command) if arg == "-c"}
    assert set(CODEX_SPLIT_OVERRIDES) <= overrides
    assert "features.shell_tool=false" in overrides and "tools.update_plan.enabled=false" not in overrides
    assert cli._turn_start_params("hi")["environments"] == [] and cli._approval_policy() == "on-request"
    assert "environments" not in _session(CodexSession, tmp_path, False)._turn_start_params("hi")


def test_split_is_never_combined_with_tools_only(tmp_path):
    assert _session(ClaudeSession, tmp_path, True, tools_only=True).split is False
    assert _session(CodexSession, tmp_path, True, tools_only=True).split is False


def test_hands_container_gets_no_environment(tmp_path, monkeypatch):
    seen = []

    async def fake_run_cmd(args, timeout=60, input_=None, env=None):
        seen.append(args)
        if args[:2] == ["docker", "inspect"]:
            return 1, "", ""
        return 0, "", ""
    monkeypatch.setattr(sandbox_module, "run_cmd", fake_run_cmd)
    box = sandbox_module.Sandbox("abc", tmp_path, SandboxConfig())
    asyncio.run(box.ensure_running())
    run = next(args for args in seen if args[:3] == ["docker", "run", "-d"])
    assert "-e" not in run and "--env" not in run and "--env-file" not in run
    assert TOKEN_ENV not in " ".join(run)


def test_killed_hands_container_is_recreated_for_the_next_call(tmp_path, monkeypatch):
    state = {"container": "running", "created": 0}
    commands = []

    async def fake_run_cmd(args, timeout=60, input_=None, env=None):
        if args[:2] == ["docker", "inspect"]:
            return (0, state["container"], "") if state["container"] else (1, "", "no such container")
        if args[:3] == ["docker", "run", "-d"]:
            state["container"], state["created"] = "running", state["created"] + 1
            return 0, "id", ""
        if args[:2] == ["docker", "exec"]:
            commands.append(args[-1])
            return 0, "ok", ""
        return 0, "", ""
    monkeypatch.setattr(sandbox_module, "run_cmd", fake_run_cmd)
    box = sandbox_module.Sandbox("abc", tmp_path, SandboxConfig())
    assert asyncio.run(box.exec("echo one")) == (0, "ok")
    state["container"] = ""  # the hands container died mid-command
    code, out = asyncio.run(box.exec("echo two"))
    assert code == 0 and out.endswith("ok") and "environment recreated" in out  # the model is told (#429)
    assert state["created"] == 1 and commands == ["echo one", "echo two"]


def _split_manager(tmp_path, mode="split"):
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", tool_mode=mode)
    return Manager(cfg)


class FakeCli:
    def __init__(self):
        self.answers = []

    async def respond_permission(self, request_id, behavior, args, message=""):
        self.answers.append((request_id, behavior, message))


class RecordingSandbox:
    def __init__(self):
        self.commands = []

    async def exec(self, command, timeout=120, network=False):
        self.commands.append((command, network))
        return 0, "hello\n"


async def _failed_session(m):
    m.runner.cli_factory = lambda **kw: (_ for _ in ()).throw(CliBackendError("stop here"))
    await m.start()
    sid = m.create("x", backend="claude")["id"]
    await wait_status(m, sid, "failed")
    await asyncio.gather(*m.tasks.values())
    return sid


def test_split_shell_and_file_calls_need_a_grant_and_run_on_the_sandbox(tmp_path):
    async def body():
        m = _split_manager(tmp_path)
        sid = await _failed_session(m)
        assert set(SPLIT_TOOLS) <= {t["function"]["name"] for t in m.runner.mcp_tool_schemas(sid)}
        box = RecordingSandbox()
        m.runner.sandbox = lambda s: box
        s, cli = m.db.get_session(sid), FakeCli()
        run = {"command": "echo hello"}
        denied = await m.runner.mcp_call(sid, "run_shell", run, "toolu-1")  # no grant: refused, nothing runs
        assert denied[1] is False and "not allowed" in denied[0] and box.commands == []
        assert await m.runner._ask_cli_policy(s, cli, "r1", {}, "mcp__harness__run_shell", run, "toolu-1") is None
        assert cli.answers == [("r1", "allow", "")]
        text, ok = await m.runner.mcp_call(sid, "run_shell", run, "toolu-1")
        assert ok and "hello" in text and box.commands == [("echo hello", False)]
        assert (await m.runner.mcp_call(sid, "run_shell", run, "toolu-1"))[1] is False  # one use per grant
        write = {"path": "a.txt", "content": "hi"}
        await m.runner._ask_cli_policy(s, cli, "r2", {}, "mcp__harness__write_file", write, "toolu-2")
        assert (await m.runner.mcp_call(sid, "write_file", write, "toolu-2"))[1] is True
        assert (m.runner.split_workspace(s).root / "a.txt").read_text() == "hi"
        assert m.db.get_session(sid)["run"]["files_touched"] == ["a.txt"]
        bad = {"command": "ls", "bogus": 1}
        await m.runner._ask_cli_policy(s, cli, "r3", {}, "mcp__harness__run_shell", bad, "toolu-3")
        text, ok = await m.runner.mcp_call(sid, "run_shell", bad, "toolu-3")
        assert not ok and "unknown argument" in text
        await m.stop()
    asyncio.run(body())


def test_shell_over_mcp_is_decided_as_the_native_tool():
    assert Policy().decide("mcp__harness__run_shell", {"command": "ls"}).action == ALLOW
    rules = Policy([{"tool": "run_shell", "action": "deny", "reason": "no shell"}])
    assert rules.decide("mcp__harness__run_shell", {"command": "ls"}).action == "deny"


def test_builtin_mode_serves_no_shell_over_mcp(tmp_path):
    async def body():
        m = _split_manager(tmp_path, "builtin")
        sid = await _failed_session(m)
        assert not set(SPLIT_TOOLS) & {t["function"]["name"] for t in m.runner.mcp_tool_schemas(sid)}
        await m.stop()
    asyncio.run(body())


def test_split_session_without_mcp_refuses_instead_of_falling_back(tmp_path):
    async def body():
        m = _split_manager(tmp_path)
        made = []
        m.runner.cli_factory = lambda **kw: made.append(kw) or (_ for _ in ()).throw(CliBackendError("stop here"))
        real = m.runner.mcp_tool_schemas
        m.runner.mcp_tool_schemas = lambda sid: []  # nothing to serve, so no relay is made
        await m.start()
        sid = m.create("x", backend="claude")["id"]
        await wait_status(m, sid, "failed")
        await asyncio.gather(*m.tasks.values())
        assert not made and m.db.get_session(sid)["run"]["failure"]["code"] == "split_unavailable"
        m.runner.mcp_tool_schemas = real
        await m.stop()
    asyncio.run(body())


def test_hosted_bakeoff_arm_runs_a_task_per_mode_and_summarizes(tmp_path, monkeypatch):
    from bakeoff import hosted
    from bakeoff.tasks import TASKS

    task = next(t for t in TASKS if t.setup is None)
    monkeypatch.setattr(hosted, "grade_hard_task", lambda *a: (True, "graded"))
    monkeypatch.setattr(hosted, "prepare_hard_task", lambda t, ws: {})

    def manager(cfg):
        m = Manager(cfg)
        m.runner.cli_factory = lambda **kw: (_ for _ in ()).throw(CliBackendError("no real CLI in tests"))
        return m
    rows = []
    for mode in ("builtin", "split"):
        run_dir = tmp_path / mode
        run_dir.mkdir()
        rows.append(asyncio.run(hosted.run_hosted(task, "claude", mode, run_dir, _backend(), poll=0.05,
                                                  manager_factory=manager)))
    assert [r["mode"] for r in rows] == ["builtin", "split"]
    assert all(r["status"] == "failed" and r["ok"] is False for r in rows)  # the CLI never ran
    summary = hosted.summarize(rows + [{**rows[0], "ok": True, "turns": 4}])
    overall = {(r["mode"], r["task"]): r for r in summary}[("builtin", "*")]
    assert overall["runs"] == 2 and overall["pass_rate"] == 0.5
    assert "builtin" in hosted.render(summary) and "split" in hosted.render(summary)
    assert hosted.throwaway_config(tmp_path, "claude", _backend(), "split").backends["claude"].tool_mode == "split"
