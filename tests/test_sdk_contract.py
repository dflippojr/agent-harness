"""Issue #27: the one-file SDK and OpenAPI document stay in lockstep."""

from __future__ import annotations

import copy
import json

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


class FakeAttachServer:
    """Synthetic session/tool/event responses for Harness.attach."""

    def __init__(self, session, pending, events, results=None):
        self.session = session
        self.pending = pending
        self.events = events
        self.requests = []
        self.results = results if results is not None else []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/v1/sessions/s1" and request.method == "GET":
            return httpx.Response(200, json=self.session)
        if path.endswith("/tool_calls") and request.method == "GET":
            return httpx.Response(200, json=self.pending)
        if "/tool_calls/" in path and request.method == "POST":
            body = json.loads(request.content)
            self.results.append((path.rsplit("/", 1)[1], body))
            self.pending[:] = [c for c in self.pending if c["call_id"] != path.rsplit("/", 1)[1]]
            return httpx.Response(200, json={})
        if path.endswith("/events"):
            after = int(request.url.params["after"])
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in self.events if e["seq"] > after)
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"detail": "missing"})

    def sdk(self):
        sdk = Harness("https://daemon.example")
        sdk.client.close()
        sdk.client = httpx.Client(base_url=sdk.base, transport=httpx.MockTransport(self.handler))
        return sdk

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


def _call(call_id, name="echo", **args):
    return {"call_id": call_id, "name": name, "args": args}


def _attach_tools(log):
    @sdk_module.tool("Echo", text={"type": "string"})
    def echo(text: str) -> str:
        log.append(text)
        return f"echo {text}"

    @sdk_module.tool("Boom")
    def boom() -> str:
        raise ValueError("bad")

    return [echo, boom]


def _active(seq=5):
    return {"id": "s1", "status": "running", "last_event_seq": seq, "answer": ""}


def test_attach_serves_pending_call_once_and_returns_final_session():
    log = []
    done = {"id": "s1", "status": "done", "last_event_seq": 8, "answer": "ok", "totals": {"prompt_tokens": 3}}
    server = FakeAttachServer(_active(), [_call("c1", text="a")], [
        {"seq": 4, "type": "run_finished", "data": {}},
        {"seq": 6, "type": "app_tool_call", "data": _call("c1", text="a")},
        {"seq": 7, "type": "app_tool_call", "data": _call("c2", "boom")},
        {"seq": 8, "type": "run_finished", "data": {}},
    ])
    seen = []
    with server.sdk() as sdk:
        server.pending.append(_call("c2", "boom"))
        original = server.handler

        def handler(request):
            if request.url.path.endswith("/events"):
                server.session = done
            return original(request)

        sdk.client = httpx.Client(base_url=sdk.base, transport=httpx.MockTransport(handler))
        result = sdk.attach("s1", tools=_attach_tools(log), on_event=seen.append)
    assert log == ["a"]
    assert [r[0] for r in server.results] == ["c1", "c2"]
    assert server.results[1][1]["ok"] is False and "ValueError" in server.results[1][1]["output"]
    assert [e["seq"] for e in result.events] == [6, 7, 8] == [e["seq"] for e in seen]
    assert result.answer == "ok" and result.usage == {"prompt_tokens": 3}
    assert all("/messages" not in p and p != "/api/v1/sessions" for _, p in server.posts())


def test_attach_skips_replayed_call_that_is_no_longer_pending_and_reports_unknown_tool():
    log = []
    server = FakeAttachServer(_active(), [_call("c2", "missing")], [
        {"seq": 6, "type": "app_tool_call", "data": _call("c1", text="old")},
        {"seq": 7, "type": "app_tool_call", "data": _call("c2", "missing")},
        {"seq": 8, "type": "run_finished", "data": {}},
    ])
    with server.sdk() as sdk:
        sdk.attach("s1", tools=_attach_tools(log))
    assert log == []
    assert [(r[0], r[1]["ok"]) for r in server.results] == [("c2", False)]
    assert "unknown tool missing" in server.results[0][1]["output"]


def test_keyed_run_retries_do_not_execute_completed_tool_calls_again():
    log = []
    done = {"id": "s1", "status": "done", "last_event_seq": 2, "answer": "purchased"}
    server = FakeAttachServer(_active(0), [_call("c1", text="purchase")], [
        {"seq": 1, "type": "app_tool_call", "data": _call("c1", text="purchase")},
        {"seq": 2, "type": "run_finished", "data": {}},
    ])
    creates = []

    def handler(request):
        if request.url.path == "/api/v1/sessions" and request.method == "POST":
            assert request.headers["Idempotency-Key"] == "order-7"
            creates.append(json.loads(request.content))
            return httpx.Response(201 if len(creates) == 1 else 200, json=server.session)
        if request.url.path.endswith("/events"):
            server.session = done
        return server.handler(request)

    with server.sdk() as sdk:
        sdk.client.close()
        sdk.client = httpx.Client(base_url=sdk.base, transport=httpx.MockTransport(handler))
        tools = _attach_tools(log)
        first = sdk.run("buy once", tools=tools, idempotency_key="order-7")
        replay = sdk.run("buy once", tools=tools, idempotency_key="order-7")
    assert first.session == replay.session == done
    assert creates[0] == creates[1]
    assert log == ["purchase"]
    assert [call_id for call_id, _ in server.results] == ["c1"]


def test_keyed_run_retry_drives_the_current_followup_past_an_earlier_finish():
    log = []
    done = {"id": "s1", "status": "done", "last_event_seq": 7, "answer": "follow-up done"}
    current = _call("c2", text="follow-up")
    server = FakeAttachServer(_active(5), [], [
        {"seq": 2, "type": "run_finished", "data": {}},
        {"seq": 6, "type": "app_tool_call", "data": current},
        {"seq": 7, "type": "run_finished", "data": {}},
    ])

    def handler(request):
        if request.url.path == "/api/v1/sessions" and request.method == "POST":
            return httpx.Response(200, json=server.session)
        if request.url.path.endswith("/events"):
            server.pending.append(current)
            server.session = done
        return server.handler(request)

    with server.sdk() as sdk:
        sdk.client.close()
        sdk.client = httpx.Client(base_url=sdk.base, transport=httpx.MockTransport(handler))
        result = sdk.run("buy once", tools=_attach_tools(log), idempotency_key="order-7")
    assert result.session == done
    assert [e["seq"] for e in result.events] == [6, 7]
    assert log == ["follow-up"]
    assert [call_id for call_id, _ in server.results] == ["c2"]


def test_attach_ignores_earlier_run_finish_and_returns_on_terminal_status_without_tools():
    log = []
    server = FakeAttachServer(_active(5), [], [
        {"seq": 3, "type": "run_finished", "data": {}},
        {"seq": 6, "type": "run_finished", "data": {}},
    ])
    with server.sdk() as sdk:
        result = sdk.attach("s1", tools=_attach_tools(log))
    assert [e["seq"] for e in result.events] == [6]

    for status in ("done", "failed", "cancelled"):
        server = FakeAttachServer({"id": "s1", "status": status, "last_event_seq": 2}, [_call("c1", text="x")], [])
        with server.sdk() as sdk:
            assert sdk.attach("s1", tools=_attach_tools(log)).status == status
        assert server.requests == [("GET", "/api/v1/sessions/s1")] and log == []


def test_attach_propagates_not_found_without_creating_anything():
    server = FakeAttachServer(_active(), [], [])
    server.session = None
    with server.sdk() as sdk:
        sdk.client = httpx.Client(base_url=sdk.base, transport=httpx.MockTransport(
            lambda r: httpx.Response(404, json={"detail": "nope"})))
        with pytest.raises(HarnessError) as exc:
            sdk.attach("s1")
    assert exc.value.status == 404


def test_sdk_pair_keeps_the_minted_app_and_its_catalog_app_id(tmp_path):
    """Synthetic round trip: the pairing response's typed `app` object reaches `Harness.paired_app`."""
    app = {"id": "k-1234abcd", "name": "shop", "prefix": "ha-abcdefg", "created_at": 1.0, "scopes": "sessions",
           "kind": "app", "origins": ["https://shop.example"], "catalog_app_id": "com.example.shop"}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"token": "ha-" + "x" * 43, "app": app, "api_version": "1.22"})

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    sdk_module.httpx.Client = lambda **kw: real_client(transport=transport, **kw)
    try:
        paired = Harness.pair("https://daemon.example", "hp-code", "https://shop.example")
    finally:
        sdk_module.httpx.Client = real_client
    try:
        assert paired.paired_app == app
        assert seen[0].headers["Origin"] == "https://shop.example"
        assert json.loads(seen[0].content) == {"code": "hp-code"}
    finally:
        paired.close()

    manager = Manager(make_cfg(tmp_path))
    with TestClient(create_app(manager)) as client:
        schema = client.get("/openapi.json").json()
    sdk = Harness("http://unused.invalid")
    try:
        drifted = copy.deepcopy(schema)
        drifted["components"]["schemas"]["PairedAppResponse"]["properties"].pop("catalog_app_id")
        with pytest.raises(ContractError, match="catalog_app_id"):
            sdk.validate_openapi(drifted)
    finally:
        sdk.close()
