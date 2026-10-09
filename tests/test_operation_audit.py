"""Owner operations: isolated stores and fake effects, no Docker, provider, GPU or live daemon."""
import asyncio
import json
import time
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness import audit_context, local_owner, operation_audit
from harness.admin import PREFIX
from harness.api import create_app
from harness.db import Database
from harness.manager import HarnessError
from harness_modules.backup.service import BackupService
from harness_modules.jobs.service import JobScheduler
from test_module_jobs import jobs_manager
from test_phase7 import seed

CTX = audit_context.owner_context({"id": "k-test"}, "admin_api")
SENTINEL = "SECRET/path/prompt/note/financial/health/command"


@pytest.fixture
def manager(tmp_path):
    m = jobs_manager(tmp_path)
    seed(m.db, "session1", "scratch", SENTINEL, [])
    yield m
    m.db.close()


def rows(m, action):
    return list(reversed(m.db.audit_page(action=action)["items"]))


def pair(m, action, context=CTX):
    result = rows(m, action)
    assert [r["outcome"] for r in result] == ["started", "ok"]
    assert len({r["metadata"]["operation_id"] for r in result}) == 1
    for r in result:
        assert (r["actor_id"], r["actor_kind"], r["key_id"], r["source"]) == (
            context.actor_id, context.actor_kind, context.key_id, context.source)
        assert SENTINEL not in json.dumps(r)
        assert r["detail"] == ""
    return result[-1]


def approval(m, aid="a-test", sid="session1"):
    m.db.insert_approval({"id": aid, "session_id": sid, "tool_call_id": "call1", "tool": "shell",
                          "args": {"command": SENTINEL}, "reason": SENTINEL, "detail": SENTINEL,
                          "status": "pending"})


@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("path,source", [("", "legacy_api"), (PREFIX, "admin_api"), ("/api/v1", "app_api")])
def test_approval_aliases(manager, path, source, approve):
    m = manager
    approval(m)
    key, secret = m.db.create_api_key("test", "admin sessions approvals", kind="owner")
    with TestClient(create_app(m)) as client:
        response = client.post(path + "/sessions/session1/approvals/a-test",
                               json={"decision": "approve" if approve else "deny", "note": SENTINEL},
                               headers={"Authorization": "Bearer " + secret, "X-Actor": SENTINEL})
        assert response.status_code == 200, response.text
        retry = client.post(path + "/sessions/session1/approvals/a-test", json={"decision": "approve"},
                            headers={"Authorization": "Bearer " + secret})
        assert retry.status_code == 409
        result = rows(m, "approval.decide")
        assert len([r for r in result if r["outcome"] == "ok"]) == 1
        assert result[-1]["outcome"] == "failure"
        assert result[-1]["metadata"]["reason"] == "already_decided"
        assert "decision" not in result[-1]["metadata"]
        success = next(r for r in result if r["outcome"] == "ok")
        assert success["source"] == source
        assert success["key_id"] == key["id"]
        assert success["metadata"]["decision"] == ("approved" if approve else "denied")
        assert SENTINEL not in json.dumps(result)


def test_notification_identity_and_idempotency(manager):
    approval(manager)
    token = manager.db.get_approval("a-test")["token"]
    manager.decide_by_token(token, True)
    manager.decide_by_token(token, False)
    ctx = audit_context.AuditContext("owner", "owner", "", "notification_link")
    record = pair(manager, "approval.decide", ctx)
    assert token not in json.dumps(record)


@pytest.mark.parametrize("token_kind", ["invalid", "revoked", "app", "owner_without_admin"])
def test_legacy_stray_token_preserves_ambient_owner(manager, token_kind):
    approval(manager)
    if token_kind == "invalid":
        secret = "invalid-token"
    else:
        key, secret = manager.db.create_api_key("test", "sessions", kind="app" if token_kind == "app" else "owner")
        if token_kind == "revoked":
            manager.db.revoke_api_key(key["id"])
    headers = {"Authorization": "Bearer " + secret, local_owner.HEADER: manager.local_owner_token}
    with TestClient(create_app(manager)) as client:
        # Admin authentication still rejects the token before attribution runs.
        rejected = client.post(PREFIX + "/sessions/session1/approvals/a-test",
                               json={"decision": "approve"}, headers=headers)
        assert rejected.status_code in (401, 403)
        response = client.post("/sessions/session1/approvals/a-test",
                               json={"decision": "approve"}, headers=headers)
        assert response.status_code == 200, response.text
        pair(manager, "approval.decide", audit_context.owner_context(None, "legacy_api"))
        job = client.post("/jobs", json={"name": "test", "prompt": "test", "cron": "@daily"}, headers=headers)
        assert job.status_code == 201, job.text
        record = rows(manager, "job.create")[-1]
        assert (record["actor_id"], record["actor_kind"], record["key_id"], record["source"]) == (
            "owner", "owner", "", "legacy_api")


@pytest.mark.parametrize("path", ["", PREFIX, "/api/v1"])
def test_rejected_approval_audit_failure_preserves_conflict(manager, monkeypatch, caplog, path):
    approval(manager)
    manager.decide("session1", "a-test", True, context=CTX)
    fail_write(monkeypatch, manager.db, "failure")
    _, secret = manager.db.create_api_key("test", "admin sessions approvals", kind="owner")
    with TestClient(create_app(manager)) as client:
        response = client.post(path + "/sessions/session1/approvals/a-test", json={"decision": "deny"},
                               headers={"Authorization": "Bearer " + secret})
        assert response.status_code == 409, response.text
    assert manager.db.get_approval("a-test")["status"] == "approved"
    pair(manager, "approval.decide")
    assert "audit record unavailable for an already-decided approval" in caplog.text
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("action", ["merge", "push", "discard"])
def test_review_fake_publish(manager, monkeypatch, action):
    effect = AsyncMock(return_value=manager.db.get_session("session1"))
    monkeypatch.setattr(manager, "_review", effect)
    asyncio.run(manager.review("session1", action, context=CTX))
    effect.assert_awaited_once()
    assert pair(manager, "review." + action)["target_id"] == "session1"


@pytest.mark.parametrize("kind", ["member", "app"])
def test_foreign_sessions_not_copied(manager, monkeypatch, kind):
    manager.db.update_session("session1", **({"owner_id": "u-member"} if kind == "member" else {"app_id": "app-a"}))
    approval(manager)
    manager.decide("session1", "a-test", True, context=CTX)
    monkeypatch.setattr(manager, "_review", AsyncMock(return_value={}))
    asyncio.run(manager.review("session1", "push", context=CTX))
    assert rows(manager, "approval.decide") == rows(manager, "review.push") == []


def test_unknown_approval_target_is_not_audited(manager):
    with pytest.raises(HarnessError):
        manager.decide("session1", "guessed", True, context=CTX)
    assert rows(manager, "approval.decide") == []


def fail_write(monkeypatch, db, outcome):
    original = db.insert_audit
    def insert(*args, **kwargs):
        if args[3] == outcome:
            raise OSError(SENTINEL)
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "insert_audit", insert)


@pytest.mark.parametrize("outcome", ["started", "ok"])
def test_publish_write_failure(manager, monkeypatch, caplog, outcome):
    effect = AsyncMock(return_value={})
    monkeypatch.setattr(manager, "_review", effect)
    fail_write(monkeypatch, manager.db, outcome)
    with pytest.raises(HarnessError) as caught:
        asyncio.run(manager.review("session1", "push", context=CTX))
    if outcome == "started":
        effect.assert_not_awaited()
        assert caught.value.code == "audit_unavailable"
        assert rows(manager, "review.push") == []
    else:
        effect.assert_awaited_once()
        assert caught.value.code == "audit_record_incomplete"
        assert caught.value.may_have_completed
        assert rows(manager, "review.push")[0]["metadata"]["operation_id"] == caught.value.operation_id
    assert SENTINEL not in caplog.text + str(caught.value)


def test_approval_terminal_failure_wakes_agent(manager, monkeypatch):
    approval(manager)
    event = asyncio.Event()
    manager.runner.approval_events["a-test"] = event
    fail_write(monkeypatch, manager.db, "ok")
    with pytest.raises(HarnessError, match="may have completed"):
        manager.decide("session1", "a-test", True, context=CTX)
    assert event.is_set()
    assert manager.db.get_approval("a-test")["status"] == "approved"


def test_cancel_and_reopen_leave_unresolved_start(tmp_path):
    path = tmp_path / "audit.sqlite3"
    db = Database(path)
    with pytest.raises(asyncio.CancelledError):
        with operation_audit.operation(db, CTX, "session1", "review.push"):
            raise asyncio.CancelledError()
    db.close()
    db = Database(path)
    assert [r["outcome"] for r in db.audit_page()["items"]] == ["started"]
    db.close()


def test_taint_clear(manager):
    manager.db.update_session("session1", taint=[{"origin": SENTINEL}])
    assert manager.clear_taint("session1", context=CTX)["taint"] == []
    pair(manager, "session.taint_clear")


@pytest.mark.parametrize("manual", [True, False])
def test_job_run_provenance_and_no_repeat(manager, monkeypatch, manual):
    now = time.time()
    job = {"id": "j-test", "name": SENTINEL, "prompt": SENTINEL, "cron": "@daily", "project": "scratch",
           "backend": "local", "model": "", "notify": "low", "enabled": True, "catch_up_minutes": 360,
           "next_run_at": now - 1}
    manager.db.insert_job(job)
    calls = []
    scheduler = JobScheduler(manager.db, lambda *a, **kw: calls.append(kw) or {"id": "session1"}, lambda _: False)
    assert scheduler.run(job, now, manual, context=CTX) == "session1"
    ctx = CTX if manual else audit_context.AuditContext("system", "system", "", "job")
    assert pair(manager, "job.run", ctx)["metadata"]["resulting_session_id"] == "session1"
    if not manual:
        fail_write(monkeypatch, manager.db, "ok")
        scheduler.run(job, now, context=CTX)
        assert manager.db.get_job("j-test")["last_session_id"] == "session1"
        assert scheduler.tick(now) == []
        assert len(calls) == 2


@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("kind", ["cleanup", "backup"])
def test_maintenance_aggregates(manager, monkeypatch, kind, manual):
    ctx = CTX if manual else audit_context.AuditContext("system", "system", "", "maintenance")
    if kind == "cleanup":
        service = manager.maintenance
        monkeypatch.setattr(service, "_containers", AsyncMock())
        monkeypatch.setattr(service, "_workspaces", lambda _, r: r["workspaces_removed"].append(SENTINEL))
        monkeypatch.setattr(service, "_remote_workspaces", AsyncMock())
        service.app_sweep = AsyncMock(return_value={"apps_erased": [SENTINEL], "sessions_expired": [SENTINEL]})
        effect = service.cleanup
    else:
        service = BackupService(manager.cfg, manager.db)
        monkeypatch.setattr(service, "_backup_sync", lambda _: {"path": SENTINEL, "bytes": 1, "removed": [SENTINEL]})
        monkeypatch.setattr(service, "_write_backup_status", lambda: None)
        effect = service.backup
    report = asyncio.run(effect(context=CTX if manual else None))
    if kind == "cleanup":
        assert SENTINEL not in json.dumps(report)
        assert report == service.last_report
        assert report["sessions_expired"] == 1
    record = pair(manager, "maintenance." + kind, ctx)
    assert record["metadata"]["removed"] == (2 if kind == "cleanup" else 1)
    assert record["metadata"]["trigger"] == ("manual" if manual else "scheduled")


def test_metadata_allowlists():
    for action in operation_audit.ACTIONS:
        result = audit_context.clean_metadata(action, {"prompt": SENTINEL, "note": SENTINEL, "path": SENTINEL,
                    "fields": [SENTINEL, "enabled"], "session_id": SENTINEL, "turn": True,
                    "decision": SENTINEL, "reviewer_mode": SENTINEL, "expired": -1})
        assert SENTINEL not in json.dumps(result)


def test_auto_decision_is_system(manager):
    existing = {"id": "a-auto", "session_id": "session1", "tool_call_id": "call1", "tool": "shell",
                "args": {"command": SENTINEL}, "reason": SENTINEL}
    manager.runner._persist_ask("session1", existing, {"status": "approved", "smart": {}})
    pair(manager, "approval.auto_decide", audit_context.AuditContext("system", "system", "", "agent"))


@pytest.mark.parametrize("path,source", [("", "legacy_api"), (PREFIX, "admin_api")])
def test_job_mutation_aliases(manager, path, source, monkeypatch):
    m = manager
    key, secret = m.db.create_api_key("test", "admin", kind="owner")
    headers = {"Authorization": "Bearer " + secret}
    body = {"name": SENTINEL, "prompt": SENTINEL, "cron": "@daily"}
    with TestClient(create_app(m)) as client:
        response = client.post(path + "/jobs", json=body, headers=headers)
        assert response.status_code == 201, response.text
        jid = response.json()["id"]
        assert client.put(path + "/jobs/" + jid, json={**body, "enabled": False}, headers=headers).status_code == 200
        changed = rows(m, "job.update")[-1]
        assert changed["metadata"]["fields"] == ["enabled"]
        assert changed["metadata"]["enabled"] is False
        assert changed["metadata"]["operation_id"]
        assert client.post(path + "/jobs/" + jid + "/run", headers=headers).status_code == 201
        assert client.delete(path + "/jobs/" + jid, headers=headers).status_code == 204
        for action in ("job.create", "job.update", "job.delete", "job.run"):
            records = rows(m, action)
            assert records[-1]["key_id"] == key["id"]
            assert records[-1]["source"] == source
            assert SENTINEL not in json.dumps(records)
        assert client.delete(path + "/jobs/guessed", headers=headers).status_code == 404
        assert len(rows(m, "job.delete")) == 1
        fail_write(monkeypatch, m.db, "ok")
        with pytest.raises(OSError):
            client.post(path + "/jobs", json=body, headers=headers)
        assert m.db.list_jobs() == []  # mutation and audit share the main writer transaction


@pytest.mark.parametrize("kind", ["fork", "rewind"])
@pytest.mark.parametrize("path,source", [("", "legacy_api"), (PREFIX, "admin_api")])
def test_checkpoint_aliases(manager, monkeypatch, kind, path, source):
    m = manager
    key, secret = m.db.create_api_key("test", "admin", kind="owner")
    monkeypatch.setattr(m, "_idle_for_checkpoint", AsyncMock(return_value=m.db.get_session("session1")))
    if kind == "fork":
        seed(m.db, "new-session", "scratch", SENTINEL, [])
        monkeypatch.setattr(m, "_fork", AsyncMock(return_value="new-session"))
        monkeypatch.setattr(m, "_spawn", lambda _: None)
    else:
        cp = m.runner.checkpointer
        monkeypatch.setattr(cp, "restore", lambda *a: SimpleNamespace(saved={}))
        monkeypatch.setattr(cp, "commit_rewind", lambda *a: None)
        monkeypatch.setattr(cp, "release", lambda *a: None)
    with TestClient(create_app(m)) as client:
        response = client.post(path + "/sessions/session1/checkpoints/2/" + kind,
                               json={"prompt": SENTINEL}, headers={"Authorization": "Bearer " + secret})
        assert response.status_code in (200, 201), response.text
        record = pair(m, "checkpoint." + kind, audit_context.owner_context(key, source))
        assert record["metadata"]["turn"] == 2
        if kind == "fork":
            assert record["metadata"]["resulting_session_id"] == "new-session"


@pytest.mark.parametrize("path,source", [("", "legacy_api"), (PREFIX, "admin_api"), ("/api/v1", "app_api")])
@pytest.mark.parametrize("action", ["merge", "push", "discard"])
def test_review_aliases_and_error_contract(manager, monkeypatch, path, source, action):
    m = manager
    key, secret = m.db.create_api_key("test", "admin sessions", kind="owner")
    monkeypatch.setattr(m, "_review", AsyncMock(return_value=m.db.get_session("session1")))
    with TestClient(create_app(m)) as client:
        response = client.post(path + "/sessions/session1/review/" + action,
                               headers={"Authorization": "Bearer " + secret})
        assert response.status_code == 200, response.text
        pair(m, "review." + action, audit_context.owner_context(key, source))
        fail_write(monkeypatch, m.db, "ok")
        response = client.post(path + "/sessions/session1/review/" + action,
                               headers={"Authorization": "Bearer " + secret})
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "audit_record_incomplete"
        assert error["may_have_completed"] and not error["retryable"]
        assert error["operation_id"] == rows(m, "review." + action)[-1]["metadata"]["operation_id"]


@pytest.mark.parametrize("outcome", ["started", "ok"])
def test_cleanup_write_failure_preserves_effect(manager, monkeypatch, outcome):
    service = manager.maintenance
    effect = AsyncMock()
    monkeypatch.setattr(service, "_containers", effect)
    monkeypatch.setattr(service, "_workspaces", lambda *a: None)
    monkeypatch.setattr(service, "_remote_workspaces", AsyncMock())
    service.app_sweep = None
    fail_write(monkeypatch, manager.db, outcome)
    with pytest.raises(HarnessError):
        asyncio.run(service.cleanup(context=CTX))
    assert effect.await_count == (0 if outcome == "started" else 1)
    if outcome == "ok":
        assert service.last_report
        assert [r["outcome"] for r in rows(manager, "maintenance.cleanup")] == ["started"]


def test_async_cancellation_preserves_intent(manager):
    async def body():
        with pytest.raises(asyncio.CancelledError):
            async with operation_audit.async_operation(manager.db, CTX, "session1", "review.push"):
                raise asyncio.CancelledError()
    asyncio.run(body())
    assert [r["outcome"] for r in rows(manager, "review.push")] == ["started"]


@pytest.mark.parametrize("path,source", [("", "legacy_api"), (PREFIX, "admin_api")])
def test_taint_and_maintenance_route_contexts(manager, monkeypatch, path, source):
    m = manager
    key, secret = m.db.create_api_key("test", "admin", kind="owner")
    headers = {"Authorization": "Bearer " + secret}
    cleanup = m.maintenance
    monkeypatch.setattr(cleanup, "_containers", AsyncMock())
    monkeypatch.setattr(cleanup, "_workspaces", lambda *a: None)
    monkeypatch.setattr(cleanup, "_remote_workspaces", AsyncMock())
    cleanup.app_sweep = None
    backup = m.modules.get("backup").service
    monkeypatch.setattr(backup, "_backup_sync", lambda _: {"path": SENTINEL, "bytes": 1, "removed": []})
    monkeypatch.setattr(backup, "_write_backup_status", lambda: None)
    with TestClient(create_app(m)) as client:
        for endpoint in ("/sessions/session1/taint/clear", "/maintenance/cleanup", "/maintenance/backup"):
            response = client.post(path + endpoint, headers=headers)
            assert response.status_code == 200, response.text
        for action in ("session.taint_clear", "maintenance.cleanup", "maintenance.backup"):
            pair(m, action, audit_context.owner_context(key, source))


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("terminal_fails", [False, True])
def test_failed_effect_and_settlement(manager, monkeypatch, caplog, scheduled, terminal_fails):
    if terminal_fails:
        fail_write(monkeypatch, manager.db, "unknown")
    expected = HarnessError if terminal_fails and not scheduled else ValueError
    with pytest.raises(expected):
        with operation_audit.operation(manager.db, CTX, "session1", "review.push", scheduled=scheduled):
            raise ValueError(SENTINEL)
    assert SENTINEL not in caplog.text
    assert [r["outcome"] for r in rows(manager, "review.push")] == (
        ["started"] if terminal_fails else ["started", "unknown"])


def test_safe_metadata_types_and_unknown_source():
    assert audit_context.AuditContext("owner", source=SENTINEL).source == "unknown"
    assert audit_context.clean_metadata("job.update", {"fields": SENTINEL, "enabled": 1}) == {}
    assert audit_context.clean_metadata("checkpoint.rewind", {"turn": 2**53, "session_id": 42}) == {}
    assert audit_context.clean_metadata("maintenance.cleanup", {"removed": 3}) == {"removed": 3}


def test_scheduled_backup_audit_gap_keeps_success(manager, monkeypatch, caplog):
    service = BackupService(manager.cfg, manager.db)
    calls = []
    monkeypatch.setattr(service, "_backup_sync", lambda _: calls.append(1) or {"path": "fake", "bytes": 1, "removed": []})
    monkeypatch.setattr(service, "_write_backup_status", lambda: None)
    fail_write(monkeypatch, manager.db, "ok")
    assert asyncio.run(service.backup())["bytes"] == 1
    assert service.last_backup["bytes"] == 1 and calls == [1]
    assert "audit settlement unavailable" in caplog.text
    assert [r["outcome"] for r in rows(manager, "maintenance.backup")] == ["started"]


def test_compare_preserves_review_initiator(manager, monkeypatch):
    m = manager
    seed(m.db, "session2", "scratch", SENTINEL, [])
    for sid in ("session1", "session2"):
        m.db.update_session(sid, compare_group="group1")
    async def review(sid, action):
        m.db.update_session(sid, review="merged" if action == "merge" else "discarded")
        return m.db.get_session(sid)
    monkeypatch.setattr(m, "_review", review)
    asyncio.run(m.compare_pick("group1", "session1", "merge", True, context=CTX))
    assert pair(m, "review.merge")["target_id"] == "session1"
    assert pair(m, "review.discard")["target_id"] == "session2"
