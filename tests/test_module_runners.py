"""Runners add-on isolation and compatibility, using only temporary tokens and fake transports."""
import asyncio
import io
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from harness import cli, config, modules
from harness.admin import PREFIX
from harness.api import create_app
from harness.fileops import ToolError
from harness.manager import HarnessError, Manager
from harness.runner_contract import NoRunnerHub, RemoteWorkspace as WorkspaceContract, RunnerOffline
from harness.settings_keys import build_registry
from harness_modules.runners import mac_client, service
from harness_modules.runners.runtime import RunnersRuntime
from test_daemon import make_cfg


def runner_manager(tmp_path, *, packages=None, installed=True):
    cfg = make_cfg(tmp_path)
    cfg.module_packages = packages
    cfg.installed.runners = installed
    token = tmp_path / "runner.token"
    if not token.exists():
        token.write_text("already-paired", encoding="utf-8")
    cfg.runners = {"mac": config.RunnerConfig(name="mac", token_file=str(token), min_free_gb=0)}
    return Manager(cfg)


def test_runners_register_routes_commands_and_capabilities(tmp_path):
    m = runner_manager(tmp_path)
    rt = m.modules.get("runners")
    assert m.hub is m.runner.hub is rt.service
    assert m.runners is m.hub
    assert m.cfg.capabilities()["modules"]["runners"] is True
    assert "modules.runners" in build_registry(m.cfg).specs
    assert {row[0] for row in cli.admin_commands()} >= {
        "runner list", "runner update", "runner-pairing-codes list", "runner-pairing-codes create"}
    assert cli.admin_request(cli._build_parser().parse_args(["runner", "update", "mac"]))[:2] == (
        "POST", "/runners/mac/update")
    with TestClient(create_app(m)) as client:
        assert client.get("/me").json()["capabilities"]["runners"] is True
        assert client.get("/api/v1").json()["features"]["runner_pairing"] is True
        assert client.get(PREFIX + "/runners").json()[0]["name"] == "mac"
        operations = {op["path"] for op in client.get(PREFIX).json()["operations"]}
        assert PREFIX + "/runners/{name}/poll" not in operations
        assert PREFIX + "/runners/{name}/results" not in operations
    assert m.hub._closing


@pytest.mark.parametrize("packages,installed", [(["harness_modules.images"], True), (None, False), ([], True)])
def test_absent_runners_leave_local_core_and_saved_pairing_intact(tmp_path, packages, installed):
    m = runner_manager(tmp_path, packages=packages, installed=installed)
    assert m.runners is None and "runners" not in m.modules
    assert isinstance(m.hub, NoRunnerHub)
    assert m.hub is m.runner.hub
    assert "runners" not in m.cfg.capabilities()["modules"]
    assert "modules.runners" not in build_registry(m.cfg).specs
    with TestClient(create_app(m)) as client:
        assert client.get("/health").json()["ok"]
        assert "runner_pairing" not in client.get("/api/v1").json()["features"]
        assert "runners" not in client.get("/me").json()["capabilities"]
        for path in ("/runners", PREFIX + "/runners", "/runner-pairing-codes",
                     "/mac-client/install.sh", "/mac-client/package.tar.gz", "/mac-client/manifest.json"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/v1/runner-pair", json={"code": "hrp-example"}).status_code in (404, 405)
        assert client.get("/maintenance").json()["runners"] == []
    with pytest.raises(HarnessError, match="runners module is unavailable"):
        m._check_free_space(True, False, "mac", "owner")
    assert (tmp_path / "runner.token").read_text() == "already-paired"


def test_existing_token_and_protocol_work_after_manager_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "POLL_HOLD_SECONDS", 0)
    for _ in range(2):
        m = runner_manager(tmp_path)
        with TestClient(create_app(m)) as client:
            response = client.post("/runners/mac/poll", headers={"Authorization": "Bearer already-paired"},
                                   json={"instance": "existing-client", "info": {"protocol": 2}})
            assert response.status_code == 200
            assert response.json() == {"requests": [], "keep_awake": False}
        assert (tmp_path / "runner.token").read_text() == "already-paired"


def test_runner_result_auth_limits_and_update_errors(tmp_path, monkeypatch):
    m = runner_manager(tmp_path)
    m.hub.call = AsyncMock(return_value={"updated": True})
    with TestClient(create_app(m)) as client:
        body = {"id": "no-request", "ok": True}
        assert client.post("/runners/mac/results", json=body).status_code == 401
        headers = {"Authorization": "Bearer already-paired"}
        assert client.post("/runners/missing/results", json=body, headers=headers).status_code == 404
        assert client.post("/runners/mac/results", json=body, headers=headers).json() == {"accepted": False}
        assert client.post("/runners/mac/results", json=body, headers={
            **headers, "Content-Length": str(16 * 2**20 + 1)}).status_code == 413
        assert client.post(PREFIX + "/runners/missing/update").status_code == 404
        assert client.post(PREFIX + "/runners/mac/update").status_code == 409
        st = m.hub.state["mac"]
        st.last_seen = time.monotonic()
        for protocol in (None, "invalid", 1, 999):
            st.info = {"protocol": protocol}
            assert client.post(PREFIX + "/runners/mac/update").status_code == 409
        st.info = {"protocol": 2}
        assert client.post(PREFIX + "/runners/mac/update").json() == {"updated": True}
        m.hub.call.assert_awaited_once_with("mac", "update_client", {}, timeout=300, wait_if_offline=False)
        m.hub.call.side_effect = RuntimeError("failed download")
        failed = client.post(PREFIX + "/runners/mac/update")
        assert failed.status_code == 409 and "failed download" in failed.json()["detail"]


def test_pairing_validation_and_token_file_failures(tmp_path, monkeypatch):
    m = runner_manager(tmp_path)
    rt = m.modules.get("runners")
    assert rt._runner_token("mac") == "already-paired"
    with pytest.raises(HarnessError, match="unknown runner"):
        rt._runner_token("missing")
    rc = m.cfg.runners["mac"]
    rc.token_file = ""
    with pytest.raises(HarnessError, match="no token_file"):
        rt._runner_token("mac")
    rc.token_file = str(tmp_path / "empty")
    Path(rc.token_file).write_text("")
    with pytest.raises(HarnessError, match="empty"):
        rt._runner_token("mac")
    monkeypatch.setattr("harness_modules.runners.runtime.os.chmod", lambda *_: (_ for _ in ()).throw(OSError()))
    created = rt._runner_token("mac", create=True)
    assert created and rt._runner_token("mac") == created
    rc.token_file = str(tmp_path / "directory")
    Path(rc.token_file).mkdir()
    with pytest.raises(HarnessError, match="unavailable"):
        rt._runner_token("mac", create=True)
    rc.token_file = str(tmp_path / "runner.token")
    with TestClient(create_app(m)) as client:
        for body in ({"name": " "}, {"runner": " "}):
            assert client.post("/runner-pairing-codes", json=body).status_code == 400
        assert client.delete("/runner-pairing-codes/missing").status_code == 404
        pair = client.post("/runner-pairing-codes", json={"runner": "mac"}).json()
        m.cfg.runners.pop("mac")
        response = client.post("/api/v1/runner-pair", json={"code": pair["code"]})
        assert response.status_code == 400 and "no longer configured" in response.json()["detail"]


def test_bundle_contains_runner_management_rows_without_loading_addons_on_mac():
    with tarfile.open(fileobj=io.BytesIO(mac_client.package_bytes()), mode="r:gz") as archive:
        rows = archive.extractfile("client/harness_module_commands.json").read()
        assert b"runner list" in rows and b"runner-pairing-codes create" in rows
        assert "app/harness_runner.py" in archive.getnames()
    assert mac_client.package_manifest()["runner_protocol"] == 3


def test_remote_workspace_and_sandbox_delegate_through_the_hub():
    async def scenario():
        hub = SimpleNamespace(call=AsyncMock(return_value="file result"), fire=lambda *args: fired.append(args))
        fired = []
        ws = service.RemoteWorkspace(hub, "macbook", "sid", 4096,
                                     verify_checks=[config.VerifyCheck(name="check", command="echo ok")])
        assert isinstance(ws, WorkspaceContract)
        assert ws.schemas()
        assert await ws.call("read_file", {"path": "a"}) == "file result"
        assert hub.call.call_args.args[1] == "file"
        assert await ws.call("git_clone", {"url": "example"}) == "file result"
        assert await ws.put_file("image.png", b"image") == "file result"
        assert await ws.preview("read_file", {}) == "file result"
        hub.call.return_value = "42"
        assert await ws.size_bytes() == 42
        hub.call.return_value = {"code": 0, "output": "ok"}
        assert "ok" in await ws.call("run_shell", {"command": "echo ok", "network": True})
        assert await ws.call("verify", {}) is not None
        with pytest.raises(ToolError, match="isn't available"):
            await ws.call("unknown", {})
        with pytest.raises(ToolError, match="limit"):
            await ws.put_file("large", b"x" * (service.MAX_PUT_BYTES + 1))
        for kind in ("tool", "timeout", "restarted"):
            hub.call.side_effect = service.RunnerError("failed", kind=kind)
            with pytest.raises(ToolError, match="failed"):
                await ws.call("read_file", {})
            assert await ws.preview("read_file", {}) == ""
        hub.call.side_effect = service.RunnerError("git failed", kind="git")
        with pytest.raises(service.RunnerError, match="git failed"):
            await ws.call("git_clone", {})
        sandbox = service.RemoteSandbox(hub, "mac", "sid")
        await sandbox.stop()
        await sandbox.restart()
        await sandbox.remove()
        assert fired == [("mac", "kill_session", {"session": "sid"})]
    asyncio.run(scenario())


def test_local_only_hub_never_queues_remote_work():
    async def scenario():
        hub = NoRunnerHub()
        assert hub.status() == [] and not hub.online("mac") and hub.startup_grace() == 0
        hub.close()
        for op in (hub.wait_online("mac"), hub.call("mac", "shell", {})):
            with pytest.raises(RunnerOffline, match="unavailable"):
                await op
        with pytest.raises(RunnerOffline):
            hub.sandbox("mac", "sid")
        with pytest.raises(RunnerOffline):
            hub.workspace("mac", "sid", 4096)
    asyncio.run(scenario())


def test_uninstalled_runtime_does_not_construct_a_hub(tmp_path):
    m = runner_manager(tmp_path, installed=False)
    module = modules.claims(m.cfg, "runners")
    rt = RunnersRuntime(m, module)
    rt.init()
    assert rt.service is None and rt.features() == {"runner_pairing": False}
    asyncio.run(rt.stop())
