"""#471 private namespaces, content minimization and external-effect failure evidence. Temp data only."""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from harness import audit_context, namespace_audit
from harness.accounts import AccountService
from harness.api import create_app
from harness.db import Database
from harness.manager import HarnessError, Manager
from sdk.harness_client import Harness, HarnessError as SDKError
from test_daemon import Script, make_cfg

PRIVATE = "SECRET_PROMPT_HEALTH_FINANCE_TOOL_PATH_LOGIN_CODE_CLAIM"
DAY = 86400


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = ["owner@example.com"]
    m = Manager(cfg, chat=Script([]))
    monkeypatch.setattr(m, "_spawn", lambda sid: None)
    members = [AccountService(m).create("owner", f"{name}@example.com", name) for name in ("alice", "bob")]
    apps = [m.db.main.create_api_key(name, "sessions approvals", kind="app") for name in ("app-a", "app-b")]
    headers = [{"Authorization": "Bearer " + secret} for _, secret in apps]
    with TestClient(create_app(m)) as client:
        yield m, client, members, apps, headers


def create(client, headers):
    r = client.post("/api/v1/sessions", json={"prompt": PRIVATE, "metadata": {"secret": PRIVATE}}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def page(client, headers, **params):
    r = client.get("/api/v1/audit", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_two_apps_two_members_owner_guesses_and_cursor(setup):
    m, client, members, apps, headers = setup
    actors = headers + [{"Tailscale-User-Login": f"{name}@example.com"} for name in ("alice", "bob")]
    scopes = [key["id"] for key, _ in apps] + [member["user_id"] for member in members]
    sids = [create(client, {**auth, "X-Actor": PRIVATE, "X-Source": PRIVATE}) for auth in actors]
    for auth, scope, sid in zip(actors, scopes, sids):
        client.put(f"/api/v1/sessions/{sid}", headers=auth, json={"title": PRIVATE})
        first = page(client, auth, limit=1)
        second = page(client, auth, limit=1, before_id=first["next_before_id"])
        assert first["items"][0]["action"] == "session.rename"
        assert second["items"][0]["action"] == "session.create"
        assert second["next_before_id"] is None
        for row in first["items"] + second["items"]:
            assert row["actor_id"] == scope
            assert row["target_id"] == sid
            assert row["source"] == "app_api"
            assert PRIVATE not in json.dumps(row)
        for other in set(sids) - {sid}:
            assert page(client, auth, target_id=other)["items"] == []
            assert client.post(f"/api/v1/sessions/{other}/messages", json={"content": PRIVATE}, headers=auth).status_code == 404
        assert client.delete("/api/v1/audit", headers=auth).status_code == 405
    assert client.get("/api/v1/audit").status_code == 403
    assert m.db.main.audit_page(action="session.create")["items"] == []
    assert m.db.main.conn.execute("SELECT COUNT(*) FROM namespace_audit").fetchone()[0] == 0
    # sessions:all never grants another namespace's audit, including Web/member detail.
    _, broad = m.db.main.create_api_key("broad", "sessions:all", kind="app")
    assert page(client, {"Authorization": "Bearer " + broad})["items"] == []


def test_member_legacy_actions_and_api_read(setup):
    m, client, members, _, _ = setup
    auth = {"Tailscale-User-Login": "alice@example.com"}
    sid = client.post("/sessions", headers=auth, json={"prompt": PRIVATE}).json()["id"]
    client.patch(f"/sessions/{sid}", headers=auth, json={"title": PRIVATE})
    rows = page(client, auth)["items"]
    assert {r["source"] for r in rows} == {"legacy_api"}
    assert {r["actor_id"] for r in rows} == {members[0]["user_id"]}
    assert all(r["key_id"] == "" for r in rows)


def test_message_context_approval_cancel_and_rerun_scoping(setup):
    m, client, _, apps, headers = setup
    auth, scope = headers[0], apps[0][0]["id"]
    sid = create(client, auth)
    assert client.post(f"/api/v1/sessions/{sid}/messages", headers=auth, json={"content": PRIVATE}).status_code == 200
    assert client.post(f"/api/v1/sessions/{sid}/context", headers=auth,
                       json={"context": [{"title": "ctx", "content": PRIVATE}]}).status_code == 200
    m.db.insert_approval({"id": "approval-a", "session_id": sid, "tool_call_id": "call-a", "tool": "shell",
                          "args": {"command": PRIVATE}, "reason": PRIVATE, "detail": PRIVATE, "status": "pending"})
    assert client.post(f"/api/v1/sessions/{sid}/approvals/approval-a", headers=auth,
                       json={"decision": "deny", "note": PRIVATE}).status_code == 200
    assert client.post(f"/api/v1/sessions/{sid}/cancel", headers=auth).status_code == 200
    again = client.post(f"/api/v1/sessions/{sid}/rerun", headers=auth)
    assert again.status_code == 201, again.text
    assert m.db.app_of(again.json()["id"]) == scope
    rows = page(client, auth)["items"]
    assert {r["action"] for r in rows} >= {"session.create", "session.message", "session.context", "approval.decide",
                                          "session.cancel", "session.rerun"}
    assert PRIVATE not in json.dumps(rows)


def test_explicit_delete_removes_detail_only_aggregate_receipt(setup, monkeypatch, caplog):
    m, client, _, apps, headers = setup
    sid = create(client, headers[0])
    foreign = create(client, headers[1])
    monkeypatch.setattr(m, "_erase_cli_history", AsyncMock())
    assert client.delete(f"/api/v1/sessions/{sid}", headers=headers[0]).status_code == 204
    assert page(client, headers[0])["items"] == []
    assert page(client, headers[1])["items"][0]["target_id"] == foreign
    receipts = m.db.main.audit_page(action="namespace.erase")["items"]
    assert [r["outcome"] for r in receipts] == ["ok", "started"]
    assert all(r["target_id"] == apps[0][0]["id"] for r in receipts)
    assert all(r["metadata"]["reason"] == "manual" and r["metadata"]["count"] == 1 for r in receipts)
    assert sid not in json.dumps(receipts) and PRIVATE not in json.dumps(receipts)
    assert sid not in caplog.text
    assert client.delete(f"/api/v1/sessions/{sid}", headers=headers[0]).status_code == 204
    assert len(m.db.main.audit_page(action="namespace.erase")["items"]) == 2


@pytest.mark.parametrize("outcome", ["started", "ok"])
def test_delete_intent_and_settlement_failure_no_repeat(setup, monkeypatch, outcome):
    m, client, _, _, headers = setup
    sid = create(client, headers[0])
    original = m.db.main.insert_audit
    def fail(*args, **kwargs):
        if args[2] == "namespace.erase" and args[3] == outcome:
            raise OSError(PRIVATE)
        return original(*args, **kwargs)
    monkeypatch.setattr(m.db.main, "insert_audit", fail)
    effect = AsyncMock()
    monkeypatch.setattr(m, "_erase_cli_history", effect)
    response = client.delete(f"/api/v1/sessions/{sid}", headers=headers[0])
    assert response.status_code == 503
    error = response.json()["error"]
    if outcome == "started":
        assert error["code"] == "audit_unavailable"
        effect.assert_not_awaited()
        assert m.db.get_session(sid)
    else:
        assert error["code"] == "audit_record_incomplete" and error["may_have_completed"]
        assert error["operation_id"] and not error["retryable"]
        effect.assert_awaited_once()
        assert m.db.get_session(sid) is None
        another = create(client, headers[0])
        assert client.delete(f"/api/v1/sessions/{another}", headers=headers[0]).status_code == 503
        effect.assert_awaited_once()
    assert PRIVATE not in response.text


def test_ordinary_gap_warns_keeps_mutation(setup, monkeypatch, caplog):
    _, client, _, _, headers = setup
    def fail(*args, **kwargs):
        raise OSError(PRIVATE)
    monkeypatch.setattr(Database, "insert_namespace_audit", fail)
    response = client.post("/api/v1/sessions", headers=headers[0], json={"prompt": PRIVATE})
    assert response.status_code == 201
    assert response.headers["X-Agent-Harness-Audit-Warning"] == "audit_gap"
    assert "private audit gap" in caplog.text and PRIVATE not in caplog.text


def test_private_login_retention_clock_restart_and_shorter_policy(setup, monkeypatch):
    m, client, _, apps, headers = setup
    scope = apps[0][0]["id"]
    store = m.db.for_app(scope)
    context = namespace_audit.key_context(apps[0][0])
    now = time.time()
    monkeypatch.setattr("harness.db.time.time", lambda: now)
    store.insert_namespace_audit(scope, "", context, "attempt-a", "login", "login.start", "started", {}, 2)
    sid = create(client, headers[0])
    monkeypatch.setattr("harness.db.time.time", lambda: now + 3 * DAY)
    assert len(page(client, headers[0])["items"]) == 1  # session rows do not have the login TTL
    store.prune_namespace_audit()
    assert store.conn.execute("SELECT COUNT(*) FROM namespace_audit WHERE session_id = ''").fetchone()[0] == 0
    m.db.close_idle(time.monotonic() + 1000)
    assert page(client, headers[0])["items"][0]["target_id"] == sid


def test_parallel_private_rows_and_filters(setup):
    m, client, _, apps, headers = setup
    sid = create(client, headers[0])
    session = m.db.get_session(sid)
    ctx = namespace_audit.key_context(apps[0][0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: namespace_audit.record(m.db, session, ctx, "session.message"), range(30)))
    rows = page(client, headers[0], action="session.message", outcome="ok")["items"]
    assert len(rows) == 30 and len({r["id"] for r in rows}) == 30
    assert page(client, headers[1], before_id=rows[0]["id"])["items"] == []


@pytest.mark.parametrize("outcome", ["started", "ok"])
def test_login_protocol_audit_failure_refuses_or_reports_effect(setup, monkeypatch, outcome):
    m, _, _, apps, _ = setup
    scope = apps[0][0]["id"]
    original = Database.insert_namespace_audit
    def fail(self, *args, **kwargs):
        if args[6] == outcome:
            raise OSError(PRIVATE)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Database, "insert_namespace_audit", fail)
    effect = AsyncMock()
    async def run():
        async with namespace_audit.login_operation(m.db, scope, "delegated-person", None, "login.start"):
            await effect()
    with pytest.raises(HarnessError) as caught:
        asyncio.run(run())
    assert caught.value.code == ("audit_unavailable" if outcome == "started" else "audit_record_incomplete")
    assert effect.await_count == (0 if outcome == "started" else 1)
    assert m.db.main.audit_page(action="login.start")["items"] == []


def test_sdk_incomplete_is_not_retryable():
    response = httpx.Response(503, json={"detail": "inspect before retry", "error": {
        "code": "audit_record_incomplete", "operation_id": "receipt-id", "may_have_completed": True}})
    with pytest.raises(SDKError) as caught:
        Harness._raise_response(response)
    assert not caught.value.retryable
    assert caught.value.may_have_completed and caught.value.operation_id == "receipt-id"
