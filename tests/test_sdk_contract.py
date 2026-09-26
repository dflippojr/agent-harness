"""Issue #27: the one-file SDK and OpenAPI document stay in lockstep."""

from __future__ import annotations

import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.manager import Manager
import sdk.harness_client as sdk_module
from sdk.harness_client import ContractError, Harness, HarnessError, RunResult
from test_daemon import make_cfg


def test_sdk_validates_live_openapi_request_types_and_typed_responses(tmp_path):
    manager = Manager(make_cfg(tmp_path))
    with TestClient(create_app(manager)) as client:
        schema = client.get("/openapi.json").json()
    sdk = Harness("http://unused.invalid")
    try:
        sdk.validate_openapi(schema)
        create_response = schema["paths"]["/api/v1/sessions"]["post"]["responses"]["201"]
        assert create_response["content"]["application/json"]["schema"]["$ref"].endswith("SessionResponse")

        drifted = copy.deepcopy(schema)
        request = drifted["paths"]["/api/v1/sessions"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        model = sdk._resolve_schema(drifted, request)
        model["properties"].pop("backend")
        with pytest.raises(ContractError, match="create_session.*backend"):
            sdk.validate_openapi(drifted)
    finally:
        sdk.close()


def test_sdk_sends_provider_and_surfaces_usage_limits_billing_and_errors():
    seen = []
    session = {"id": "s1", "project": "scratch", "target": "tower", "backend": "codex", "model": "gpt",
               "title": "Task", "status": "done", "stop_reason": "final_message", "created_at": 1,
               "updated_at": 2, "totals": {"prompt_tokens": 12, "completion_tokens": 4}, "answer": "done",
               "failure": None, "app_tools": [], "metadata": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(201, json=session)
        if request.url.path.endswith("/approvals"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "missing", "error": {"code": "not_found"}})

    sdk = Harness("https://daemon.example", "ha-secret")
    sdk.client.close()
    sdk.client = httpx.Client(base_url=sdk.base, transport=httpx.MockTransport(handler),
                              headers={"Authorization": "Bearer ha-secret"})
    try:
        created = sdk.create_session("work", backend="codex", context={"Ticket": "A-17"})
        assert created["backend"] == "codex"
        assert seen[0].read().decode()
        assert '"backend":"codex"' in seen[0].content.decode()
        assert sdk.pending_approvals("s1") == []
        with pytest.raises(HarnessError) as exc:
            sdk.session("absent")
        assert exc.value.code == "not_found"
        assert not exc.value.retryable
    finally:
        sdk.close()

    result = RunResult(session=session, events=[
        {"type": "rate_limit", "data": {"utilization": 0.28}},
        {"type": "billing_warning", "data": {"message": "credits may be charged"}},
        {"type": "error", "data": {"code": "provider_error", "message": "failed"}},
    ])
    assert result.usage["prompt_tokens"] == 12
    assert result.limits["utilization"] == 0.28
    assert result.billing_notices == ["credits may be charged"]
    assert result.errors[0]["code"] == "provider_error"


def test_sdk_pair_binds_the_returned_client_to_the_approved_origin(monkeypatch):
    made = []

    class FakeClient:
        def __init__(self, base_url, timeout, headers):
            self.base_url, self.timeout, self.headers = base_url, timeout, headers
            made.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def close(self):
            return None

        def post(self, path, json):
            assert path == "/api/v1/pair"
            assert json == {"code": "pair-code-123"}
            return httpx.Response(201, json={"token": "ha-paired", "app": {"name": "browser"},
                                             "api_version": "1.5"})

    monkeypatch.setattr(sdk_module.httpx, "Client", FakeClient)
    paired = Harness.pair("https://daemon.example/", "pair-code-123", "https://app.example")
    assert paired.token == "ha-paired"
    assert paired.origin == "https://app.example"
    assert made[0].headers == {"Origin": "https://app.example"}
    assert made[1].headers == {"Authorization": "Bearer ha-paired", "Origin": "https://app.example"}
