"""Agent Harness Web's store (#330 stage c, decision 4): the owner's and members' sessions move out of the main store
into Web's own App store once, at startup, and every owner surface reads them there."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import app_stores
from harness.api import create_app
from harness.app_stores import APP_STORE_FILE, SESSION_TABLES, WEB_APP_ID, WEB_MIGRATION_ENV, WEB_STORE_META, \
    SessionStores
from harness.db import Database
from harness.llm import Completion
from harness.manager import Manager
from harness_modules.search.service import SessionSearch

from test_app_stores import _fill, _key, _session, rows, rows_of, session_ids
from test_daemon import Script, call, make_cfg, wait_status
from test_phase6 import wait_for

OWNER = (("own1", "heron"), ("own2", "egret"), ("own3", "plover"))
MEMBERS = (("mem1", "u-ann", "kestrel"), ("mem2", "u-bob", "merlin"))
EVERYONE = {sid for sid, _ in OWNER} | {sid for sid, _, _ in MEMBERS}


def _seed(path: Path) -> None:
    """A main store as before #330 stage c: the owner's and members' sessions, each with its rows, and an App's."""
    db = Database(path)
    for sid, word in OWNER:
        db.insert_session(_session(sid))
        _fill(db, sid, word)
    for sid, user, word in MEMBERS:
        db.insert_session(_session(sid, owner_id=user))
        _fill(db, sid, word)
    db.insert_session(_session("app1", "k-aaaa"))
    _fill(db, "app1", "pelican")
    db.add_checkpoint("own1", 1, "abc123", "abc123", "main")
    db.add_review_comment("own1", {"repo": "", "path": "x", "side": "new", "start_line": 1, "end_line": 1,
                                   "quoted": [], "comment": "c", "base": "", "head": ""})
    db.record_usage("fake", "own1", "", 10, 5, 0.0, "subscription")
    db.set_meta("profile_emoji", "🦉")
    db.close()


def _web(root: Path) -> Path:
    return root / "apps" / WEB_APP_ID / APP_STORE_FILE


def _backups(root: Path) -> list[Path]:
    return sorted((root / "pre-migration").glob("harness-web-store-*.sqlite3"))


def _snapshot(path: Path) -> dict:
    """Everything the migration could change in a store file."""
    out = {t: rows(path, f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("sessions", *SESSION_TABLES, "search_index")}
    out["keys"] = rows(path, "SELECT id, kind FROM api_keys ORDER BY id")
    out["meta"] = rows(path, "SELECT key, value FROM meta WHERE key != 'search_index' ORDER BY key")
    return out


def _for_a_request(*modules: str) -> bool:
    """Whether this call is made for a request or tool in one of these harness modules, not by the daemon's own
    background work on another thread."""
    return any(Path(f.filename).name in modules and "harness" in Path(f.filename).parts
               for f in traceback.extract_stack())


def test_the_migration_moves_every_owner_and_member_session_with_a_backup_once(tmp_path, caplog):
    path = tmp_path / "harness.sqlite3"
    _seed(path)
    main_before = rows_of(path, EVERYONE)

    with caplog.at_level(logging.INFO, logger="harness.app_stores"):
        stores = SessionStores(Database(path), tmp_path / "apps")
    web = _web(tmp_path)
    assert stores.web is not stores.main and Path(stores.web.path) == web

    # The backup came first: it holds every session the main store had, the App's included (moved just before).
    [backup] = _backups(tmp_path)
    assert session_ids(backup) == EVERYONE
    assert rows_of(backup, EVERYONE) == main_before
    # Every owner and member session moved with all its rows; the search index was rebuilt in Web's store.
    assert session_ids(web) == EVERYONE
    moved = rows_of(web, EVERYONE)
    assert {t: n for t, n in moved.items() if t != "search_index"} == \
        {t: n for t, n in main_before.items() if t != "search_index"}
    assert moved["events"] and moved["approvals"] and moved["artifacts"] and moved["checkpoints"]
    assert moved["review_comments"] and moved["search_index"]
    assert "app1" not in session_ids(web)
    # The main store keeps no sessions, and everything global.
    assert session_ids(path) == set() and not any(rows_of(path, EVERYONE).values())
    assert rows(path, "SELECT session_id FROM usage") == [("own1",)]
    assert stores.get_meta("profile_emoji") == "🦉"
    # Web is registered in the App registry under its reserved id, without a usable key, and the move is recorded.
    web_app = stores.get_api_key(WEB_APP_ID)
    assert web_app["kind"] == "web" and web_app["revoked_at"] is None
    assert WEB_APP_ID not in {k["id"] for k in stores.list_api_keys()}
    record = json.loads(stores.get_meta(WEB_STORE_META))
    assert record["sessions"] == len(EVERYONE) and record["backup"] == str(backup)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert f"moving {len(EVERYONE)} session(s)" in logged and f"moved {len(EVERYONE)} session(s)" in logged

    # Reads route there: the routing wrapper finds each session, its events, approvals and artifacts.
    assert stores.get_session("own1")["owner_id"] == "owner"
    assert [e["data"]["content"] for e in stores.events("mem2")] == ["find the merlin"]
    assert stores.get_approval("ap-own2")["session_id"] == "own2"
    assert stores.full_artifact("own3", "h1") == "artifact body"
    assert stores.checkpoints("own1")
    stores.close()

    # A restart finds nothing to move: no new backup, no store changes.
    before = {p: _snapshot(p) for p in (path, web)}
    again = SessionStores(Database(path), tmp_path / "apps")
    assert Path(again.web.path) == web
    assert again.get_session("own2")["title"] == "own2"
    again.close()
    assert len(_backups(tmp_path)) == 1
    assert {p: _snapshot(p) for p in before} == before


def test_a_dry_run_changes_nothing(tmp_path, monkeypatch, caplog):
    path = tmp_path / "harness.sqlite3"
    _seed(path)
    monkeypatch.setenv(WEB_MIGRATION_ENV, "dry-run")
    stores = SessionStores(Database(path), tmp_path / "apps")  # the App session moves (stage a); nothing else
    stores.close()
    before = _snapshot(path)

    with caplog.at_level(logging.WARNING, logger="harness.app_stores"):
        stores = SessionStores(Database(path), tmp_path / "apps")
    assert stores.web is stores.main                              # the main store keeps serving the sessions
    assert stores.get_session("own1")["title"] == "own1"
    assert {s["id"] for s in stores.scope("").list_sessions(owner_id="owner")} == {"own1", "own2", "own3"}
    stores.close()
    assert "dry run): would move 5 session(s)" in caplog.text
    assert _snapshot(path) == before
    assert not _web(tmp_path).exists() and not _backups(tmp_path)


def test_an_aborted_migration_leaves_the_main_store_untouched(tmp_path, monkeypatch, caplog):
    path = tmp_path / "harness.sqlite3"
    _seed(path)
    real = app_stores._session_row_counts

    def miscount(db, sids):  # Web's store "lost" an event in the copy
        counts = real(db, sids)
        if Path(db.path).parent.name == WEB_APP_ID:
            counts["events"] -= 1
        return counts
    monkeypatch.setattr(app_stores, "_session_row_counts", miscount)
    stores = SessionStores(Database(path), tmp_path / "apps")
    stores.close()
    before = _snapshot(path)                                       # after the App's own move (stage a)

    with caplog.at_level(logging.ERROR, logger="harness.app_stores"):
        stores = SessionStores(Database(path), tmp_path / "apps")
    assert "Web store migration aborted" in caplog.text
    assert stores.web is stores.main and stores.get_session("mem1")["owner_id"] == "u-ann"
    stores.close()
    assert _snapshot(path) == before and session_ids(path) == EVERYONE
    assert session_ids(_web(tmp_path)) == set()                     # the copy rolled back
    assert len(_backups(tmp_path)) == 2                             # one per attempt, never deleted

    monkeypatch.setattr(app_stores, "_session_row_counts", real)  # fixed: the next start moves them
    stores = SessionStores(Database(path), tmp_path / "apps")
    stores.close()
    assert session_ids(_web(tmp_path)) == EVERYONE and session_ids(path) == set()


def test_owner_surfaces_read_webs_store(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.search.enabled = True
    _seed(Path(cfg.db_path))
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        assert session_ids(Path(cfg.db_path)) == set()
        # Web's lists, transcripts, search and the owner token's API read the moved sessions.
        assert {s["id"] for s in client.get("/sessions").json()} == {"own1", "own2", "own3"}
        assert client.get("/sessions/own1").json()["id"] == "own1"
        assert client.get("/sessions/own1/approvals?all=true").status_code == 200
        assert [r["id"] for r in client.get("/search?q=heron").json()["results"]] == ["own1"]
        _, owner = _key(client, "control-center", "admin", kind="owner")
        assert {s["id"] for s in client.get("/api/v1/sessions", headers=owner).json()} == {"own1", "own2", "own3"}
        # The agent's own tools: session_search and session_read from an owner session.
        tools = SessionSearch(m.db)
        assert "own1" in tools.session_search("heron", _session="own2")
        assert "heron" in tools.session_read("own1", _session="own2")
        # Background counts: metrics, the Control Center totals, the smart-review stats.
        from harness.metrics import render
        assert 'harness_sessions{status="done"} 5' in render(m)
        assert m.db.count_sessions("owner", "done") == 3 + 1        # the App's counted by owner id, as before
        assert m.db.smart_review_stats() is not None
        # Web's store can't be revoked, erased or given a retention through the App paths.
        assert client.delete(f"/keys/{WEB_APP_ID}").status_code == 404
        with pytest.raises(ValueError):
            m.db.drop_app(WEB_APP_ID)
        with pytest.raises(ValueError):
            m.db.mark_app_erased(WEB_APP_ID)
        assert not m.db.set_app_retention(WEB_APP_ID, 1)
        assert m.db.get_api_key(WEB_APP_ID)["revoked_at"] is None
    m.db.close()


def test_members_stay_isolated_in_webs_store(tmp_path):
    path = tmp_path / "harness.sqlite3"
    _seed(path)
    stores = SessionStores(Database(path), tmp_path / "apps")
    view = stores.scope("")
    assert {s["id"] for s in view.list_sessions(owner_id="u-ann")} == {"mem1"}
    assert {s["id"] for s in view.list_sessions(owner_id="owner")} == {"own1", "own2", "own3"}
    assert {r["session_id"] for r in view.search_events("find", user_id="u-bob")} == {"mem2"}
    assert view.find_session_ids("mem", user_id="u-ann") == ["mem1"]
    assert stores.get_session_for_user("own1", "u-ann") is None
    assert stores.get_session_for_user("mem2", "u-ann") is None
    tools = SessionSearch(stores)
    stores.insert_session(_session("mem3", owner_id="u-ann"))
    assert tools.session_search("heron", _session="mem3").startswith("No earlier sessions")
    assert tools.session_search("merlin", _session="mem3").startswith("No earlier sessions")
    assert "kestrel" in tools.session_search("kestrel", _session="mem3")
    stores.close()


def test_other_apps_never_reach_webs_store_and_the_owner_never_reaches_theirs(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.search.enabled = True
    _seed(Path(cfg.db_path))
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        a_id, a = _key(client, "app-a", "sessions")
        a_sid = client.post("/api/v1/sessions", headers=a, json={"prompt": "check the pelican ledger"}).json()["id"]
        wait_for(lambda: m.db.get_session(a_sid)["status"] == "done" and a_sid not in m.tasks)

        # Every read the App makes, by any path, stays out of Web's store. (The daemon's own background work may read
        # it meanwhile: only calls made for the App's requests and tools count.)
        web, reads = m.db.web, []

        class Spy:
            def __getattr__(self, name):
                if not name.startswith("_") and _for_a_request("apps.py", "search.py"):
                    reads.append(name)
                return getattr(web, name)
        m.db.web = Spy()
        try:
            assert {s["id"] for s in client.get("/api/v1/sessions", headers=a).json()} == {a_sid}
            assert client.get(f"/api/v1/sessions/{a_sid}", headers=a).status_code == 200
            for ref in ("own1", "own", "mem1"):
                assert client.get(f"/api/v1/sessions/{ref}", headers=a).status_code == 404
            assert {r["id"] for r in client.get("/api/v1/search?q=ledger", headers=a).json()["results"]} == {a_sid}
            assert not client.get("/api/v1/search?q=heron", headers=a).json()["results"]
            assert client.get("/api/v1/queue", headers=a).status_code == 200
            assert SessionSearch(m.db).session_search("heron", _session=a_sid).startswith("No earlier sessions")
        finally:
            m.db.web = web
        assert reads == [], reads

        # And nothing the owner asks for opens an App's store: they see its metadata.
        opened: list[str] = []
        acquire = m.db._acquire
        m.db._acquire = lambda app_id: (_for_a_request("api.py", "endpoint.py") and opened.append(app_id))             or acquire(app_id)
        assert {s["id"] for s in client.get("/sessions").json()} == {"own1", "own2", "own3"}
        assert client.get(f"/sessions/{a_sid}").status_code == 404
        assert not client.get("/search?q=pelican").json()["results"]
        keys = {k["id"]: k for k in client.get("/keys").json()}
        m.db._acquire = acquire
        assert opened == []
        assert keys[a_id]["store"]["sessions"] == {"done": 1}
    m.db.close()


def test_a_session_from_before_the_move_resumes_from_webs_store(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    script = Script([
        Completion(tool_calls=[call("list_files", 0)]),
        lambda msgs: Completion(content="resumed"),
    ])

    async def before_the_move():  # a dry run keeps the main store in use, as before stage c
        monkeypatch.setenv(WEB_MIGRATION_ENV, "dry-run")
        m = Manager(cfg, chat=script)
        assert m.db.web is m.db.main
        s = m.create("list")
        m.tasks[s["id"]].cancel()
        await asyncio.sleep(0.05)
        m.db.update_session(s["id"], status="running")
        m.db.close()
        return s["id"]

    async def after_the_move(sid):
        monkeypatch.delenv(WEB_MIGRATION_ENV)
        m = Manager(cfg, chat=script)
        assert session_ids(Path(cfg.db_path)) == set() and session_ids(_web(Path(cfg.data_dir))) == {sid}
        await m.start()
        s = await wait_status(m, sid, "done")
        assert s["answer"] == "resumed"
        await m.stop()
        m.db.close()
    sid = asyncio.run(before_the_move())
    asyncio.run(after_the_move(sid))
