"""The app API presents one lifecycle contract over local, Claude, Codex, and Cursor adapters."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.config import BackendConfig
from harness.llm import Completion
from harness.manager import Manager
from test_daemon import Script, make_cfg
from test_phase8 import _claude_manager, _codex_manager, _cursor_manager


def local_manager(root):
    return Manager(make_cfg(root), chat=Script([Completion(content="local done"), Completion(content="local done")]))


BUILDERS = {
    "local": local_manager,
    "claude": lambda root: _claude_manager(root, "echo")[0],
    "codex": lambda root: _codex_manager(root, "file")[0],
    "cursor": lambda root: _cursor_manager(root, "normal")[0],
}


def wait_session(client, headers, sid, newer_than=0.0, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = client.get(f"/api/v1/sessions/{sid}", headers=headers).json()
        if row["status"] in ("done", "failed", "cancelled") and row["updated_at"] > newer_than:
            return row
        time.sleep(0.03)
    raise AssertionError(f"session {sid} did not finish")


def parse_sse(text):
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


@pytest.mark.parametrize("backend", BUILDERS)
def test_same_app_lifecycle_contract_across_providers(tmp_path, backend):
    manager = BUILDERS[backend](tmp_path / backend)
    with TestClient(create_app(manager)) as client:
        token = client.post("/keys", json={"name": f"contract-{backend}", "kind": "app",
                                           "scopes": ["sessions", "approvals"]}).json()["key"]
        headers = {"Authorization": f"Bearer {token}"}
        created = client.post("/api/v1/sessions", headers=headers, json={
            "prompt": "Run the provider contract scenario", "backend": backend,
            "context": [{"title": "Initial", "content": "alpha"}],
            "tools": [{"name": "lookup_contract", "description": "Return the contract fixture"}],
            "metadata": {"scenario": "provider-contract"},
        })
        assert created.status_code == 201
        first = wait_session(client, headers, created.json()["id"])
        assert first["backend"] == backend
        assert first["failure"] is None
        assert first["app_tools"] == ["lookup_contract"]
        assert first["metadata"] == {"scenario": "provider-contract"}
        assert {"turns", "prompt_tokens", "completion_tokens", "total_cost_usd"} <= set(first["totals"])

        # Incremental context starts another run through the same adapter.
        sent = client.post(f"/api/v1/sessions/{first['id']}/context", headers=headers,
                           json={"context": [{"title": "Incremental", "content": "beta"}]})
        assert sent.status_code == 200
        assert sent.json()["status"] in ("queued", "running")
        final = wait_session(client, headers, first["id"], newer_than=first["updated_at"])
        assert final["status"] == "done"
        assert final["answer"]

        # Tool results and approvals use the same durable broker contract regardless of the model adapter.
        manager.db.insert_app_tool_call(first["id"], "app-call-1", "lookup_contract", {"key": "x"})
        accepted = client.post(f"/api/v1/sessions/{first['id']}/tool_calls/app-call-1", headers=headers,
                               json={"output": "fixture", "ok": True})
        assert accepted.json() == {"accepted": True}
        manager.db.insert_approval({"id": "a-contract", "session_id": first["id"], "tool_call_id": "native-1",
                                    "tool": "run_shell", "args": {"command": "echo ok"},
                                    "reason": "contract", "detail": "same approval shape"})
        pending = client.get(f"/api/v1/sessions/{first['id']}/approvals", headers=headers).json()
        assert pending[0]["id"] == "a-contract"
        assert "token" not in pending[0]
        decided = client.post(f"/api/v1/sessions/{first['id']}/approvals/a-contract", headers=headers,
                              json={"decision": "approve", "note": "contract"})
        assert decided.status_code == 200
        assert decided.json()["status"] == "approved"

        events = parse_sse(client.get(f"/api/v1/sessions/{first['id']}/events?follow=false",
                                      headers=headers).text)
        persisted = [event for event in events if event.get("seq") is not None]
        assert [event["seq"] for event in persisted] == sorted({event["seq"] for event in persisted})
        assert any(event["type"] == "run_finished" for event in persisted)
        pivot = persisted[len(persisted) // 2]["seq"]
        replay = parse_sse(client.get(
            f"/api/v1/sessions/{first['id']}/events?follow=false&after={pivot}", headers=headers).text)
        assert replay
        assert all(event.get("seq") is None or event["seq"] > pivot for event in replay)


@pytest.mark.parametrize("backend", ["claude", "codex", "cursor"])
def test_provider_failures_have_one_normalized_shape(tmp_path, backend):
    cfg = make_cfg(tmp_path / backend)
    cfg.backends[backend] = BackendConfig(enabled=True, model=f"{backend}-model")
    manager = Manager(cfg)
    manager._spawn = lambda *_args, **_kwargs: None
    session = manager.create("fail consistently", backend=backend)
    manager.runner._finish_cli_result(session["id"], {
        "subtype": "failed", "is_error": True, "result": "provider rejected the turn", "usage": {},
    })
    public = manager.summary(manager.get(session["id"]))
    assert public["status"] == "failed"
    assert public["stop_reason"] == "provider_error"
    assert public["failure"] == {"code": "provider_error", "provider": backend,
                                 "message": "provider rejected the turn", "retryable": True}
    error = [event for event in manager.db.events(session["id"]) if event["type"] == "error"][-1]
    assert error["data"] == public["failure"]
