"""Per-App data stores (#330 stage a): each App's sessions live only in `<data_dir>/apps/<app_id>/harness.sqlite3`."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.app_stores import APP_STORE_FILE, SESSION_TABLES, SessionStores
from harness.db import Database
from harness.llm import Completion
from harness.manager import Manager
from harness.policy import TOOLS_ONLY

from test_daemon import Script, call, make_cfg
from test_phase6 import wait_for

BALANCE = {"name": "get_balance", "description": "Balance of one account",
           "parameters": {"type": "object", "properties": {"account": {"type": "string"}},
                          "required": ["account"]}}
RULES = [{"tool": "write_file", "path": "secret/*", "action": "ask", "reason": "sensitive"}]


def rows(path: Path, sql: str, *params) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def session_ids(path: Path) -> set[str]:
    return {r[0] for r in rows(path, "SELECT id FROM sessions")}


def rows_of(path: Path, sids: set[str]) -> dict[str, int]:
    """How many rows of these sessions each session-tied table (and the search index) holds."""
    marks = ",".join("?" * len(sids))
    out = {t: rows(path, f"SELECT COUNT(*) FROM {t} WHERE session_id IN ({marks})", *sids)[0][0]
           for t in (*SESSION_TABLES, "search_index")}
    out["sessions"] = rows(path, f"SELECT COUNT(*) FROM sessions WHERE id IN ({marks})", *sids)[0][0]
    return out


def test_two_apps_sessions_are_written_only_to_their_own_stores(tmp_path):
    steps = [Completion(tool_calls=[call("write_file", 0, path="secret/a.txt", content="x"),
                                    call("get_balance", 1, account="checking")]),
             Completion(content="Checking has $120."),
             Completion(content="Still $120.")]
    cfg = make_cfg(tmp_path, rules=RULES)
    cfg.search.enabled = True
    m = Manager(cfg, chat=Script(steps))
    client = TestClient(create_app(m))
    with client:
        apps = {}
        for name, word in (("app-a", "pelican"), ("app-b", "walrus")):
            key = client.post("/keys", json={"name": name, "kind": "app",
                                             "scopes": ["sessions", "approvals"]}).json()
            auth = {"Authorization": f"Bearer {key['key']}"}
            agent = client.post("/api/v1/sessions", headers=auth, json={
                "prompt": f"check the {word} account", "project": "guarded", "tools": [BALANCE]})
            tools_only = client.post("/api/v1/sessions", headers=auth, json={
                "prompt": f"{word} balance please", "tools_only": True, "tools": [BALANCE]})
            assert agent.status_code == 201 and tools_only.status_code == 201, (agent.text, tools_only.text)
            apps[name] = {"id": key["id"], "auth": auth, "word": word,
                          "sids": {agent.json()["id"], tools_only.json()["id"]}}
        owner_sid = client.post("/sessions", json={"prompt": "owner task", "project": "scratch"}).json()["id"]

        def drive(app, sid) -> bool:
            """Approve what waits for approval, answer the App's tool calls; True once the run is done."""
            auth = app["auth"]
            for a in client.get(f"/api/v1/sessions/{sid}/approvals", headers=auth).json():
                client.post(f"/api/v1/sessions/{sid}/approvals/{a['id']}", headers=auth,
                            json={"decision": "approve"})
            for c in client.get(f"/api/v1/sessions/{sid}/tool_calls", headers=auth).json():
                client.post(f"/api/v1/sessions/{sid}/tool_calls/{c['call_id']}", headers=auth,
                            json={"output": "$120"})
            return client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"] == "done"

        for app in apps.values():
            for sid in app["sids"]:
                wait_for(lambda app=app, sid=sid: drive(app, sid), timeout=60)
                assert client.post(f"/api/v1/sessions/{sid}/messages", headers=app["auth"],
                                   json={"content": "And now?"}).status_code == 200
        for app in apps.values():
            for sid in app["sids"]:
                wait_for(lambda app=app, sid=sid: client.get(f"/api/v1/sessions/{sid}", headers=app["auth"])
                         .json()["answer"] == "Still $120.", timeout=60)
        wait_for(lambda: client.get(f"/sessions/{owner_sid}").json()["status"] == "done")

        # Each App sees its own sessions through the API, search included, and never the other App's.
        for name, app in apps.items():
            other = apps["app-b" if name == "app-a" else "app-a"]
            listed = {s["id"] for s in client.get("/api/v1/sessions", headers=app["auth"]).json()}
            assert listed == app["sids"]
            found = client.get(f"/api/v1/search?q={app['word']}", headers=app["auth"]).json()
            assert {r["id"] for r in found["results"]} & app["sids"]
            assert not client.get(f"/api/v1/search?q={other['word']}", headers=app["auth"]).json()["results"]
            for sid in other["sids"]:
                assert client.get(f"/api/v1/sessions/{sid}", headers=app["auth"]).status_code == 404

    m.db.close()
    main = Path(cfg.db_path)
    all_app_sids = apps["app-a"]["sids"] | apps["app-b"]["sids"]
    assert session_ids(main) == {owner_sid}
    assert not any(rows_of(main, all_app_sids).values())
    assert rows_of(main, {owner_sid})["events"] > 0                       # the owner's session is where it was
    for name, app in apps.items():
        store = Path(cfg.data_dir) / "apps" / app["id"] / APP_STORE_FILE
        assert session_ids(store) == app["sids"]
        held = rows_of(store, app["sids"])
        assert held["events"] and held["approvals"] and held["app_tool_calls"] and held["search_index"]
        kinds = {r[0] for r in rows(store, "SELECT kind FROM sessions")}
        assert kinds == {"agent", TOOLS_ONLY}
        assert {r[0] for r in rows(store, "SELECT status FROM approvals")} == {"approved"}


def _session(sid: str, app_id: str = "", **extra) -> dict:
    now = time.time()
    return {"id": sid, "project": "scratch", "target": "tower", "model": "fake", "title": sid, "status": "done",
            "workspace": "", "created_at": now, "updated_at": now, "context": [], "app_id": app_id,
            "owner_id": "owner", **extra}


def _fill(db: Database, sid: str, word: str) -> None:
    db.insert_event(sid, "user_message", {"content": f"find the {word}"})
    db.insert_approval({"id": f"ap-{sid}", "session_id": sid, "tool_call_id": "c1", "tool": "write_file",
                        "args": {"path": "x"}, "reason": "ask"})
    db.insert_app_tool_call(sid, "c2", "get_balance", {"account": "checking"})
    db.put_artifact(sid, "h1", "artifact body")


def test_startup_migration_moves_app_sessions_with_a_backup_once(tmp_path):
    path = tmp_path / "harness.sqlite3"
    apps_dir = tmp_path / "apps"
    db = Database(path)
    for sid, app_id, word in (("own1", "", "heron"), ("a1", "k-aaaa", "pelican"), ("a2", "k-aaaa", "osprey"),
                              ("b1", "k-bbbb", "walrus")):
        db.insert_session(_session(sid, app_id))
        _fill(db, sid, word)
    db.record_usage("fake", "a1", "k-aaaa", 10, 5, 0.0, "subscription")  # metadata: stays in the main store
    db.close()

    stores = SessionStores(Database(path), apps_dir)
    backups = sorted((tmp_path / "pre-migration").glob("harness-app-stores-*.sqlite3"))
    assert len(backups) == 1 and session_ids(backups[0]) == {"own1", "a1", "a2", "b1"}
    assert stores.get_session("a1")["app_id"] == "k-aaaa"
    assert [e["data"]["content"] for e in stores.events("b1")] == ["find the walrus"]
    assert stores.get_approval("ap-a2")["session_id"] == "a2"
    assert stores.full_artifact("a1", "h1") == "artifact body"
    assert stores.get_app_tool_call("b1", "c2")["name"] == "get_balance"
    found = stores.search_events("osprey", app_id="k-aaaa")
    assert [r["session_id"] for r in found] == ["a2"]
    stores.close()

    assert session_ids(path) == {"own1"}
    assert not any(rows_of(path, {"a1", "a2", "b1"}).values())
    assert all(rows_of(path, {"own1"})[t] for t in ("events", "approvals", "app_tool_calls", "artifacts"))
    assert rows(path, "SELECT session_id FROM usage") == [("a1",)]
    assert session_ids(apps_dir / "k-aaaa" / APP_STORE_FILE) == {"a1", "a2"}
    assert session_ids(apps_dir / "k-bbbb" / APP_STORE_FILE) == {"b1"}

    # A second start finds nothing to move: no new backup, nothing changes.
    before = {p: rows_of(p, {"a1", "a2", "b1", "own1"}) for p in
              (path, apps_dir / "k-aaaa" / APP_STORE_FILE, apps_dir / "k-bbbb" / APP_STORE_FILE)}
    again = SessionStores(Database(path), apps_dir)
    assert again.migrate_app_sessions() is None
    assert again.get_session("b1")["app_id"] == "k-bbbb" and again.get_session("own1")["app_id"] == ""
    again.close()
    assert len(list((tmp_path / "pre-migration").glob("harness-app-stores-*.sqlite3"))) == 1
    assert {p: rows_of(p, {"a1", "a2", "b1", "own1"}) for p in before} == before


def test_an_idle_app_store_closes_and_reopens_on_use(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps", idle_seconds=0.2)
    stores.insert_session(_session("a1", "k-aaaa", status="queued"))
    assert stores.open_apps() == ["k-aaaa"]
    first = stores._open["k-aaaa"].db
    assert first._writer.is_alive()

    wait_for(lambda: stores.open_apps() == [], timeout=10)                  # the reaper closed it
    wait_for(lambda: not first._writer.is_alive() and first._readers == [], timeout=10)  # closed after let go
    assert not first._writer.is_alive() and first._readers == []

    stores.update_session("a1", status="running")                           # first use reopens it
    assert stores.open_apps() == ["k-aaaa"]
    assert stores._open["k-aaaa"].db is not first
    assert stores.get_session("a1")["status"] == "running"
    assert [s["id"] for s in stores.sessions_with_status("running")] == ["a1"]
    assert stores.count_sessions("owner", "running") == 1
    stores.close()


def test_a_store_in_use_is_not_closed(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps", idle_seconds=60)
    stores.insert_session(_session("a1", "k-aaaa"))
    assert stores.close_idle() == []                                        # used just now
    with stores._using("k-aaaa"):
        assert stores.close_idle(now=time.monotonic() + 61) == []           # busy: kept
    assert stores.close_idle(now=time.monotonic() + 61) == ["k-aaaa"]
    assert stores.events("a1") == []                                        # reopened on use
    stores.close()


def test_one_writer_per_store_and_no_write_across_stores(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    stores.insert_session(_session("own1"))
    stores.insert_session(_session("a1", "k-aaaa"))
    stores.insert_session(_session("b1", "k-bbbb"))
    writers = {sid: stores.for_session(sid).write(threading.current_thread) for sid in ("own1", "a1", "b1")}
    assert len({id(t) for t in writers.values()}) == 3
    assert all(t is not threading.current_thread() for t in writers.values())

    seen = []

    def app_tx():  # an App's transaction may use the main store (its writer is waited for)
        stores.update_session("a1", title="renamed")
        stores.set_meta("probe", "1")
        seen.append(stores.in_transaction())
        stores.after_commit(lambda: seen.append("committed"))
    stores.for_session("a1").write(app_tx)
    assert stores.get_session("a1")["title"] == "renamed" and stores.get_meta("probe") == "1"
    assert seen == [True, "committed"]

    with pytest.raises(RuntimeError, match="another store's transaction"):
        stores.main.write(lambda: stores.update_session("a1", title="from main"))
    with pytest.raises(RuntimeError, match="another store's transaction"):
        stores.for_session("b1").write(lambda: stores.update_session("a1", title="from b"))
    assert stores.get_session("a1")["title"] == "renamed"

    async def reads_off_the_loop():
        return await stores.aio.get_session("a1"), await stores.for_session("a1").aio.events("a1")
    s, events = asyncio.run(reads_off_the_loop())
    assert s["id"] == "a1" and events == []
    stores.close()


def test_session_ids_stay_unique_across_stores(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    stores.insert_session(_session("same"))
    with pytest.raises(sqlite3.IntegrityError):
        stores.insert_session(_session("same", "k-aaaa"))
    stores.insert_session(_session("a1", "k-aaaa"))
    with pytest.raises(sqlite3.IntegrityError):
        stores.insert_session(_session("a1", "k-bbbb"))
    with pytest.raises(sqlite3.IntegrityError):
        stores.insert_session(_session("a1"))
    assert stores.get_session("a1")["app_id"] == "k-aaaa"
    assert stores.find_session_ids("a") == ["a1"]
    stores.close()
    assert session_ids(tmp_path / "apps" / "k-aaaa" / APP_STORE_FILE) == {"a1"}


def test_an_app_session_create_that_rolls_back_leaves_no_trace_in_the_index(tmp_path, monkeypatch):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    real = Database.insert_event

    def failing(self, sid, type_, data):
        if type_ == "session_created":
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, sid, type_, data)
    monkeypatch.setattr(Database, "insert_event", failing)
    seen = []

    def insert_created(status="queued"):  # as Manager._insert_created: the session, then its events
        stores.insert_session(_session("a1", "k-aaaa", status=status))
        seen.append((stores.app_of("a1"), stores.count_sessions("owner", status)))
        stores.insert_event("a1", "session_created", {})
    with pytest.raises(sqlite3.OperationalError):
        stores.for_app("k-aaaa").write(insert_created)
    assert seen == [("k-aaaa", 0)]                            # routed inside its transaction, not counted yet
    assert stores.app_of("a1") == "" and stores.get_session("a1") is None
    assert stores.count_sessions("owner", "queued") == 0
    assert stores.sessions_with_status("queued") == [] and stores.find_session_ids("a") == []

    monkeypatch.setattr(Database, "insert_event", real)
    stores.for_app("k-aaaa").write(insert_created)            # the same id again
    assert stores.get_session("a1")["status"] == "queued"
    assert [e["type"] for e in stores.events("a1")] == ["session_created"]
    assert stores.count_sessions("owner", "queued") == 1
    stores.close()
    assert session_ids(tmp_path / "apps" / "k-aaaa" / APP_STORE_FILE) == {"a1"}


def test_a_status_write_that_rolls_back_leaves_the_index_as_committed(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    stores.insert_session(_session("a1", "k-aaaa", status="queued"))
    stores.insert_session(_session("a2", "k-aaaa", status="running"))
    seen = []

    def status_write(sid, status):  # as Manager._status_writer: the status, then its event
        stores.update_session(sid, status=status)
        seen.append(stores.count_sessions("owner", status))
        raise sqlite3.OperationalError("disk I/O error")
    for sid in ("a1", "a2"):
        with pytest.raises(sqlite3.OperationalError):
            stores.for_session(sid).write(status_write, sid, "done")
    assert seen == [0, 0]                                     # not ahead of the commit, even inside it
    assert {sid: stores.get_session(sid)["status"] for sid in ("a1", "a2")} == {"a1": "queued", "a2": "running"}
    assert {sid: e["status"] for sid, e in stores._indexed(lambda e: True)} == {"a1": "queued", "a2": "running"}
    assert stores.count_sessions("owner", "queued", "running") == 2 and stores.count_sessions("owner", "done") == 0
    # what startup resume asks for
    assert {s["id"] for s in stores.sessions_with_status("queued", "running", "waiting_approval")} == {"a1", "a2"}

    with pytest.raises(sqlite3.OperationalError):             # nor does a delete count before it commits
        stores.for_session("a1").write(lambda: (stores.delete_session("a1"), status_write("a2", "done")))
    assert stores.app_of("a1") == "k-aaaa" and stores.get_session("a1")["status"] == "queued"

    stores.for_session("a1").write(lambda: stores.update_session("a1", status="running"))
    assert stores.count_sessions("owner", "running") == 2
    stores.delete_session("a2")
    assert stores.app_of("a2") == "" and stores.count_sessions("owner", "running") == 1
    stores.close()


def test_an_app_approval_insert_that_rolls_back_is_not_routed(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    stores.insert_session(_session("a1", "k-aaaa", status="running"))
    approval = {"id": "ap1", "session_id": "a1", "tool_call_id": "c1", "tool": "write_file", "args": {}, "reason": "", "status": "pending",
                "created_at": time.time()}

    def persist_ask():
        stores.insert_approval(dict(approval))
        assert stores.get_approval("ap1") is not None         # routed inside its transaction
        raise sqlite3.OperationalError("disk I/O error")
    with pytest.raises(sqlite3.OperationalError):
        stores.for_session("a1").write(persist_ask)
    assert "ap1" not in stores._approvals and stores._tokens == {}
    stores.insert_approval(dict(approval))
    assert stores.get_approval("ap1")["session_id"] == "a1" and stores.pending_approvals("a1")
    stores.close()
