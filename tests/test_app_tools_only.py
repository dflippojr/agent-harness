"""App-tools-only sessions (#329): only the App's own tools, no workspace, project, built-in or CLI tools."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.apps import AppTool
from harness.cli_backends import ClaudeSession
from harness.config import BackendConfig, SandboxConfig
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from harness.mcp_server import McpRelay, relay_node_args
from harness.policy import ALLOW, DENY, TOOLS_ONLY, AppToolsPolicy
from harness.warmup import LOW_MEMORY, PAUSED, SLEEPING

from test_daemon import Script, call, events, make_cfg, wait_status
from test_mcp_server import FAKE_MCP_CLAUDE, NODE, _free_port, _log
from test_phase6 import wait_for

BALANCE = {"name": "get_balance", "description": "Balance of one account",
           "parameters": {"type": "object", "properties": {"account": {"type": "string"}},
                          "required": ["account"]}}


def test_policy_allows_only_the_apps_tools_and_never_asks():
    policy = AppToolsPolicy(["get_balance", "list_accounts"])
    assert policy.decide("get_balance", {}).action == ALLOW
    assert policy.decide("mcp__harness__list_accounts", {}).action == ALLOW
    for name in ("run_shell", "read_file", "Bash", "Read", "WebFetch", "Task", "web_search", "memory_read",
                 "mcp__harness__web_search", "mcp__other__get_balance", "generate_image", "read_artifact"):
        decision = policy.decide(name, {"command": "ls"})
        assert decision.action == DENY and "App-tools-only" in decision.reason, name
    assert policy.fingerprint() == AppToolsPolicy(["list_accounts", "get_balance"]).fingerprint()


class RecordingScript(Script):
    """Script that also keeps the tool schemas each model call was offered."""

    def __init__(self, steps):
        super().__init__(steps)
        self.tools: list = []

    async def __call__(self, model, messages, tools, *args, **kwargs):
        if tools is not None:
            self.tools.append([t["function"]["name"] for t in tools])
        return await super().__call__(model, messages, tools, *args, **kwargs)


def _client(tmp_path, steps, **backends):
    cfg = make_cfg(tmp_path)
    cfg.backends.update(backends)
    script = RecordingScript(steps)
    m = Manager(cfg, chat=script)
    return TestClient(create_app(m)), m, script


def _app(client, name="finance-bot", scopes=("sessions",)):
    key = client.post("/keys", json={"name": name, "kind": "app", "scopes": list(scopes)}).json()
    return {"Authorization": f"Bearer {key['key']}"}, key


def test_local_session_sends_only_app_tools_and_denies_a_builtin_the_model_tries(tmp_path, monkeypatch):
    ran = []

    async def no_sandbox(self, name, args):  # a built-in tool must never reach the workspace
        ran.append(name)
        raise AssertionError(f"{name} ran")
    monkeypatch.setattr("harness.runner.Workspace.call", no_sandbox)
    steps = [Completion(tool_calls=[call("run_shell", 0, command="cat /etc/passwd"),
                                    call("get_balance", 1, account="checking")]),
             Completion(content="Checking has $120.")]
    client, m, script = _client(tmp_path, steps)
    projects_before = dict(m.cfg.projects)
    with client:
        auth, key = _app(client)
        s = client.post("/api/v1/sessions", headers=auth, json={
            "prompt": "How much is in checking?", "tools_only": True, "tools": [BALANCE]})
        assert s.status_code == 201, s.text
        sid = s.json()["id"]
        row = m.db.get_session(sid)
        assert row["kind"] == TOOLS_ONLY and row["project"] == "" and row["app_id"] == key["id"]
        assert "only tools are the App's own tools" in row["context"][0]["content"]

        pending = wait_for(lambda: client.get(f"/api/v1/sessions/{sid}/tool_calls", headers=auth).json())
        assert [(p["name"], p["args"]) for p in pending] == [("get_balance", {"account": "checking"})]
        assert client.post(f"/api/v1/sessions/{sid}/tool_calls/{pending[0]['call_id']}", headers=auth,
                           json={"output": "$120"}).status_code == 200
        done = wait_for(lambda: (lambda d: d if d["status"] == "done" else None)(
            client.get(f"/api/v1/sessions/{sid}", headers=auth).json()))
        assert done["answer"] == "Checking has $120."

        assert script.tools and all(names == ["get_balance"] for names in script.tools)
        decided = {e["name"]: e for e in events(m, sid, "tool_call")}
        assert decided["run_shell"]["decision"] == DENY
        assert decided["get_balance"]["decision"] == ALLOW
        assert ran == [] and m.db.pending_approvals(sid) == []
        denied = next(c for c in m.db.get_session(sid)["context"] if c.get("tool_call_id") == "c0-run_shell")
        assert "App-tools-only" in denied["content"]
        final = m.db.get_session(sid)
        assert [t["kind"] for t in final["taint"]] == ["app_tool"]  # App tool results are untrusted (#262)
        wait_for(lambda: not Path(final["workspace"]).exists())  # the throwaway working directory goes with the run
        assert m.cfg.projects == projects_before

        # A follow-up runs again (no workspace needed) and is still limited to the App's tools.
        script.steps.append(Completion(content="Still $120."))
        assert client.post(f"/api/v1/sessions/{sid}/messages", headers=auth,
                           json={"content": "And now?"}).status_code == 200
        wait_for(lambda: client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["answer"] == "Still $120.")


def test_local_session_answers_the_hosted_alias_instead_of_leaving_it_pending(tmp_path):
    """The policy allows mcp__harness__<tool>, but locally it is no tool name: the call must still get a result."""
    steps = [Completion(tool_calls=[call("mcp__harness__get_balance", 0, account="checking")]),
             Completion(content="I could not check the balance.")]
    client, m, _ = _client(tmp_path, steps)
    with client:
        auth, _ = _app(client)
        sid = client.post("/api/v1/sessions", headers=auth, json={
            "prompt": "How much is in checking?", "tools_only": True, "tools": [BALANCE]}).json()["id"]
        done = wait_for(lambda: (lambda d: d if d["status"] == "done" else None)(
            client.get(f"/api/v1/sessions/{sid}", headers=auth).json()))
        assert done["answer"] == "I could not check the balance."
        assert client.get(f"/api/v1/sessions/{sid}/tool_calls", headers=auth).json() == []
        result = next(c for c in m.db.get_session(sid)["context"]
                      if c.get("tool_call_id") == "c0-mcp__harness__get_balance")
        assert "unknown tool 'mcp__harness__get_balance'" in result["content"]


def test_owner_phone_never_gets_a_tools_only_sessions_events(tmp_path):
    """run_finished carries the title and answer: a tools-only session's must not reach the owner's ntfy."""
    from harness.notify import Notifier
    client, m, _ = _client(tmp_path, [Completion(content="Checking has $120."), Completion(content="hi")])
    with client:
        auth, _ = _app(client)
        sid = client.post("/api/v1/sessions", headers=auth, json={
            "prompt": "How much is in checking?", "tools_only": True, "tools": [BALANCE]}).json()["id"]
        wait_for(lambda: client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"] == "done")
        owner = client.post("/sessions", json={"prompt": "hello", "project": "scratch"}).json()
        wait_for(lambda: client.get(f"/sessions/{owner['id']}").json()["status"] == "done")
    m.cfg.notify.enabled = True
    notifier = Notifier(m.cfg, m.db)
    notifier.listener({"type": "run_finished", "session_id": sid, "data": {"answer": "$120"}})
    assert notifier.queue.empty()
    notifier.listener({"type": "run_finished", "session_id": owner["id"], "data": {"answer": "hi"}})
    assert notifier.queue.qsize() == 1


def test_tools_only_sessions_are_the_creating_apps_alone(tmp_path):
    client, m, _ = _client(tmp_path, [Completion(content="hi")])
    with client:
        auth, _ = _app(client)
        broad, _ = _app(client, "reader", ("sessions", "sessions:all"))
        sid = client.post("/api/v1/sessions", headers=auth, json={
            "prompt": "hello", "tools_only": True, "tools": [BALANCE]}).json()["id"]
        agent = client.post("/api/v1/sessions", headers=auth, json={"prompt": "agent work"}).json()["id"]
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
        assert {s["id"] for s in client.get("/api/v1/sessions", headers=auth).json()} == {sid, agent}
        assert client.get(f"/api/v1/sessions/{sid}", headers=auth).status_code == 200
        assert client.get(f"/api/v1/sessions/{sid}", headers=broad).status_code == 404
        assert sid not in {s["id"] for s in client.get("/api/v1/sessions", headers=broad).json()}
        assert client.get(f"/api/v1/sessions/{sid}").status_code == 404  # the owner's surface too
        assert sid not in {s["id"] for s in client.get("/api/v1/sessions").json()}
        assert sid not in {s["id"] for s in m.db.list_sessions(50, owner_id="owner")}
        with pytest.raises(HarnessError) as e:
            m.rerun(sid)
        assert e.value.status == 409


@pytest.mark.parametrize("body,status,code", [
    ({"backend": "codex"}, 400, "app_tools_only_unsupported"),
    ({"backend": "cursor"}, 400, "app_tools_only_unsupported"),
    ({"backend": "claude-nomcp"}, 400, "app_tools_only_unsupported"),
    ({"project": "scratch"}, 400, "invalid_request"),
    ({"tools": []}, 400, "invalid_request"),
])
def test_refusals(tmp_path, body, status, code):
    backends = {"codex": BackendConfig(enabled=True, model="gpt"), "cursor": BackendConfig(enabled=True, model="c"),
                "claude-nomcp": BackendConfig(enabled=True, model="opus", mcp=False)}
    client, m, _ = _client(tmp_path, [Completion(content="hi")], **backends)
    with client:
        auth, _ = _app(client)
        r = client.post("/api/v1/sessions", headers=auth,
                        json={"prompt": "hi", "tools_only": True, "tools": [BALANCE], **body})
        assert r.status_code == status, r.text
        assert r.json()["error"]["code"] == code
        assert m.db.list_sessions(50, kind=TOOLS_ONLY) == []


def test_owner_token_cannot_start_one(tmp_path):
    client, m, _ = _client(tmp_path, [Completion(content="hi")])
    with client:
        r = client.post("/api/v1/sessions", json={"prompt": "hi", "tools_only": True, "tools": [BALANCE]})
        assert r.status_code == 403


def test_device_token_cannot_start_one(tmp_path):
    """Only an App can see a tools-only session, so a device key must not create one it could never read."""
    client, m, _ = _client(tmp_path, [Completion(content="hi")])
    with client:
        key = client.post("/keys", json={"name": "phone", "kind": "device", "scopes": ["sessions"]}).json()
        headers = {"Authorization": f"Bearer {key['key']}"}
        assert client.get("/api/v1/sessions", headers=headers).status_code == 200  # a working device key
        r = client.post("/api/v1/sessions", json={"prompt": "hi", "tools_only": True, "tools": [BALANCE]},
                        headers=headers)
        assert r.status_code == 403
        assert m.db.list_sessions(50, kind=TOOLS_ONLY) == []


def test_capability_discovery_lists_supporting_backends(tmp_path):
    backends = {"claude": BackendConfig(enabled=True, model="opus"), "codex": BackendConfig(enabled=True, model="g")}
    client, m, _ = _client(tmp_path, [Completion(content="hi")], **backends)
    with client:
        auth, _ = _app(client)
        root = client.get("/api/v1").json()
        assert root["features"]["app_tools_only"] is True
        assert root["features"]["app_tools_only_backends"] == ["local", "claude"]
        listed = {b["name"]: b["app_tools_only"] for b in client.get("/api/v1/backends", headers=auth).json()}
        assert listed == {"claude": True, "codex": False}


def test_models_status_for_apps_and_warm_behind_its_scope(tmp_path, monkeypatch):
    client, m, _ = _client(tmp_path, [Completion(content="hi")])
    with client:
        plain, _ = _app(client)
        warm, _ = _app(client, "warmer", ("sessions", "models:warm"))
        assert "models:warm" in client.get("/api/v1").json()["scopes"]

        async def state(model):
            return SLEEPING
        monkeypatch.setattr(m.warmer, "state", state)
        status = client.get("/api/v1/models/status", headers=plain)
        assert status.status_code == 200 and status.json()[0]["state"] == SLEEPING

        assert client.post("/api/v1/models/warm", headers=plain).status_code == 403
        answers = iter([SLEEPING, PAUSED, LOW_MEMORY])

        async def warm_now(model, force=False):
            assert not force  # an App never overrides the RAM guard
            return next(answers)
        monkeypatch.setattr(m.warmer, "warm", warm_now)
        assert client.post("/api/v1/models/warm", headers=warm).json()["state"] == SLEEPING
        held = client.post("/api/v1/models/warm", headers=warm)
        assert held.status_code == 409 and held.json()["error"]["code"] == "gpu_held"
        low = client.post("/api/v1/models/warm", headers=warm)
        assert low.status_code == 409 and low.json()["error"]["code"] == "low_memory"


def _backend(**kw):
    return BackendConfig(enabled=True, proxy="http://proxy:8888", volume="auth", network="cli-net", **kw)


def test_claude_command_drops_every_builtin_tool(tmp_path):
    relay = McpRelay(session_id="abc", backend=_backend(), server=None)
    cli = ClaudeSession(session_id="abc", workspace=tmp_path, backend=_backend(permission_mode="bypassPermissions"),
                        sandbox=SandboxConfig(), system_prompt="only app tools", mcp=relay, mcp_token="t",
                        tools_only=True)
    command = cli.command()
    assert command[command.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in command and "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "default"  # can_use_tool always sees the call
    assert command[command.index("--system-prompt") + 1] == "only app tools"
    assert "--append-system-prompt" not in command
    agent = ClaudeSession(session_id="abc", workspace=tmp_path, backend=_backend(), sandbox=SandboxConfig(),
                          system_prompt="s").command()
    assert "--tools" not in agent and "--append-system-prompt" in agent


FAKE_TOOLS_ONLY_CLAUDE = FAKE_MCP_CLAUDE.replace('name = "mcp__harness__" + step["tool"]',
                                                 'name = step.get("raw") or "mcp__harness__" + step["tool"]')


def _hosted_manager(tmp_path, plan):
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_TOOLS_ONLY_CLAUDE, encoding="utf-8")
    state = tmp_path / "state.jsonl"
    port = _free_port()
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    m = Manager(cfg)
    made = []

    class RelayedClaude(ClaudeSession):
        async def start(self):
            await self.mcp.start()
            await super().start()

        async def stop(self):
            await super().stop()
            await self.mcp.stop()

    def factory(**kwargs):
        made.append(kwargs)
        return RelayedClaude(**kwargs, command=[sys.executable, "-u", str(fake), str(port), str(state),
                                                json.dumps(plan)])
    m.runner.cli_factory = factory
    m.runner.mcp_relay_factory = lambda **kw: McpRelay(**kw, command=[NODE, *relay_node_args(port)])
    return m, made, state


@pytest.mark.skipif(NODE is None, reason="node is needed to run the relay")
def test_hosted_claude_reaches_only_app_tools_and_builtins_are_denied(tmp_path):
    async def body():
        plan = [{"tool": "Bash", "raw": "Bash", "input": {"command": "cat ~/.claude/.credentials.json"}},
                {"tool": "web_search", "input": {"query": "sneaky"}},
                {"tool": "get_balance", "input": {"account": "savings"}}]
        m, made, state = _hosted_manager(tmp_path, plan)
        await m.start()
        app, _ = m.db.create_api_key("finance-bot", "sessions", kind="app")
        sid = m.create("savings?", backend="claude", app=app, app_tools=[AppTool(**BALANCE)], kind=TOOLS_ONLY)["id"]
        pending = []
        for _ in range(600):
            pending = m.db.app_tool_calls(sid, "pending")
            if pending:
                break
            await asyncio.sleep(0.05)
        assert [(p["name"], p["args"]) for p in pending] == [("get_balance", {"account": "savings"})]
        assert m.app_tools.submit(sid, pending[0]["call_id"], "$900", True)
        await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())

        assert made[0]["tools_only"] is True
        log = _log(state)
        assert next(entry[1] for entry in log if entry[0] == "tools") == ["get_balance"]
        calls = [entry[1:] for entry in log if entry[0] == "call"]
        assert calls[0][:2] == ["Bash", True] and "App-tools-only" in calls[0][2]
        assert calls[1][:2] == ["web_search", True]
        assert calls[2] == ["get_balance", False, "$900"]
        decided = [(e["name"], e["decision"]) for e in events(m, sid, "tool_call")]
        assert decided == [("Bash", DENY), ("mcp__harness__web_search", DENY), ("mcp__harness__get_balance", ALLOW)]
        assert m.db.pending_approvals(sid) == []
        assert not Path(m.db.get_session(sid)["workspace"]).exists()
        await m.stop()
    asyncio.run(body())


def test_hosted_cli_without_tool_limits_refuses_at_run_time(tmp_path):
    """If the App's tools can't reach the CLI over MCP at run time, the run fails; it never starts with built-ins."""
    async def body():
        cfg = make_cfg(tmp_path)
        cfg.backends["claude"] = BackendConfig(enabled=True, model="opus")
        m = Manager(cfg)
        started = []
        m.runner.cli_factory = lambda **kw: started.append(kw)
        m.runner.mcp_tool_schemas = lambda sid: []
        await m.start()
        app, _ = m.db.create_api_key("finance-bot", "sessions", kind="app")
        sid = m.create("hi", backend="claude", app=app, app_tools=[AppTool(**BALANCE)], kind=TOOLS_ONLY)["id"]
        s = await wait_status(m, sid, "failed")
        await m.stop()
        assert started == []
        assert s["run"]["failure"]["code"] == "app_tools_only_unsupported"
    asyncio.run(body())
