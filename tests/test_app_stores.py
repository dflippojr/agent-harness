"""Per-App data stores (#330): each App's sessions live only in `<data_dir>/apps/<app_id>/harness.sqlite3` (stage a),
and the owner sees metadata about them, never their content (stage b)."""

from __future__ import annotations

import asyncio
import json
import logging
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
    main, web = Path(cfg.db_path), Path(cfg.data_dir) / "apps" / "app-web" / APP_STORE_FILE
    all_app_sids = apps["app-a"]["sids"] | apps["app-b"]["sids"]
    assert session_ids(main) == set()                                     # the main store holds no sessions (#330 c)
    assert session_ids(web) == {owner_sid}
    assert not any(rows_of(web, all_app_sids).values())
    assert rows_of(web, {owner_sid})["events"] > 0                        # the owner's session is in Web's store
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
    web_backups = sorted((tmp_path / "pre-migration").glob("harness-web-store-*.sqlite3"))
    assert len(web_backups) == 1 and session_ids(web_backups[0]) == {"own1"}  # the App sessions had moved
    assert stores.get_session("a1")["app_id"] == "k-aaaa"
    assert [e["data"]["content"] for e in stores.events("b1")] == ["find the walrus"]
    assert stores.get_approval("ap-a2")["session_id"] == "a2"
    assert stores.full_artifact("a1", "h1") == "artifact body"
    assert stores.get_app_tool_call("b1", "c2")["name"] == "get_balance"
    found = stores.search_events("osprey", app_id="k-aaaa")
    assert [r["session_id"] for r in found] == ["a2"]
    stores.close()

    web = apps_dir / "app-web" / APP_STORE_FILE
    assert session_ids(path) == set() and session_ids(web) == {"own1"}  # the owner's moved into Web's store
    assert not any(rows_of(path, {"a1", "a2", "b1", "own1"}).values())
    assert not any(rows_of(web, {"a1", "a2", "b1"}).values())
    assert all(rows_of(web, {"own1"})[t] for t in ("events", "approvals", "app_tool_calls", "artifacts"))
    assert rows(path, "SELECT session_id FROM usage") == [("a1",)]
    assert session_ids(apps_dir / "k-aaaa" / APP_STORE_FILE) == {"a1", "a2"}
    assert session_ids(apps_dir / "k-bbbb" / APP_STORE_FILE) == {"b1"}

    # A second start finds nothing to move: no new backup, nothing changes.
    before = {p: rows_of(p, {"a1", "a2", "b1", "own1"}) for p in
              (path, web, apps_dir / "k-aaaa" / APP_STORE_FILE, apps_dir / "k-bbbb" / APP_STORE_FILE)}
    again = SessionStores(Database(path), apps_dir)
    assert again.migrate_app_sessions() is None
    assert again.get_session("b1")["app_id"] == "k-bbbb" and again.get_session("own1")["app_id"] == ""
    again.close()
    assert len(list((tmp_path / "pre-migration").glob("harness-app-stores-*.sqlite3"))) == 1
    assert len(list((tmp_path / "pre-migration").glob("harness-web-store-*.sqlite3"))) == 1
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
    approval = {"id": "ap1", "session_id": "a1", "tool_call_id": "c1", "tool": "write_file", "args": {},
                "reason": "", "status": "pending", "created_at": time.time()}

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


# stage (b): the owner sees metadata only; backups; logs ------------------------------------------------------------
def _key(client, name: str, *scopes: str, kind: str = "app") -> tuple[str, dict]:
    key = client.post("/keys", json={"name": name, "kind": kind, "scopes": list(scopes)}).json()
    return key["id"], {"Authorization": f"Bearer {key['key']}"}


def test_the_owner_and_sessions_all_never_reach_another_apps_sessions(tmp_path):
    from harness_modules.skills import service as skills
    from harness.fileops import ToolError
    from harness_modules.search.service import SessionSearch

    cfg = make_cfg(tmp_path)
    cfg.search.enabled = True
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        a_id, a = _key(client, "app-a", "sessions", "approvals")
        b_id, b = _key(client, "app-b", "sessions", "sessions:all")
        owner_key_id, owner = _key(client, "control-center", "admin", kind="owner")
        a_sid = client.post("/api/v1/sessions", headers=a, json={"prompt": "check the pelican ledger"}).json()["id"]
        b_sid = client.post("/api/v1/sessions", headers=b, json={"prompt": "check the walrus ledger"}).json()["id"]
        owner_sid = client.post("/sessions", json={"prompt": "check the heron ledger"}).json()["id"]
        for sid in (a_sid, b_sid, owner_sid):
            wait_for(lambda sid=sid: m.db.get_session(sid)["status"] == "done")
        tools = SessionSearch(m.db)

        # Nothing the owner asks for opens an App's store.
        opened: list[str] = []
        acquire, aacquire = m.db._acquire, m.db._aacquire
        m.db._acquire = lambda app_id: opened.append(app_id) or acquire(app_id)
        m.db._aacquire = lambda app_id: opened.append(app_id) or aacquire(app_id)
        assert {s["id"] for s in client.get("/sessions").json()} == {owner_sid}
        for path in ("/sessions/{}", "/sessions/{}/transcript", "/sessions/{}/approvals",
                     "/sessions/{}/events?follow=false", "/sessions/{}/metrics"):
            for ref in (a_sid, a_sid[:6]):
                assert client.get(path.format(ref)).status_code == 404, path
        assert client.post(f"/sessions/{a_sid}/messages", json={"content": "x"}).status_code == 404
        assert not client.get("/search?q=pelican").json()["results"]
        assert [r["id"] for r in client.get("/search?q=ledger").json()["results"]] == [owner_sid]
        assert {s["id"] for s in client.get("/api/v1/sessions", headers=owner).json()} == {owner_sid}
        assert client.get(f"/api/v1/sessions/{a_sid}", headers=owner).status_code == 404
        assert not client.get("/api/v1/search?q=pelican", headers=owner).json()["results"]
        assert tools.session_search("pelican", _session=owner_sid).startswith("No earlier sessions")
        with pytest.raises(ToolError):
            tools.session_read(a_sid, _session=owner_sid)
        keys = {k["id"]: k for k in client.get("/keys").json()}
        m.db._acquire, m.db._aacquire = acquire, aacquire
        assert opened == []

        # The owner gets metadata instead.
        assert keys[a_id]["store"]["sessions"] == {"done": 1}
        assert keys[b_id]["store"]["sessions"] == {"done": 1}
        assert "store" not in keys[owner_key_id]
        assert not skills.session_eligible(m.db.get_session(a_sid))

        # sessions:all adds the owner's sessions to an App's own, never another App's.
        assert {s["id"] for s in client.get("/api/v1/sessions", headers=b).json()} == {owner_sid, b_sid}
        assert client.get(f"/api/v1/sessions/{owner_sid}", headers=b).status_code == 200
        assert client.get(f"/api/v1/sessions/{a_sid}", headers=b).status_code == 404
        assert not client.get("/api/v1/search?q=pelican", headers=b).json()["results"]
        found = client.get("/api/v1/search?q=ledger", headers=b).json()["results"]
        assert {r["id"] for r in found} == {owner_sid, b_sid}
        assert {r["id"] for r in client.get("/api/v1/search?q=ledger", headers=a).json()["results"]} == {a_sid}
        assert "heron" in tools.session_read(owner_sid, _session=b_sid)
        with pytest.raises(ToolError):
            tools.session_read(a_sid, _session=b_sid)
        assert tools.session_search("pelican", _session=b_sid).startswith("No earlier sessions")


def test_lists_searches_and_lookups_reach_only_the_app_they_name(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    for sid, app_id in (("abc1", ""), ("abc2", "k-aaaa"), ("abc3", "k-bbbb")):
        stores.insert_session(_session(sid, app_id, status="running"))
        _fill(stores, sid, "ledger")
    owner, app_a = stores.scope(""), stores.scope("k-aaaa")

    assert owner.find_session_ids("abc") == ["abc1"]
    assert sorted(app_a.find_session_ids("abc")) == ["abc1", "abc2"]
    assert sorted(stores.find_session_ids("abc")) == ["abc1", "abc2", "abc3"]  # the daemon's own lookups
    assert {s["id"] for s in owner.list_sessions()} == {"abc1"}
    assert {s["id"] for s in app_a.list_sessions()} == {"abc1", "abc2"}
    assert {r["session_id"] for r in owner.search_events("ledger")} == {"abc1"}
    assert {r["session_id"] for r in app_a.search_events("ledger")} == {"abc1", "abc2"}
    assert {r["session_id"] for r in stores.search_events("ledger", app_id="k-bbbb")} == {"abc3"}
    assert {a["id"] for a in owner.pending_approvals()} == {"ap-abc1"}
    assert {a["id"] for a in app_a.pending_approvals()} == {"ap-abc1", "ap-abc2"}
    assert {s["id"] for s in stores.sessions_with_status("running")} == {"abc1", "abc2", "abc3"}  # sweeps
    stores.close()


def test_app_metadata_counts_sessions_usage_and_errors_without_their_content(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    app, _ = stores.main.create_api_key("shop", "sessions", "app")
    stores.main.create_api_key("control-center", "admin", "owner")
    assert stores.app_metadata(app["id"]) == {"sessions": {}, "usage": {"requests": 0, "prompt_tokens": 0,
                                              "completion_tokens": 0, "cost_usd": 0}, "errors": 0,
                                              "last_error": "", "last_error_at": None}
    for sid in ("s1", "s2", "s3"):
        stores.insert_session(_session(sid, app["id"], status="running"))
    stores.update_session("s1", status="done")
    stores.update_session("s2", status="failed", stop_reason="quota_exceeded: 900 MB of checking > 500 MB")
    stores.main.record_usage("hosted", "s1", app["id"], 10, 5, 0.25, "api")

    meta = stores.app_metadata(app["id"])
    assert meta["sessions"] == {"done": 1, "failed": 1, "running": 1}
    assert meta["usage"] == {"requests": 1, "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.25}
    assert meta["errors"] == 1 and meta["last_error"] == "quota_exceeded" and meta["last_error_at"]
    assert "checking" not in json.dumps(meta)
    keys = {k["kind"]: k for k in stores.list_api_keys()}
    assert keys["app"]["store"] == meta and "store" not in keys["owner"]
    stores.close()


def test_owner_totals_count_the_main_store_and_apps_only_as_metadata(tmp_path):
    from harness.metrics import render

    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    for sid, app_id in (("own1", ""), ("a1", "k-aaaa"), ("a2", "k-aaaa")):
        m.db.insert_session(_session(sid, app_id))
        m.db.insert_smart_review(f"sr-{sid}", sid, "", {"outcome": "auto_approved", "tool": "write_file"})
    assert 'harness_sessions{status="done"} 1' in render(m)
    assert m.db.smart_review_stats()["attempts"] == 1
    assert m.db.app_metadata("k-aaaa")["sessions"] == {"done": 2}
    m.db.close()


def test_the_nightly_backup_holds_one_file_per_app_store(tmp_path):
    from harness.config import BackupConfig

    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=False, dir=str(tmp_path / "backups"), keep_days=14)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="ok")]))
        await m.start(maintenance=False)
        for sid, app_id in (("own1", ""), ("a1", "k-aaaa"), ("a2", "k-aaaa"), ("b1", "k-bbbb")):
            m.db.insert_session(_session(sid, app_id))
        (Path(cfg.data_dir) / "apps" / "not an app").mkdir()
        result = await m.modules.get("backup").service.backup()
        await m.stop()
        return result

    result = asyncio.run(body())
    dest = Path(result["path"])
    assert result["app_stores"] == 3  # Web's store too (#330 decision 4)
    assert sorted(p.name for p in (dest / "apps").iterdir()) == ["app-web.sqlite3", "k-aaaa.sqlite3",
                                                                 "k-bbbb.sqlite3"]
    assert session_ids(dest / "apps" / "k-aaaa.sqlite3") == {"a1", "a2"}
    assert session_ids(dest / "apps" / "k-bbbb.sqlite3") == {"b1"}
    assert session_ids(dest / "apps" / "app-web.sqlite3") == {"own1"}
    assert session_ids(dest / "harness.sqlite3") == set()


def test_app_tool_arguments_and_results_never_reach_logs_spans_or_the_audit_log(tmp_path, caplog):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from harness import telemetry
    from harness.config import TelemetryConfig

    arg, result = "zq-app-arg-5c1e07", "zq-app-result-9d2741"
    cfg = make_cfg(tmp_path)
    cfg.telemetry = TelemetryConfig(otlp_endpoint="http://127.0.0.1:4318/v1/traces")
    m = Manager(cfg, chat=Script([Completion(tool_calls=[call("get_balance", 0, account=arg)]),
                                  Completion(content="Balance noted.")]))
    exporter = InMemorySpanExporter()
    telemetry.configure(cfg.telemetry, processor=SimpleSpanProcessor(exporter))
    caplog.set_level(logging.DEBUG)
    client = TestClient(create_app(m))
    try:
        with client:
            _, auth = _key(client, "bank", "sessions")
            sid = client.post("/api/v1/sessions", headers=auth, json={
                "prompt": "what is the balance", "tools": [BALANCE]}).json()["id"]

            def answer() -> bool:
                for c in client.get(f"/api/v1/sessions/{sid}/tool_calls", headers=auth).json():
                    client.post(f"/api/v1/sessions/{sid}/tool_calls/{c['call_id']}", headers=auth,
                                json={"output": f"balance {result}"})
                return m.db.get_session(sid)["status"] == "done"
            wait_for(answer, timeout=60)
            held = json.dumps([e["data"] for e in m.db.events(sid)])
            audit = m.db.main.read(lambda: [tuple(r) for r in
                                            m.db.main.conn.execute("SELECT * FROM account_audit").fetchall()])
    finally:
        telemetry.configure(None)

    assert arg in held and result in held  # the App's own store has them
    spans = exporter.get_finished_spans()
    assert any(sp.name == "execute_tool" and sp.attributes.get("gen_ai.tool.name") == "get_balance" for sp in spans)
    for sp in spans:
        for key, value in sp.attributes.items():
            assert arg not in str(value) and result not in str(value), (sp.name, key)
        assert arg not in (sp.status.description or "") and result not in (sp.status.description or "")
        assert not sp.events
    logged = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert caplog.records and arg not in logged and result not in logged
    assert not any(arg in str(row) or result in str(row) for row in audit)


def test_an_apps_approvals_never_reach_the_owners_phone_or_one_tap_links(tmp_path):
    """The ntfy push and its approve/deny link are owner credentials; an App decides its own approvals."""
    from harness_modules.notifications.service import Notifier
    steps = [Completion(tool_calls=[call("write_file", 0, path="secret/a.txt", content="x")]),
             Completion(content="done")]
    cfg = make_cfg(tmp_path, rules=RULES)
    m = Manager(cfg, chat=Script(steps))
    client = TestClient(create_app(m))
    with client:
        key = client.post("/keys", json={"name": "app-a", "kind": "app",
                                         "scopes": ["sessions", "approvals"]}).json()
        auth = {"Authorization": f"Bearer {key['key']}"}
        sid = client.post("/api/v1/sessions", headers=auth, json={
            "prompt": "write the secret", "project": "guarded", "tools": [BALANCE]}).json()["id"]
        pending = wait_for(lambda: client.get(f"/api/v1/sessions/{sid}/approvals", headers=auth).json())
        approval = m.db.get_approval(pending[0]["id"])
        cfg.notify.enabled = True
        notifier = Notifier(cfg, m.db)
        notifier.listener({"type": "approval_requested", "session_id": sid, "data": {"id": approval["id"]}})
        assert notifier.queue.empty()
        assert client.post(f"/a/{approval['token']}/approve").status_code == 404
        assert m.db.get_approval(approval["id"])["status"] == "pending"
