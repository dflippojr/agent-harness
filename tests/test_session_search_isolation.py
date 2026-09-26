"""Issue #105: session_search / session_read honor the same app boundary as /api/v1."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.db import Database
from harness.fileops import ToolError
from harness.llm import Completion
from harness.manager import Manager
from harness.search import SessionSearch, restrict_app_id, search

from test_daemon import Script, call, events, wait_status
from test_phase7 import search_cfg, seed


OWNER_MARKER = "ownermarkerq7k2"
APP_A_MARKER = "appamarkern4p8"
APP_B_MARKER = "appbmarkerw9m1"
SHARED_MARKER = "sharedmarkerz3v6"


def _tools(tmp_path) -> tuple[Database, SessionSearch]:
    db = Database(tmp_path / "h.sqlite3")
    return db, SessionSearch(db)


def _apps(db: Database):
    app_a, _ = db.create_api_key("shop", "sessions", "app")
    app_b, _ = db.create_api_key("other", "sessions", "app")
    reader, _ = db.create_api_key("dash", "sessions sessions:all", "app")
    return app_a, app_b, reader


def _history(db: Database, app_a: dict, app_b: dict, reader: dict):
    seed(db, "aaaa000001", "scratch", "Owner private notes", [
        ("user_message", {"content": f"keep {OWNER_MARKER} secret"}),
        ("status", {"status": "done", "answer": f"stored {OWNER_MARKER}"}),
    ], created=1)
    seed(db, "aaaa00000a", "scratch", "App A shopping list", [
        ("user_message", {"content": f"buy {APP_A_MARKER} milk"}),
        ("status", {"status": "done", "answer": f"listed {APP_A_MARKER}"}),
    ], created=2, app_id=app_a["id"])
    seed(db, "cccc00000b", "scratch", "App B itinerary", [
        ("user_message", {"content": f"fly {APP_B_MARKER} tomorrow"}),
        ("status", {"status": "done", "answer": f"booked {APP_B_MARKER}"}),
    ], created=3, app_id=app_b["id"])
    seed(db, "bbbb00000a", "scratch", "App A follow-up", [
        ("user_message", {"content": "what did I list last time?"}),
    ], created=4, app_id=app_a["id"])
    seed(db, "bbbb00000b", "scratch", "App B follow-up", [
        ("user_message", {"content": "search earlier trips"}),
    ], created=5, app_id=app_b["id"])
    seed(db, "dddd00000r", "scratch", "Read-all follow-up", [
        ("user_message", {"content": "look up earlier work"}),
    ], created=6, app_id=reader["id"])


def assert_no_hit(text: str, *ids: str) -> None:
    assert text.startswith("No earlier sessions match")
    for sid in ids:
        assert sid not in text


def test_restrict_app_id_matches_api_scopes(tmp_path):
    db, _ = _tools(tmp_path)
    app_a, app_b, reader = _apps(db)
    _history(db, app_a, app_b, reader)
    assert restrict_app_id(db, "") is None
    assert restrict_app_id(db, "aaaa000001") is None
    assert restrict_app_id(db, "bbbb00000a") == app_a["id"]
    assert restrict_app_id(db, "bbbb00000b") == app_b["id"]
    assert restrict_app_id(db, "dddd00000r") is None
    with pytest.raises(ToolError, match="no session matches"):
        restrict_app_id(db, "missing")


def test_revoked_sessions_all_does_not_unrestrict(tmp_path):
    db, tools = _tools(tmp_path)
    app_a, app_b, reader = _apps(db)
    _history(db, app_a, app_b, reader)

    assert restrict_app_id(db, "dddd00000r") is None
    assert db.revoke_api_key(reader["id"])
    assert restrict_app_id(db, "dddd00000r") == reader["id"]
    with pytest.raises(ToolError, match="no session matches"):
        tools.session_read("aaaa000001", _session="dddd00000r")
    hidden = tools.session_search(OWNER_MARKER, _session="dddd00000r")
    assert_no_hit(hidden, "aaaa000001")

    # A non-elevated revoked app keeps its own-app boundary; in-flight sessions are not cancelled.
    assert db.revoke_api_key(app_a["id"])
    assert restrict_app_id(db, "bbbb00000a") == app_a["id"]
    own = tools.session_search(APP_A_MARKER, _session="bbbb00000a")
    assert "aaaa00000a" in own
    assert APP_A_MARKER in own
    assert "aaaa000001" not in own


def test_direct_tools_isolate_owner_two_apps_and_read_all(tmp_path):
    db, tools = _tools(tmp_path)
    app_a, app_b, reader = _apps(db)
    _history(db, app_a, app_b, reader)

    owner_search = tools.session_search(APP_A_MARKER, _session="aaaa000001")
    assert "aaaa00000a" in owner_search
    assert APP_A_MARKER in owner_search
    a_own = tools.session_search(APP_A_MARKER, _session="bbbb00000a")
    assert "aaaa00000a" in a_own
    assert APP_A_MARKER in a_own
    assert "aaaa000001" not in a_own
    assert "cccc00000b" not in a_own

    b_owner = tools.session_search(OWNER_MARKER, _session="bbbb00000b")
    assert_no_hit(b_owner, "aaaa000001")
    b_other = tools.session_search(APP_A_MARKER, _session="bbbb00000b")
    assert_no_hit(b_other, "aaaa00000a")

    all_search = tools.session_search(OWNER_MARKER, _session="dddd00000r")
    assert "aaaa000001" in all_search
    assert OWNER_MARKER in all_search

    owner_read = tools.session_read("aaaa00000a", _session="aaaa000001")
    assert APP_A_MARKER in owner_read
    own_read = tools.session_read("aaaa00000a", _session="bbbb00000a")
    assert APP_A_MARKER in own_read
    assert "listed" in own_read
    with pytest.raises(ToolError, match="no session matches"):
        tools.session_read("aaaa000001", _session="bbbb00000a")
    with pytest.raises(ToolError, match="no session matches"):
        tools.session_read("cccc00000b", _session="bbbb00000a")
    read_all = tools.session_read("aaaa000001", _session="dddd00000r")
    assert OWNER_MARKER in read_all


def test_prefix_and_guessed_ids_do_not_bypass_authorization(tmp_path):
    db, tools = _tools(tmp_path)
    app_a, app_b, reader = _apps(db)
    _history(db, app_a, app_b, reader)

    # Shared prefix matches owner + app A; the app must resolve only its own session.
    resolved = tools.session_read("aaaa", _session="bbbb00000a")
    assert "aaaa00000a" in resolved
    assert APP_A_MARKER in resolved
    assert OWNER_MARKER not in resolved
    assert "aaaa000001" not in resolved
    with pytest.raises(ToolError, match=r"no session matches 'aaaa000001'"):
        tools.session_read("aaaa000001", _session="bbbb00000a")
    with pytest.raises(ToolError, match=r"no session matches 'aaaa0000'"):
        tools.session_read("aaaa0000", _session="bbbb00000b")
    with pytest.raises(ToolError, match="no session matches"):
        tools.session_read("aaaa000001", _session="bbbb00000b")


def test_visibility_applies_before_ranking_and_limits(tmp_path):
    db, tools = _tools(tmp_path)
    app_a, _app_b, _reader = _apps(db)
    now = time.time()
    seed(db, "appa000001", "scratch", "App A hit", [
        ("user_message", {"content": SHARED_MARKER}),
        ("status", {"status": "done", "answer": f"app stored {SHARED_MARKER}"}),
    ], created=now - 1000, app_id=app_a["id"])
    seed(db, "appa000002", "scratch", "App A caller", [
        ("user_message", {"content": "look it up"}),
    ], created=now + 100, app_id=app_a["id"])
    for i in range(8):
        seed(db, f"ownr{i:06d}", "scratch", f"Owner hit {i}", [
            ("user_message", {"content": SHARED_MARKER}),
            ("status", {"status": "done", "answer": f"owner {i} {SHARED_MARKER}"}),
        ], created=now + i)
    found = search(db, SHARED_MARKER, limit=5, exclude="appa000002", app_id=app_a["id"])
    assert [r["id"] for r in found["results"]] == ["appa000001"]
    text = tools.session_search(SHARED_MARKER, limit=5, _session="appa000002")
    assert "appa000001" in text
    assert SHARED_MARKER in text
    assert "ownr" not in text
    assert "owner 0" not in text


def test_http_api_still_hides_foreign_sessions(tmp_path):
    cfg = search_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    m._spawn = lambda *_args, **_kwargs: None
    with TestClient(create_app(m)) as client:
        owner = client.post("/sessions", json={"prompt": f"remember {OWNER_MARKER}"}).json()
        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions"]}).json()
        auth = {"Authorization": f"Bearer {app['key']}"}
        app_session = client.post("/api/v1/sessions", headers=auth,
                                  json={"prompt": f"buy {APP_A_MARKER}"}).json()
        assert client.get(f"/api/v1/sessions/{owner['id']}", headers=auth).status_code == 404
        tools = SessionSearch(m.db)
        with pytest.raises(ToolError, match="no session matches"):
            tools.session_read(owner["id"], _session=app_session["id"])
        hidden = tools.session_search(OWNER_MARKER, _session=app_session["id"])
        assert_no_hit(hidden, owner["id"])
        assert app_session["id"] in tools.session_search(APP_A_MARKER, _session=owner["id"])


def test_call_ignores_spoofed_session_argument(tmp_path):
    db, tools = _tools(tmp_path)
    app_a, app_b, reader = _apps(db)
    _history(db, app_a, app_b, reader)

    async def body():
        with pytest.raises(ToolError, match="no session matches"):
            await tools.call("session_read", {"session_id": "aaaa000001", "_session": "aaaa000001"},
                             session={"id": "bbbb00000a"})
        text = await tools.call("session_search", {"query": OWNER_MARKER, "_session": "aaaa000001"},
                                session={"id": "bbbb00000a"})
        assert "aaaa000001" not in text
    asyncio.run(body())


def test_agent_tool_path_owner_two_apps_own_session_and_read_all(tmp_path):
    cfg = search_cfg(tmp_path)

    def router(messages):
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if any(m["role"] == "tool" for m in messages):
            return Completion(content="noted")
        if user.startswith("search "):
            return Completion(tool_calls=[call("session_search", 0, query=user[7:], limit=10)])
        if user.startswith("read "):
            return Completion(tool_calls=[call("session_read", 0, session_id=user[5:].strip())])
        return Completion(content=user)

    async def body():
        m = Manager(cfg, chat=Script([router]))
        await m.start(maintenance=False)
        owner = m.create(f"keep {OWNER_MARKER} secret")
        await wait_status(m, owner["id"], "done")
        app_a, _ = m.db.create_api_key("shop", "sessions", "app")
        app_b, _ = m.db.create_api_key("other", "sessions", "app")
        reader, _ = m.db.create_api_key("dash", "sessions sessions:all", "app")
        a_hist = m.create(f"buy {APP_A_MARKER} milk", app=app_a)
        await wait_status(m, a_hist["id"], "done")
        b_hist = m.create(f"fly {APP_B_MARKER} tomorrow", app=app_b)
        await wait_status(m, b_hist["id"], "done")

        a_search_own = m.create(f"search {APP_A_MARKER}", app=app_a)
        await wait_status(m, a_search_own["id"], "done")
        a_search_owner = m.create(f"search {OWNER_MARKER}", app=app_a)
        await wait_status(m, a_search_owner["id"], "done")
        a_read_owner = m.create(f"read {owner['id']}", app=app_a)
        await wait_status(m, a_read_owner["id"], "done")
        a_read_b = m.create(f"read {b_hist['id']}", app=app_a)
        await wait_status(m, a_read_b["id"], "done")
        b_search_a = m.create(f"search {APP_A_MARKER}", app=app_b)
        await wait_status(m, b_search_a["id"], "done")
        owner_search = m.create(f"search {APP_A_MARKER}")
        await wait_status(m, owner_search["id"], "done")
        all_search = m.create(f"search {OWNER_MARKER}", app=reader)
        await wait_status(m, all_search["id"], "done")
        all_read_b = m.create(f"read {b_hist['id']}", app=reader)
        await wait_status(m, all_read_b["id"], "done")

        def output(sid):
            return events(m, sid, "tool_result")[0]

        own = output(a_search_own["id"])
        assert own["ok"]
        assert a_hist["id"] in own["output"]
        assert APP_A_MARKER in own["output"]
        assert owner["id"] not in own["output"]
        hidden_owner = output(a_search_owner["id"])
        assert hidden_owner["ok"]
        assert_no_hit(hidden_owner["output"], owner["id"])
        denied_owner = output(a_read_owner["id"])
        assert not denied_owner["ok"]
        assert "no session matches" in denied_owner["output"]
        assert OWNER_MARKER not in denied_owner["output"]
        denied_b = output(a_read_b["id"])
        assert not denied_b["ok"]
        assert "no session matches" in denied_b["output"]
        hidden_a = output(b_search_a["id"])
        assert hidden_a["ok"]
        assert_no_hit(hidden_a["output"], a_hist["id"])
        visible_to_owner = output(owner_search["id"])
        assert visible_to_owner["ok"]
        assert a_hist["id"] in visible_to_owner["output"]
        visible_to_all = output(all_search["id"])
        assert visible_to_all["ok"]
        assert owner["id"] in visible_to_all["output"]
        assert OWNER_MARKER in visible_to_all["output"]
        read_all_b = output(all_read_b["id"])
        assert read_all_b["ok"]
        assert APP_B_MARKER in read_all_b["output"]
        await m.stop()
    asyncio.run(body())
