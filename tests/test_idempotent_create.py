"""#462: an App retries POST /api/v1/sessions with the same Idempotency-Key after losing the response and gets the
session the first request created, never a second run."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import apps, idempotency
from harness.api import create_app
from harness.app_stores import APP_STORE_FILE
from harness.llm import Completion
from harness.manager import HarnessError, Manager

from test_app_stores import _key, rows
from test_daemon import Script, make_cfg
from test_phase6 import wait_for

BODY = {"prompt": "plan dinner", "metadata": {"order": 7}}


def _manager(cfg) -> Manager:
    """A manager whose `spawned` lists the sessions it handed to the runner."""
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    spawned: list[str] = []
    spawn = m._spawn

    def counting(sid, recovered=False):
        spawned.append(sid)
        spawn(sid, recovered)
    m._spawn = counting
    setattr(m, "spawned", spawned)
    return m


def _spawned(m: Manager) -> list[str]:
    return getattr(m, "spawned")


def _created_events(m: Manager, sid: str) -> int:
    return sum(e["type"] == "session_created" for e in m.db.events(sid))


def _post(client, auth: dict, key: str | bytes | None, body: dict = BODY):
    headers = {**auth, **({idempotency.HEADER: key} if key is not None else {})}
    return client.post("/api/v1/sessions", headers=headers, json=body)


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(idempotency, "clock", lambda: now[0])
    return now


def test_a_retry_after_a_lost_response_returns_the_first_session(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        first = _post(client, auth, "order-7")
        assert first.status_code == 201 and idempotency.REPLAYED_HEADER not in first.headers
        sid = first.json()["id"]
        wait_for(lambda: client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"] == "done")

        again = _post(client, auth, "order-7", {"metadata": {"order": 7}, "prompt": "plan dinner"})  # key order
        assert again.status_code == 200 and again.headers[idempotency.REPLAYED_HEADER] == "true"
        assert again.json()["id"] == sid and again.json()["status"] == "done"  # its current view
        assert m.db.app_session_ids(app_id) == [sid]
        assert _created_events(m, sid) == 1 and _spawned(m) == [sid]

        # Only the hash of the key and a digest of the request are kept, in the App's own store.
        store = Path(m.cfg.data_dir) / "apps" / app_id / APP_STORE_FILE
        [(key_hash, digest, kept_sid)] = rows(store, "SELECT key_hash, request_digest, session_id FROM idempotency_keys")
        assert kept_sid == sid and "order-7" not in key_hash and len(digest) == 64
        assert not rows(m.cfg.db_path, "SELECT name FROM sqlite_master WHERE name = 'idempotency_keys' "
                                       "AND EXISTS (SELECT 1 FROM idempotency_keys)")


def test_concurrent_creates_under_one_key_make_one_session(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        with ThreadPoolExecutor(8) as pool:
            responses = list(pool.map(lambda _: _post(client, auth, "same"), range(8)))
        assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
        assert len({r.json()["id"] for r in responses}) == 1
        [sid] = m.db.app_session_ids(app_id)
        assert _spawned(m) == [sid] and _created_events(m, sid) == 1


def test_the_key_row_commits_with_the_session_and_a_second_insert_rolls_back(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, _ = _key(client, "shop", "sessions")
        app = m.db.get_api_key(app_id)
        record = idempotency.new_record(app_id, "k1", BODY)

        async def create() -> dict:
            return m.create("plan dinner", app=app, idempotency=record)
        first = client.portal.call(create)
        # A second create that got past every check (another process, say) cannot commit a second session.
        with pytest.raises(sqlite3.IntegrityError):
            client.portal.call(create)
        assert m.db.app_session_ids(app_id) == [first["id"]] and _spawned(m) == [first["id"]]


def test_a_crash_after_the_commit_but_before_the_response_replays_after_a_restart(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    m = _manager(cfg)
    client = TestClient(create_app(m), raise_server_exceptions=False)
    with client:
        app_id, auth = _key(client, "shop", "sessions")

        def lost(*_):
            raise RuntimeError("connection dropped")
        with monkeypatch.context() as patch:
            patch.setattr(apps, "view", lost)
            assert _post(client, auth, "order-7").status_code == 500
        [sid] = m.db.app_session_ids(app_id)
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")

    again = _manager(cfg)
    client = TestClient(create_app(again))
    with client:
        replay = _post(client, auth, "order-7")
        assert replay.status_code == 200 and replay.json()["id"] == sid
        assert again.db.app_session_ids(app_id) == [sid] and _spawned(again) == []


def test_a_different_request_under_a_key_conflicts_and_apps_never_share_keys(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        a_id, a = _key(client, "app-a", "sessions")
        b_id, b = _key(client, "app-b", "sessions")
        sid = _post(client, a, "k").json()["id"]
        for changed in ({**BODY, "prompt": "plan lunch"}, {**BODY, "end_user": "person-2", "backend": "claude"},
                        {**BODY, "metadata": {"order": 8}}, {**BODY, "context": [{"title": "t", "content": "c"}]}):
            r = _post(client, a, "k", changed)
            assert r.status_code == 409 and r.json()["error"]["code"] == idempotency.CONFLICT
            assert "plan" not in r.text and sid not in r.text
        # The same key in another App is a different key: its own session, nothing of App A's.
        other = _post(client, b, "k")
        assert other.status_code == 201 and other.json()["id"] != sid
        assert _post(client, b, "k").json()["id"] == other.json()["id"]
        assert m.db.app_session_ids(a_id) == [sid] and m.db.app_session_ids(b_id) == [other.json()["id"]]


def test_failed_requests_leave_the_key_free_and_unkeyed_creates_are_unchanged(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        assert _post(client, auth, "k", {"prompt": "  "}).status_code == 400          # refused by the manager
        assert _post(client, auth, "k", {"prompt": "x", "project": "nope"}).status_code == 400
        assert _post(client, auth, "k", {"metadata": {}}).status_code == 422           # refused by validation
        assert m.db.app_session_ids(app_id) == []
        assert _post(client, auth, "k", {"prompt": "x", "project": "nope"}).status_code == 400  # not a replay
        assert _post(client, auth, "k").status_code == 201

        # Without a key every request is a new session, as before.
        ids = {_post(client, auth, None).json()["id"] for _ in range(2)}
        assert len(ids) == 2 and len(m.db.app_session_ids(app_id)) == 3

        for bad in ("", "x" * 129, "has space", "naïve".encode("latin-1"), "a/b"):
            r = _post(client, auth, bad)
            assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_idempotency_key"
        # v1 is for App tokens: the owner's own token is told so rather than silently ignored.
        _, owner = _key(client, "control-center", "admin", "sessions", kind="owner")
        r = _post(client, owner, "k")
        assert r.status_code == 400 and r.json()["error"]["code"] == "idempotency_unsupported"


def test_a_revoked_token_fails_on_replay(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        assert _post(client, auth, "k").status_code == 201
        assert client.delete(f"/keys/{app_id}").status_code == 204
        assert _post(client, auth, "k").status_code == 401


def test_a_key_is_protected_for_24_hours_then_reusable(tmp_path, clock):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        sid = _post(client, auth, "k").json()["id"]
        clock[0] += idempotency.WINDOW_SECONDS - 1
        assert _post(client, auth, "k").status_code == 200
        assert _post(client, auth, "k", {"prompt": "other"}).status_code == 409
        clock[0] += 1
        fresh = _post(client, auth, "k", {"prompt": "other"})
        assert fresh.status_code == 201 and fresh.json()["id"] != sid
        # The expired row made way for the new one.
        store = Path(m.cfg.data_dir) / "apps" / app_id / APP_STORE_FILE
        assert rows(store, "SELECT session_id FROM idempotency_keys") == [(fresh.json()["id"],)]


def test_a_retry_after_the_app_erased_the_session_is_gone_until_the_key_expires(tmp_path, clock):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        sid = _post(client, auth, "k").json()["id"]
        wait_for(lambda: client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"] == "done")
        assert client.delete(f"/api/v1/sessions/{sid}", headers=auth).status_code == 204
        r = _post(client, auth, "k")
        assert r.status_code == 410 and r.json()["error"]["code"] == idempotency.ERASED
        assert m.db.app_session_ids(app_id) == [] and _spawned(m) == [sid]
        # The tombstone keeps the key's hash, the digest and the expiry: not even the session id.
        store = Path(m.cfg.data_dir) / "apps" / app_id / APP_STORE_FILE
        assert rows(store, "SELECT session_id FROM idempotency_keys") == [("",)]
        assert _post(client, auth, "k", {"prompt": "other"}).status_code == 409
        clock[0] += idempotency.WINDOW_SECONDS
        assert _post(client, auth, "k").status_code == 201


def test_erasing_the_apps_store_removes_its_keys(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        sid = _post(client, auth, "k").json()["id"]
        wait_for(lambda: client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"] == "done")
        store = Path(m.cfg.data_dir) / "apps" / app_id / APP_STORE_FILE
        assert rows(store, "SELECT COUNT(*) FROM idempotency_keys") == [(1,)]
        m.db.drop_app(app_id)
        assert not store.exists()


def test_the_openapi_document_describes_the_header_and_both_create_statuses(tmp_path):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        op = client.get("/openapi.json").json()["paths"]["/api/v1/sessions"]["post"]
    [header] = [p for p in op["parameters"] if p["name"] == idempotency.HEADER]
    assert header["in"] == "header" and not header.get("required")
    assert {"200", "201", "409", "410"} <= set(op["responses"])
    assert idempotency.REPLAYED_HEADER in op["responses"]["200"]["headers"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == \
        op["responses"]["201"]["content"]["application/json"]["schema"]


def test_replayed_session_raises_harness_errors_without_a_store_write():
    class Store:
        row: dict | None = None

        def find_idempotency_key(self, _key_hash, _now):
            return self.row

        def get_session(self, sid):
            return {"id": sid} if sid == "s1" else None
    store, record = Store(), idempotency.new_record("app", "k", BODY)
    assert idempotency.replayed_session(store, record) is None
    store.row = {**record, "session_id": "s1"}
    assert idempotency.replayed_session(store, record) == "s1"
    store.row = {**record, "session_id": "s1", "request_digest": "other"}
    with pytest.raises(HarnessError) as e:
        idempotency.replayed_session(store, record)
    assert e.value.status == 409
    store.row = {**record, "session_id": ""}
    with pytest.raises(HarnessError) as e:
        idempotency.replayed_session(store, record)
    assert e.value.status == 410


def test_the_sdk_forwards_the_key_and_a_lost_response_is_recovered_by_retrying(tmp_path, monkeypatch):
    from sdk.harness_client import Harness, HarnessError as SdkError, RunResult

    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        client.headers.update(auth)
        sdk = Harness("http://testserver")
        sdk.client.close()
        sdk.client = client
        first = sdk.create_session("plan dinner", idempotency_key="order-7")
        # The response was lost: the App sends the same call again and gets the same session back.
        assert sdk.create_session("plan dinner", idempotency_key="order-7")["id"] == first["id"]
        with pytest.raises(SdkError) as e:
            sdk.create_session("plan lunch", idempotency_key="order-7")
        assert e.value.status == 409 and e.value.code == idempotency.CONFLICT
        monkeypatch.setattr(sdk, "_drive", lambda sid, tools, result, on_event: result)  # no event stream here
        result = sdk.run("plan lunch", idempotency_key="order-8")
        assert isinstance(result, RunResult)
        assert sdk.run("plan lunch", idempotency_key="order-8").session["id"] == result.session["id"]
        assert sdk.create_session("plan dinner")["id"] != first["id"]  # no key: a new session, nothing retried
        assert len(m.db.app_session_ids(app_id)) == 3


def test_a_key_that_expires_while_the_request_is_checked_is_reused_not_a_500(tmp_path, clock, monkeypatch):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        first = _post(client, auth, "k").json()["id"]
        expiry = clock[0] + idempotency.WINDOW_SECONDS
        # The retry's record is made a second before the key expires; the looks that follow run after it has.
        ticks = iter([expiry - 1])
        monkeypatch.setattr(idempotency, "clock", lambda: next(ticks, expiry))
        r = _post(client, auth, "k")
        assert r.status_code == 201 and r.json()["id"] != first
        assert len(m.db.app_session_ids(app_id)) == 2


def test_insert_uses_the_current_clock_for_cleanup_and_the_new_protection_window(tmp_path, clock):
    m = _manager(make_cfg(tmp_path))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        first = _post(client, auth, "k").json()["id"]
        clock[0] += idempotency.WINDOW_SECONDS - 1
        record = idempotency.new_record(app_id, "k", BODY)
        # The handler prepared this record before expiry, then waited before the durable insert.
        clock[0] += 61

        async def create():
            return m.create("plan dinner", app=m.db.get_api_key(app_id), idempotency=record)

        fresh = client.portal.call(create)
        assert fresh["id"] != first
        kept = m.db.for_app(app_id).find_idempotency_key(record["key_hash"], clock[0])
        assert kept["session_id"] == fresh["id"]
        assert kept["created_at"] == clock[0]
        assert kept["expires_at"] == clock[0] + idempotency.WINDOW_SECONDS
        assert set(m.db.app_session_ids(app_id)) == {first, fresh["id"]}
        assert _spawned(m) == [first, fresh["id"]]
