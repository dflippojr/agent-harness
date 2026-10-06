"""Homelab extraction tests: temporary stacks and fake Docker/Prometheus only."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from harness.config import Project
from harness.manager import Manager
from harness.llm import Completion
from harness_modules.homelab import service
from test_daemon import make_cfg, Script
from test_phase3 import lab


@pytest.mark.parametrize("selection", ["present", "off", "absent"])
def test_presence_and_workspace_gates(tmp_path, selection):
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    for section in (cfg.memory_library, cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    cfg.installed.homelab = selection != "off"
    if selection == "absent":
        cfg.module_packages = ["harness_modules.images"]
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    project = Project(name="lab", homelab=True)
    rt = m.modules.get("homelab")
    if selection != "present":
        assert rt is None
        assert "Homelab access" not in m._optional_prompts(project, {}, None)
        m.db.close()
        return
    assert cfg.capabilities()["modules"]["homelab"]
    assert rt.workspace_toolkit(project, {}, False) is rt.service
    assert rt.workspace_toolkit(project, {}, True) is None
    assert rt.workspace_toolkit(None, {}, False) is None
    assert rt.workspace_toolkit(Project(name="plain"), {}, False) is None
    assert rt.workspace_toolkit(project, {"app.capabilities": []}, False) is None
    assert rt.project_prompt(project, {"app.capabilities": []}) == ""
    assert "no repository" in rt.project_prompt(project, {})
    project.repo = "https://example.test/repo"
    assert "no repository" not in rt.project_prompt(project, {})
    assert set(rt.service.tool_names) == {t["function"]["name"] for t in rt.service.schemas()}
    m.db.close()


def test_config_read_errors_and_dispatch(tmp_path):
    h = lab(tmp_path)
    root = __import__("pathlib").Path(h.cfg.docker_root)
    (root / "web/large").write_bytes(b"x" * 200001)
    (root / "web/empty").mkdir()
    assert h.read_service_config("web/empty") == "(empty directory)"
    for path, message in [("web/missing", "no such file"), ("web/large", "too large")]:
        with pytest.raises(service.HomelabError, match=message):
            h.read_service_config(path)
    assert asyncio.run(h.call("read_service_config", {"path": "web/config/app.yaml"})) == "a: 1\n"


def test_fake_docker_success_and_failures(tmp_path, monkeypatch):
    h = lab(tmp_path)
    responses = []

    async def fake(args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(service, "run_cmd", fake)
    async def run():
        responses.extend([(0, "[]", "")])
        assert "container missing" in await h.homelab_services()
        responses.extend([(1, "invalid", "inspect error")])
        with pytest.raises(service.HomelabError, match="inspect failed"):
            await h.homelab_services()
        responses.extend([(0, "", "")])
        assert await h.call("container_logs", {"service": "web"}) == "(no log lines)"
        responses.extend([(1, "", "logs error")])
        with pytest.raises(service.HomelabError, match="logs failed"):
            await h.container_logs("web")
        responses.extend([(0, "running", ""), (0, "ok", ""), (0, "running since now", "")])
        assert "restarted" in await h.restart_service("web")
        responses.extend([(0, "running", ""), (1, "", "bad")])
        with pytest.raises(service.HomelabError, match="restart failed"):
            await h.restart_service("web")
        responses.extend([(0, "built", ""), (0, "running", "")])
        assert "rebuilt and recreated" in await h.rebuild_service("web")
        responses.extend([(1, "build failed", "")])
        with pytest.raises(service.HomelabError, match="rebuild failed"):
            await h.rebuild_service("web")
        h.cfg.services.clear()
        assert await h.homelab_services() == "No services are allowlisted."
    asyncio.run(run())


def test_fake_prometheus(tmp_path, monkeypatch):
    h = lab(tmp_path)
    responses = []
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, params):
            calls.append((url, params))
            return responses.pop(0)

    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    async def run():
        responses.append(httpx.Response(200, json={"status": "success", "data": {
            "resultType": "scalar", "result": [0, "1"]}}))
        assert await h.prometheus_query("up") == "scalar: 1"
        assert calls[-1][0].endswith("/query")
        responses.append(httpx.Response(200, json={"status": "success", "data": {
            "resultType": "matrix", "result": []}}))
        assert await h.prometheus_query("up", range_minutes=20000, step_seconds=30) == "no data"
        assert calls[-1][1]["step"] == 30
        responses.append(httpx.Response(200, json={"status": "error", "error": "bad query"}))
        with pytest.raises(service.HomelabError, match="bad query"):
            await h.prometheus_query("bad", range_minutes=1)
        responses.append(httpx.Response(502, text="gateway down"))
        with pytest.raises(service.HomelabError, match="HTTP 502"):
            await h.prometheus_query("up")
    asyncio.run(run())


def test_prometheus_formats_and_container_health():
    assert service.format_prometheus({"resultType": "string", "result": [0, "ok"]}) == "string: ok"
    series = {"metric": {}, "values": [[i, "NaN"] for i in range(30)]}
    assert "no numbers" in service.format_prometheus({"resultType": "matrix", "result": [series, series]}, limit=1)
    assert "1 more series" in service.format_prometheus({"resultType": "matrix", "result": [series, series]}, limit=1)
    parts = service._container_parts(SimpleNamespace(stack="web"), {"State": {
        "Status": "running", "Health": {"Status": "healthy"}, "Error": "oops"}})
    assert "health healthy" in parts and "error: oops" in parts
