"""Endpoint extraction uses temporary stores and an in-process fake upstream only."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.manager import Manager
from harness_modules.endpoint import settings, service
from harness_modules.endpoint.store import EndpointRequestStore
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
    attempts = []

    def fail(store, record):
        attempts.append(record["status"])
        raise RuntimeError("fake database failure")
    monkeypatch.setattr(EndpointRequestStore, "log_endpoint_request", fail)
    with TestClient(create_app(m)) as client:
        key = client.post("/keys", json={"name": "fake"}).json()["key"]
        assert client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={}).status_code == 200
    assert attempts == [200]


DEEP = 100_000  # far past the interpreter's recursion limit, still well under MAX_BODY
HOSTILE_BODIES = {  # #930: each is a body json.loads rejects or that isn't an object; none may become a 5xx
    "nested arrays": b"[" * DEEP + b"]" * DEEP,
    "nested objects": b'{"a":' * DEEP + b"1" + b"}" * DEEP,
    "nesting inside an object": b'{"messages":' + b"[" * DEEP + b"]" * DEEP + b"}",
    "unterminated nesting": b"[" * DEEP,
    "not JSON": b"invalid",
    "invalid UTF-8": b'{"model": "\xff\xfe"}',
    "huge integer": b'{"max_tokens": ' + b"9" * 5000 + b"}",
    "array": b"[1]",
    "string": b'"text"',
    "null": b"null",
}


@pytest.mark.parametrize("path", sorted(service.ROUTES))
def test_hostile_bodies_get_the_flavor_400(tmp_path, path):
    """Class-level check (#930): every inference route, OpenAI and Anthropic alike, answers each hostile body with its
    own flavor's 400, never a 500. It walks service.ROUTES, so a new route is covered when it's added."""
    m = endpoint_manager(tmp_path)
    flavor = service.ROUTES[path]
    with TestClient(create_app(m), raise_server_exceptions=False) as client:
        key = client.post("/keys", json={"name": "fake"}).json()["key"]
        auth = {"x-api-key": key} if flavor == "anthropic" else {"Authorization": "Bearer " + key}
        wrong = {}
        for name, content in HOSTILE_BODIES.items():
            response = client.post(path, headers=auth, content=content)
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            shaped = (body.get("type") == "error" and body["error"]["type"] == "invalid_request_error"
                      if flavor == "anthropic" else
                      body.get("error", {}).get("type") == "invalid_request_error" and body["error"]["code"] == 400)
            if response.status_code != 400 or not shaped:
                wrong[name] = (response.status_code, response.text[:120])
        assert wrong == {}
        assert m.db.conn.execute("SELECT COUNT(*) FROM endpoint_requests").fetchone()[0] == 0
    m.db.close()


def _log(m, key_id: str, status: int = 200) -> None:
    EndpointRequestStore(m.db).log_endpoint_request(
        {"key_id": key_id, "route": "/v1/chat/completions", "model": "fake", "stream": False, "status": status})


@pytest.mark.parametrize("endpoint_present", [True, False])
def test_every_key_listing_counts_its_requests_from_the_endpoint_module(tmp_path, endpoint_present):
    """#992: the core's key listing names no module table; `requests` comes from the endpoint module through the host,
    on the list, the one-key read and the undone revoke alike, and is 0 (still present) while the module is absent."""
    from test_app_stores import _key
    m = endpoint_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        a_id, _ = _key(client, "app-a", "sessions")
        b_id, _ = _key(client, "app-b", "sessions")
        for _ in range(3):
            _log(m, a_id)
        _log(m, b_id, status=502)
        if not endpoint_present:
            del m.modules.runtimes["endpoint"]
        want_a, want_b = (3, 1) if endpoint_present else (0, 0)

        listed = {k["id"]: k for k in client.get("/keys").json()}
        assert (listed[a_id]["requests"], listed[b_id]["requests"]) == (want_a, want_b)
        assert all(k["requests"] == 0 for kid, k in listed.items() if kid not in (a_id, b_id))
        one = client.put(f"/api/admin/v1/apps/{a_id}/retention", json={"retention_days": 30})
        assert one.status_code == 200 and one.json()["requests"] == want_a
        assert m.db.get_api_key_listing(b_id)["requests"] == want_b

        assert client.delete(f"/keys/{a_id}").status_code == 204
        restored = client.post(f"/api/admin/v1/apps/{a_id}/restore")
        assert restored.status_code == 200 and restored.json()["requests"] == want_a
        assert "requests" not in m.db.main.get_api_key_listing(a_id)  # the core's own row counts nothing
    m.db.close()


def test_request_counts_reads_only_the_keys_asked_for(tmp_path):
    m = endpoint_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        ids = [client.post("/keys", json={"name": f"k{i}"}).json()["id"] for i in range(3)]
    for kid, n in zip(ids, (2, 0, 1), strict=True):
        for _ in range(n):
            _log(m, kid)
    store = EndpointRequestStore(m.db)
    assert store.request_counts(ids) == {ids[0]: 2, ids[2]: 1}
    assert store.request_counts([ids[2], ids[2], "k-missing"]) == {ids[2]: 1}
    assert store.request_counts([]) == {}
    assert m.modules.key_request_counts(ids[:1]) == {ids[0]: 2}
    m.db.close()


def test_logging_a_request_never_marks_a_revoked_key_used(tmp_path):
    """The log marks its key used through the core's `touch_api_key`, which leaves a revoked key alone: a request that
    finishes after its key was revoked is still logged, but the revoked key does not look active."""
    m = endpoint_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        kid = client.post("/keys", json={"name": "fake"}).json()["id"]
    _log(m, kid)
    used = m.db.get_api_key(kid)["last_used_at"]
    assert used is not None
    assert m.db.main.revoke_api_key(kid)
    _log(m, kid, status=499)
    with m.db.main.reading(), m.db.main.lock:
        statuses = [r[0] for r in m.db.main.conn.execute(
            "SELECT status FROM endpoint_requests WHERE key_id = ? ORDER BY id", (kid,)).fetchall()]
    assert statuses == [200, 499]
    assert m.db.main.get_api_key(kid)["last_used_at"] == used
    m.db.close()
