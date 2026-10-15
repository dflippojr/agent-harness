"""Endpoint extraction uses temporary stores and an in-process fake upstream only."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.manager import Manager
from harness_modules.endpoint import settings, service
from test_daemon import make_cfg, Script, Completion


def endpoint_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    for section in (cfg.memory_library, cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    cfg.endpoint.enabled = True
    cfg.endpoint.max_waiting = 7
    cfg.endpoint.agent_fair_seconds = 123
    m = Manager(cfg, chat=Script([Completion(content="fake")]))
    m.endpoint_transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"usage": {"prompt_tokens": 2}}))
    return m


def test_checks_and_live_queue(tmp_path):
    m = endpoint_manager(tmp_path)
    m.cfg.endpoint.max_waiting, m.cfg.endpoint.agent_fair_seconds = 8, 160
    settings.apply_endpoint_queue(m, None, None)
    assert (m.runner.gate.max_waiting, m.runner.gate.fair_seconds) == (8, 160)
    assert settings.check_endpoint(m.cfg) == []
    m.cfg.installed.local_model = False
    m.cfg.models = {}
    assert len(settings.check_endpoint(m.cfg)) == 2
    m.db.close()


def test_model_auth_validation_and_upstream_errors(tmp_path, monkeypatch):
    m = endpoint_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"x-api-key": "bad"}).json()["type"] == "error"
        row = client.post("/keys", json={"name": "fake"}).json()
        auth = {"Authorization": "Bearer " + row["key"]}
        for content in (b"[1]", b"invalid"):
            assert client.post("/v1/responses", headers=auth, content=content).status_code == 400
        monkeypatch.setattr(service, "MAX_BODY", 2)
        assert client.post("/v1/completions", headers=auth, content=b"long").status_code == 413
        monkeypatch.setattr(service, "MAX_BODY", 32 * 2**20)
        def fail(request):
            raise httpx.ConnectError("fake upstream offline")
        m.endpoint_transport = httpx.MockTransport(fail)
        assert client.post("/v1/chat/completions", headers=auth, json={"model": "fake"}).status_code == 502
        assert m.runner.gate.endpoint_active == 0
        assert m.db.conn.execute("SELECT status FROM endpoint_requests").fetchone()[0] == 502


@pytest.mark.parametrize("exception,status", [(service.GpuExclusive, 503), (service.QueueFull, 429)])
def test_gate_rejections_are_accounted(tmp_path, exception, status):
    m = endpoint_manager(tmp_path)
    async def reject():
        raise exception()
    m.runner.gate.endpoint_request = reject
    with TestClient(create_app(m)) as client:
        key = client.post("/keys", json={"name": "fake"}).json()["key"]
        response = client.post("/v1/messages", headers={"x-api-key": key}, json={})
        assert response.status_code == status
        assert response.json()["type"] == "error"
        assert m.db.conn.execute("SELECT status FROM endpoint_requests").fetchone()[0] == status


@pytest.mark.parametrize("held", [True, False])
def test_parked_model_rejections(tmp_path, held):
    m = endpoint_manager(tmp_path)
    m.warmer.parked = lambda model: True
    m.warmer.blocked = lambda: True
    m.guard = SimpleNamespace(active=held, manual=False)
    recorded = []
    result = asyncio.run(service._ensure_model(m, m.cfg.models["fake"], "openai", recorded.append))
    assert result.status_code == 503
    assert result.headers["retry-after"] == ("180" if held else "60")
    assert recorded == [503]
    m.db.close()


def test_accounting_failure_preserves_response(tmp_path, monkeypatch):
    m = endpoint_manager(tmp_path)
    def fail(record):
        raise RuntimeError("fake database failure")
    monkeypatch.setattr(m.db, "log_endpoint_request", fail)
    with TestClient(create_app(m)) as client:
        key = client.post("/keys", json={"name": "fake"}).json()["key"]
        assert client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={}).status_code == 200
