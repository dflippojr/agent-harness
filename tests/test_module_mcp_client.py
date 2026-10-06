"""Pinned client acceptance through local fake stdio servers only."""

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

from harness import config, modules, taint
from harness.fileops import ToolError
from harness.manager import Manager
from harness.policy import Policy
from harness.settings_keys import build_registry
from harness_modules.mcp_client import MODULE
from harness_modules.mcp_client import service
from test_daemon import Script, call, events, make_cfg, wait_status
from harness.llm import Completion

SERVER = {"name": "demo", "image": "local/fake@sha256:" + "a" * 64, "command": ["fake", "--stdio"]}
NAME = "mcp__demo__echo"
SESSION = {"id": "fake-session", "project": "scratch", "workspace": "unused", "owner_id": "owner",
           "backend": "local", "kind": "agent", "target": "tower"}
FAKE = Path(__file__).with_name("fake_mcp_stdio.py")


@pytest.fixture
def fake_transport(monkeypatch):
    clients, commands = [], []
    real = service.StdioClient

    def spawn(argv, secrets=None):
        mode = argv[-1] if argv[-1] != "--stdio" else "normal"
        client = real([sys.executable, "-u", str(FAKE), mode], secrets)
        clients.append(client)
        return client

    async def cmd(argv, **kwargs):
        commands.append(argv)
        return 0, "", ""

    monkeypatch.setattr(service, "StdioClient", spawn)
    monkeypatch.setattr(service, "run_cmd", cmd)
    yield clients, commands
    for client in clients:
        if client.proc.poll() is None:
            client.close()


@pytest.mark.parametrize("change", [
    {"image": "repo:latest"}, {"url": "https://example.com"}, {"name": "harness"}, {"name": "A"},
    {"command": "shell"}, {"command": []}, {"command": [1]}, {"command": ["\0"]},
    {"env": {"TOKEN": "plaintext"}}, {"env": {"TOKEN": {"secret_file": ""}}},
    {"mount_workspace": True}, {"rules": [{"tool": "write_file", "action": "allow"}]}, {"unknown": True},
])
def test_invalid_project_config(change):
    with pytest.raises(ValueError):
        config._project_from_spec("demo", {"mcp_servers": [SERVER | change]})


def test_config_owner_only_and_duplicates():
    for servers in ([SERVER, SERVER], ["bad"], "bad"):
        with pytest.raises(ValueError):
            config._project_from_spec("demo", {"mcp_servers": servers})
    with pytest.raises(ValueError, match="member"):
        config._project_from_spec("demo", {"mcp_servers": [SERVER]}, owner_id="member")
    assert config._project_from_spec("demo", {"mcp_servers": [SERVER]}).mcp_servers == [SERVER]
    assert config._project_from_spec("demo", {}).mcp_servers == []
    for names in (("a", "a__b"), ("a__b", "a"), ("a", "a_")):
        with pytest.raises(ValueError, match="overlap"):
            config._project_from_spec("demo", {"mcp_servers": [SERVER | {"name": name} for name in names]})


@pytest.mark.parametrize("mount", [False, "ro", "rw"])
def test_container_hardening_and_mount(tmp_path, mount):
    argv = service.container_argv(SESSION | {"workspace": str(tmp_path)}, SERVER | {"mount_workspace": mount},
                                  config.SandboxConfig())
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv and "--pull=never" in argv
    assert all(arg in argv for arg in ("--memory", "--cpus", "--pids-limit", "--init"))
    assert argv[-3:] == [SERVER["image"], "fake", "--stdio"]
    if mount:
        binding = argv[argv.index("--mount") + 1]
        assert "target=/workspace" in binding
        assert binding.endswith(",readonly") == (mount == "ro")
    else:
        assert "--mount" not in argv and "--workdir" not in argv


def test_policy_is_scoped_default_ask_and_rules_apply():
    assert Policy(mcp_servers=["demo"]).decide(NAME, {}).action == "ask"
    for action in ("allow", "ask", "deny"):
        policy = Policy([{"tool": NAME, "action": action}], mcp_servers=["demo"])
        assert policy.decide(NAME, {}).action == action
        assert policy.decide("mcp__other__echo", {}).action == "deny"
    assert Policy([{"tool": "*", "action": "allow"}]).decide(NAME, {}).action == "deny"
    assert Policy(mcp_servers=["demo"]).decide("mcp__demo__", {}).action == "deny"
    assert Policy().decide("mcp__harness__read_file", {}).action == "allow"
    assert Policy().fingerprint() != Policy(mcp_servers=["demo"]).fingerprint()
    assert taint.source_for(NAME, {}) == ("mcp", NAME)


def manager(tmp_path, enabled=True, packages=None, steps=None):
    cfg = make_cfg(tmp_path)
    cfg.module_packages = packages
    cfg.installed.mcp_client = enabled
    cfg.modules.mcp_client = enabled
    cfg.memory_library.enabled = False
    cfg.remote_control.enabled = False
    cfg.gpu_guard.enabled = False
    cfg.notify.enabled = False
    cfg.modules.runners = False
    cfg.installed.runners = False
    cfg.projects["scratch"].mcp_servers = [copy.deepcopy(SERVER)]
    return Manager(cfg, chat=Script(steps or [Completion(content="done")]))


def test_module_absent_off_and_native_only(tmp_path, fake_transport):
    async def body():
        for enabled, packages in ((False, None), (True, [])):
            m = manager(tmp_path / str(enabled), enabled, packages)
            assert "modules.mcp_client" not in build_registry(m.cfg).specs
            assert m.runner.policy(SESSION).decide(NAME, {}).action == "deny"
            assert all(NAME not in kit.tool_names for kit in m.runner.daemon_toolkits(SESSION))
            await m.modules.prepare_session(SESSION)
            assert not fake_transport[0]
        m = manager(tmp_path / "on")
        rt = m.modules.get("mcp_client")
        for changes in ({"backend": "claude"}, {"owner_id": "member"}, {"kind": "chat"},
                        {"kind": "tools_only"}, {"target": "mac"}, {"project": "missing"}):
            s = SESSION | changes
            await rt.prepare_session(s)
            assert rt.session_toolkit(s) is None
        m.cfg.projects["scratch"].owner_id = "member"
        await rt.prepare_session(SESSION)
        assert not fake_transport[0]
    asyncio.run(body())


@pytest.mark.parametrize("mode", ["normal", "pagination", "notification", "noisy"])
def test_listing_call_and_cleanup(tmp_path, fake_transport, mode):
    async def body():
        m = manager(tmp_path)
        m.cfg.projects["scratch"].mcp_servers[0]["command"] = [mode]
        await m.modules.prepare_session(SESSION)
        await m.modules.prepare_session(SESSION)
        kits = m.runner.daemon_toolkits(SESSION)
        kit = next(kit for kit in kits if NAME in kit.tool_names)
        assert m.modules.gate_for(kit) is MODULE.tools and not MODULE.tools.mcp
        assert kit.schemas()[0]["function"]["name"] == NAME
        assert json.loads(await kit.call(NAME, {"text": "ok"}))["content"][0]["text"] == "ok"
        # A healthy server may send many notifications while the native loop is waiting for another model turn.
        await asyncio.sleep(0.05)
        assert json.loads(await kit.call(NAME, {"text": "again"}))["content"][0]["text"] == "again"
        with pytest.raises(ToolError):
            await kit.call("unknown", {})
        assert len(fake_transport[0]) == 1
        await m.modules.stop()
        assert not m.modules.get("mcp_client").sessions
        assert fake_transport[0][0].proc.poll() is not None
        assert fake_transport[1][-1][1:3] == ["rm", "-f"]
    asyncio.run(body())


@pytest.mark.parametrize("mode", ["version", "invalid_tool", "schema", "bad_schema", "duplicate", "empty_pages", "invalid_list",
                                 "protocol_error", "id", "result", "malformed", "oversize", "nonobject"])
def test_failed_listing_cleans_sidecar(tmp_path, fake_transport, mode):
    async def body():
        m = manager(tmp_path)
        m.cfg.projects["scratch"].mcp_servers[0]["command"] = [mode]
        with pytest.raises(ToolError):
            await m.modules.prepare_session(SESSION)
        assert not m.modules.get("mcp_client").sessions
        assert all(client.proc.poll() is not None for client in fake_transport[0])
    asyncio.run(body())


def test_tool_error_and_timeout(fake_transport, monkeypatch):
    async def body():
        kit = service.SessionTools(SESSION, [SERVER | {"command": ["tool_error"]}], config.SandboxConfig())
        await kit.start()
        with pytest.raises(ToolError, match="tool returned"):
            await kit.call(NAME, {"text": "bad"})
        await kit.close()
        monkeypatch.setattr(service, "RPC_TIMEOUT", 0.1)
        client = fake_transport[0][-1]
        with pytest.raises((ToolError, OSError, ValueError)):
            await asyncio.to_thread(client.request, "closed", {})
        real = service.StdioClient(["unused", "timeout"])
        with pytest.raises(ToolError):
            await asyncio.to_thread(real.request, "wait", {})
        real.close()
    asyncio.run(body())


def test_secrets_are_references_never_argv(tmp_path, fake_transport, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("private-token", encoding="utf-8")
    server = SERVER | {"env": {"TOKEN": {"secret_file": str(secret)}}, "command": ["env"]}
    monkeypatch.setenv("DAEMON_SECRET", "never-inherit")
    config._project_from_spec("demo", {"mcp_servers": [server]})
    argv = service.container_argv(SESSION, server, config.SandboxConfig())
    assert argv[argv.index("--env") + 1] == "TOKEN"
    assert "private-token" not in str(argv)
    with pytest.raises(ToolError):
        service.container_argv(SESSION | {"workspace": "a,b"}, SERVER | {"mount_workspace": "rw"}, config.SandboxConfig())
    async def body():
        kit = service.SessionTools(SESSION, [server], config.SandboxConfig())
        await kit.start()
        result = json.loads(await kit.call(NAME, {"text": "env"}))
        assert result["content"][0]["text"] == "private-token:"
        await kit.close()
    asyncio.run(body())


@pytest.mark.parametrize("approve", [True, False])
def test_native_loop_approval_events_taint_metrics_and_cleanup(tmp_path, fake_transport, approve):
    async def body():
        m = manager(tmp_path, packages=["harness_modules.mcp_client"], steps=[
            Completion(tool_calls=[call(NAME, text="untrusted result")]), Completion(content="done")])
        await m.start()
        try:
            s = m.create("echo")
            sid = s["id"]
            # wait_status waits for the run task for terminal states, so use the pending approval as the checkpoint.
            for _ in range(500):
                if m.db.pending_approvals(sid):
                    break
                await asyncio.sleep(0.01)
            assert m.db.pending_approvals(sid), m.db.get_session(sid)
            assert not events(m, sid, "tool_result")
            m.decide(sid, None, approve=approve)
            final = await wait_status(m, sid, "done", "failed")
            assert final["status"] == "done", final["stop_reason"]
            assert events(m, sid, "tool_call")[0]["name"] == NAME
            assert events(m, sid, "tool_result")[0]["ok"] == approve
            assert bool(final["taint"]) == approve
            if approve:
                assert final["taint"][0]["origin"] == NAME
                assert "untrusted result" in events(m, sid, "tool_result")[0]["output"]
                from harness.efficiency import compose
                assert compose(final["context"], 0, 3, None)["buckets"]["tool_outputs"] > 0
            assert events(m, sid, "turn_metrics")
            assert not m.modules.get("mcp_client").sessions
            assert all(client.proc.poll() is not None for client in fake_transport[0])
        finally:
            await m.stop()
    asyncio.run(body())


def test_session_isolation_rule_overrides_and_cancellation(tmp_path, fake_transport):
    async def body():
        m = manager(tmp_path)
        m.cfg.projects["scratch"].mcp_servers[0]["rules"] = [{"tool": NAME, "action": "allow"}]
        assert m.runner.policy(SESSION).decide(NAME, {}).action == "allow"
        await m.modules.prepare_session(SESSION)
        other = SESSION | {"id": "second-session"}
        await m.modules.prepare_session(other)
        rt = m.modules.get("mcp_client")
        assert rt.session_toolkit(SESSION) is not rt.session_toolkit(other)
        assert service.container_name(SESSION["id"], SERVER) != service.container_name(other["id"], SERVER)
        await m.modules.end_session(SESSION["id"])
        assert rt.session_toolkit(other) is not None
        await m.modules.stop()

        m = manager(tmp_path / "cancel", packages=["harness_modules.mcp_client"], steps=[
            Completion(tool_calls=[call(NAME, text="never run")])])
        await m.start()
        try:
            s = m.create("echo")
            for _ in range(500):
                if m.db.pending_approvals(s["id"]):
                    break
                await asyncio.sleep(0.01)
            assert m.db.pending_approvals(s["id"])
            m.tasks[s["id"]].cancel()
            await asyncio.gather(m.tasks[s["id"]], return_exceptions=True)
            assert not m.modules.get("mcp_client").sessions
            assert not m.db.get_session(s["id"])["taint"]
            assert all(client.proc.poll() is not None for client in fake_transport[0])
        finally:
            await m.stop()
    asyncio.run(body())


def test_request_limit(fake_transport, monkeypatch):
    client = service.StdioClient(["unused", "normal"])
    monkeypatch.setattr(service, "MAX_MESSAGE", 10)
    with pytest.raises(ToolError, match="request exceeded"):
        client.request("too big", {"text": "x" * 50})
    client.close()
    monkeypatch.setattr(service, "MAX_MESSAGE", 1_000_000)
    with pytest.raises(ToolError, match="stdin is closed"):
        client.request("x", {})


def test_apps_cannot_claim_mcp_namespaces():
    from harness.apps import AppTool, validate_tools
    for name in (NAME, "mcp__harness__echo", "mcp__unconfigured__echo"):
        with pytest.raises(ValueError, match="already taken"):
            validate_tools([AppTool(name=name, description="spoof")], set())


def test_client_prefix_can_overlap_the_harness_alias(tmp_path, fake_transport):
    async def body():
        m = manager(tmp_path)
        m.cfg.projects["scratch"].mcp_servers[0]["name"] = "harness__docs"
        await m.start()
        try:
            s = m.create("done")
            final = await wait_status(m, s["id"], "done")
            name = "mcp__harness__docs__echo"
            assert m.runner.policy(final).decide(name, {}).action == "ask"
            m.runner._taint_from_result(final, name, {})
            assert m.db.get_session(s["id"])["taint"][0]["origin"] == name
            assert taint.source_for("mcp__harness__read_file", {}) is None
        finally:
            await m.stop()
    asyncio.run(body())


def test_timeout_does_not_include_waiting_for_rpc_lock(fake_transport, monkeypatch):
    async def body():
        kit = service.SessionTools(SESSION, [SERVER], config.SandboxConfig())
        await kit.start()
        client = fake_transport[0][0]
        monkeypatch.setattr(service, "RPC_TIMEOUT", 0.5)
        client.lock.acquire()
        pending = asyncio.create_task(kit.call(NAME, {"text": "queued"}))
        try:
            await asyncio.sleep(1)
            assert client.proc.poll() is None
        finally:
            client.lock.release()
        assert json.loads(await pending)["content"][0]["text"] == "queued"
        await kit.close()
    asyncio.run(body())


def test_cleanup_failure_still_closes_every_sidecar(fake_transport, monkeypatch):
    async def body():
        kit = service.SessionTools(SESSION, [SERVER, SERVER | {"name": "second"}], config.SandboxConfig())
        await kit.start()
        attempted = []

        async def fail_first(argv, **kwargs):
            attempted.append(argv[-1])
            if len(attempted) == 1:
                raise OSError("docker failed")
            return 0, "", ""

        monkeypatch.setattr(service, "run_cmd", fail_first)
        with pytest.raises(ToolError, match="cleanup failed"):
            await kit.close()
        assert len(attempted) == 2
        assert not kit.clients and not kit.tools
        assert all(client.proc.poll() is not None for client in fake_transport[0])
    asyncio.run(body())
