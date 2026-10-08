"""#471 private namespaces, content minimization and external-effect failure evidence. Temp data only."""
import asyncio
import hashlib
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


def test_failed_known_action_has_truthful_outcome(setup):
    _, client, _, _, headers = setup
    sid = create(client, headers[0])
    response = client.post(f"/api/v1/sessions/{sid}/messages", headers=headers[0], json={"content": " "})
    assert response.status_code == 400
    rows = page(client, headers[0], action="session.message")["items"]
    assert len(rows) == 1 and rows[0]["outcome"] == "failure"


def test_interrupted_erase_survives_restart_blocks_automatic_repeat(setup, monkeypatch):
    m, _, _, apps, headers = setup
    scope = apps[0][0]["id"]
    async def interrupted():
        async with namespace_audit.erase_operation(m.db, scope, None, "app", "retention", 1):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(interrupted())
    reopened = Database(m.db.main.path)
    try:
        with pytest.raises(HarnessError) as caught:
            namespace_audit.refuse_unresolved_erase(SimpleNamespace(main=reopened), scope)
        assert caught.value.may_have_completed
        assert reopened.audit_page(action="namespace.erase")["items"][0]["actor_kind"] == "system"
    finally:
        reopened.close()


def test_login_finish_truthful_and_settlement_error_visible(setup, monkeypatch):
    m, client, _, apps, headers = setup
    scope = apps[0][0]["id"]
    store = namespace_audit.LoginStore(m.db, scope, "delegated-user", backend="claude")
    context = namespace_audit.key_context(apps[0][0])
    attempt = SimpleNamespace(attempt_id="known-attempt", state="completed", audit_incomplete="")
    store.finish(context, "operation-a")(attempt)
    row = page(client, headers[0], action="login.finish")["items"][0]
    assert row["outcome"] == "ok" and row["metadata"]["subject_trust"] == "caller_asserted"
    assert row["metadata"]["backend"] == "claude"
    assert row["actor_id"] == scope
    def fail(*args, **kwargs):
        raise OSError(PRIVATE)
    monkeypatch.setattr(Database, "insert_namespace_audit", fail)
    store.finish(context, "operation-b")(attempt)
    assert attempt.audit_incomplete == "operation-b"


def test_erase_app_drops_private_audit_and_only_receipts_remain(setup, monkeypatch):
    m, client, _, apps, headers = setup
    scope = apps[0][0]["id"]
    sid = create(client, headers[0])
    effect = AsyncMock()
    monkeypatch.setattr("harness.cli_domains.drop_app_volumes", effect)
    m.db.revoke_api_key(scope)
    asyncio.run(m.erase_app(scope))
    assert not m.db.store_path(scope).exists()
    rows = m.db.main.audit_page(action="namespace.erase")["items"]
    assert len(rows) == 2
    assert all(r["actor_id"] == "system" and r["metadata"]["reason"] == "revoked_app" for r in rows)
    assert sid not in json.dumps(rows) and PRIVATE not in json.dumps(rows)


def test_checksums_and_record_does_not_resurrect_erased_detail(setup):
    m, client, _, apps, headers = setup
    sid = create(client, headers[0])
    session = m.db.get_session(sid)
    store = m.db.for_app(apps[0][0]["id"])
    row = dict(store.conn.execute("SELECT * FROM namespace_audit").fetchone())
    values = [row[k] for k in ("ts", "namespace", "session_id", "actor_id", "actor_kind", "key_id", "source",
                              "target_id", "target_kind", "action", "outcome", "metadata", "expires_at")]
    assert row["checksum"] == hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()
    m.db.delete_session(sid)
    namespace_audit.record(m.db, session, None, "session.cancel")
    assert page(client, headers[0])["items"] == []
    assert m.db.web.conn.execute("SELECT COUNT(*) FROM namespace_audit").fetchone()[0] == 0


def test_concurrent_delete_runs_effect_once_and_refuses_message(setup, monkeypatch):
    m, client, _, _, headers = setup
    sid = create(client, headers[0])
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def effect(session):
            entered.set()
            await release.wait()
        fake = AsyncMock(side_effect=effect)
        monkeypatch.setattr(m, "_erase_cli_history", fake)
        first = asyncio.create_task(m.erase_session(sid))
        await entered.wait()
        second = asyncio.create_task(m.erase_session(sid))
        with pytest.raises(HarnessError, match="erasure"):
            await m.send(sid, PRIVATE)
        release.set()
        assert await asyncio.gather(first, second) == [True, False]
        fake.assert_awaited_once()
    asyncio.run(run())
    assert len(m.db.main.audit_page(action="namespace.erase")["items"]) == 2


def test_shortened_app_login_retention_applies_to_reads_immediately(setup, monkeypatch):
    m, client, _, apps, headers = setup
    key = apps[0][0]
    now = time.time()
    store = m.db.for_app(key["id"])
    store.insert_namespace_audit(key["id"], "", namespace_audit.key_context(key), "attempt", "login",
                                 "login.start", "started", {}, 30)
    m.db.set_app_retention(key["id"], 1)
    monkeypatch.setattr("harness.db.time.time", lambda: now + 2 * DAY)
    assert page(client, headers[0])["items"] == []
    asyncio.run(m.sweep_app_data(now + 2 * DAY))
    assert store.conn.execute("SELECT COUNT(*) FROM namespace_audit").fetchone()[0] == 0


def test_sdk_audit_cursor_filters_and_gap_warning():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [], "next_before_id": None},
                              headers={"X-Agent-Harness-Audit-Warning": "audit_gap"})
    with Harness("https://unused.invalid") as sdk:
        sdk.client.close()
        sdk.client = httpx.Client(base_url=sdk.base, transport=httpx.MockTransport(respond))
        assert sdk.audit(limit=10, before_id=42, action="login.finish")["items"] == []
        assert seen[0].url.path == "/api/v1/audit" and seen[0].url.params["before_id"] == "42"
        assert sdk.audit_warning == "audit_gap"


def test_cli_private_reader_uses_scoped_api(monkeypatch, capsys):
    from harness import cli
    seen = []
    monkeypatch.setattr("sys.argv", ["harness", "audit", "private", "--limit", "10", "--before-id", "42",
                                    "--actor-id", "app-a", "--key-id", "app-a"])
    monkeypatch.setattr(cli, "configure", lambda path: {})
    monkeypatch.setattr(cli, "api", lambda method, path, **kwargs: seen.append((method, path, kwargs)) or {"items": []})
    assert cli.main() == 0
    assert seen == [("GET", "/audit", {"prefix": "/api/v1", "params": {
        "limit": 10, "before_id": 42, "actor_id": "app-a", "key_id": "app-a"}})]
    assert '"items"' in capsys.readouterr().out


def test_metadata_allowlist_and_actor_truth():
    assert namespace_audit.clean({"fields": ["prompt", "output", PRIVATE], "count": True,
                                   "sessions": -1, "reason": [PRIVATE], "decision": PRIVATE,
                                   "subject_trust": "verified", "secret": PRIVATE}) == {"fields": ["output", "prompt"]}
    assert namespace_audit.clean({"count": 3, "sessions": 2, "reason": "retention", "decision": "approved",
                                  "subject_trust": "caller_asserted"}) == {
                                      "count": 3, "sessions": 2, "reason": "retention", "decision": "approved",
                                      "subject_trust": "caller_asserted"}
    assert namespace_audit.key_context({"id": "device-a", "kind": "device"}).actor_kind == "device"
    assert namespace_audit.key_context({"id": "", "kind": "owner", "bundled": True}).actor_id == "owner"


def test_private_auto_approval_is_system_agent_and_never_in_main(setup):
    m, client, _, _, headers = setup
    sid = create(client, headers[0])
    approval = {"id": "auto-approval", "session_id": sid, "tool_call_id": "call-auto", "tool": "shell",
                "args": {"command": PRIVATE}, "reason": PRIVATE, "detail": PRIVATE, "status": "pending"}
    m.runner._persist_ask(sid, approval, {"status": "approved"})
    row = page(client, headers[0], action="approval.auto_decide")["items"][0]
    assert (row["actor_id"], row["actor_kind"], row["source"]) == ("system", "system", "agent")
    assert PRIVATE not in json.dumps(row)
    assert m.db.main.audit_page(action="approval.auto_decide")["items"] == []


def test_member_erasure_leaves_only_account_receipt(setup):
    m, client, members, _, _ = setup
    auth = {"Tailscale-User-Login": "alice@example.com"}
    sid = create(client, auth)
    ctx = audit_context.AuditContext(members[0]["user_id"], "member", "", "app_api")
    asyncio.run(m.erase_session(sid, context=ctx))
    assert page(client, auth)["items"] == []
    rows = m.db.main.audit_page(action="namespace.erase")["items"]
    assert all(r["target_kind"] == "account" and r["target_id"] == members[0]["user_id"] for r in rows)
    assert sid not in json.dumps(rows) and PRIVATE not in json.dumps(rows)


def test_retention_removes_private_rows_with_system_reason(setup):
    m, client, _, apps, headers = setup
    sid = create(client, headers[0])
    m.db.set_app_retention(apps[0][0]["id"], 1)
    asyncio.run(m.sweep_app_data(time.time() + 2 * DAY))
    assert page(client, headers[0])["items"] == []
    rows = m.db.main.audit_page(action="namespace.erase")["items"]
    assert [r["outcome"] for r in rows] == ["ok", "started"]
    assert all(r["metadata"]["reason"] == "retention" and r["actor_id"] == "system" for r in rows)
    assert sid not in json.dumps(rows)


def test_partial_provider_erase_does_not_replay_or_leak_exception(setup, monkeypatch, caplog):
    m, client, _, apps, headers = setup
    sid = create(client, headers[0])
    m.db.set_app_retention(apps[0][0]["id"], 1)
    effect = AsyncMock(side_effect=OSError(PRIVATE))
    monkeypatch.setattr(m, "_erase_cli_history", effect)
    asyncio.run(m.sweep_app_data(time.time() + 2 * DAY))
    asyncio.run(m.sweep_app_data(time.time() + 3 * DAY))
    effect.assert_awaited_once()
    rows = m.db.main.audit_page(action="namespace.erase")["items"]
    assert [r["outcome"] for r in rows] == ["unknown", "started"]
    assert PRIVATE not in caplog.text and sid not in caplog.text


def test_failed_approval_uses_verified_approval_target_and_drops_guesses(setup):
    m, client, _, _, headers = setup
    sid = create(client, headers[0])
    m.db.insert_approval({"id": "known-approval", "session_id": sid, "tool_call_id": "call", "tool": "shell",
                          "args": {}, "reason": PRIVATE, "detail": PRIVATE, "status": "pending"})
    url = f"/api/v1/sessions/{sid}/approvals/known-approval"
    assert client.post(url, headers=headers[0], json={"decision": "deny"}).status_code == 200
    assert client.post(url, headers=headers[0], json={"decision": "deny"}).status_code == 409
    rows = page(client, headers[0], target_id="known-approval")["items"]
    assert [r["outcome"] for r in rows] == ["failure", "ok"]
    assert all(r["target_kind"] == "approval" for r in rows)
    assert client.post(f"/api/v1/sessions/{sid}/approvals/{PRIVATE}", headers=headers[0],
                       json={"decision": "deny"}).status_code == 404
    unknown = page(client, headers[0], action="approval.decide")["items"][0]
    assert unknown["target_id"] == "" and unknown["target_kind"] == "approval"
    assert PRIVATE not in json.dumps(unknown)
