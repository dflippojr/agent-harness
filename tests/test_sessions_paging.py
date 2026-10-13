"""Agents list paging (#663): active sessions never drop off the first page, older ones page by a keyset cursor and
the machine filter runs in SQL."""

from __future__ import annotations

import pytest

from harness.app_stores import SessionStores
from harness.db import Database
from harness.llm import Completion

from test_api import LOGIN, make_client
from test_app_stores import _session

OWNER = {"Tailscale-User-Login": LOGIN}
BASE = 1_760_000_000.0


def _seed(db, n: int = 60) -> list[str]:
    """`n` finished sessions one second apart, oldest first; the oldest waits on an approval, the next one runs."""
    sids = [f"s{i:03d}" for i in range(n)]
    for i, sid in enumerate(sids):
        status = {0: "waiting_approval", 1: "running"}.get(i, "done")
        db.insert_session(_session(sid, status=status, created_at=BASE + i, updated_at=BASE + 1000 + i))
    return sids


def _walk(db, **kw) -> list[str]:
    seen, before = [], None
    while True:
        rows, before = db.session_page(50, owner_id="owner", before=before, **kw)
        seen += [r["id"] for r in rows]
        if before is None:
            return seen


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "harness.sqlite3")
    yield database
    database.close()


def test_first_page_holds_every_active_session_once(db):
    sids = _seed(db)
    db.insert_session(_session("other", owner_id="u-ann", status="running", created_at=BASE - 5))
    db.insert_session(_session("chat1", kind="chat", status="running", created_at=BASE - 4))
    db.insert_session(_session("asks", status="done", created_at=BASE - 3))
    db.insert_approval({"id": "ap1", "session_id": "asks", "tool_call_id": "c1", "tool": "write_file",
                        "args": {"path": "x"}, "reason": "ask"})

    ids = [s["id"] for s in db.list_sessions(50, owner_id="owner", include_active=True)]
    assert len(ids) == len(set(ids)) == 53
    assert set(ids) == set(sids[10:]) | {"s000", "s001", "asks"}
    assert ids == sids[:9:-1] + ["s001", "s000", "asks"]  # newest first, the old active ones after the page
    assert [s["id"] for s in db.list_sessions(50, owner_id="owner")] == sids[:-51:-1]  # default: no extras


def test_paging_walks_every_session_once_newest_first(db):
    sids = _seed(db)
    for sid in ("t-b", "t-a", "t-c"):  # same instant: the id breaks the tie, so none is skipped or repeated
        db.insert_session(_session(sid, created_at=BASE + 25.5))
    walked = _walk(db)
    assert len(walked) == len(set(walked)) == 63
    expected = sorted([(BASE + i, sid) for i, sid in enumerate(sids)] + [(BASE + 25.5, s) for s in ("t-a", "t-b", "t-c")],
                      reverse=True)
    assert walked == [sid for _, sid in expected]
    rows, cursor = db.session_page(63, owner_id="owner")
    assert len(rows) == 63 and cursor is None  # the last page names no next one


def test_target_filters_in_sql_and_uses_the_list_index(db):
    _seed(db)
    for i in range(3):
        db.insert_session(_session(f"mac{i}", target="mac", created_at=BASE - 100 + i))
    assert [s["id"] for s in db.list_sessions(50, owner_id="owner", target="mac", include_active=True)] == \
        ["mac2", "mac1", "mac0"]
    plan = " ".join(r[3] for r in db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM sessions WHERE kind = 'agent' AND owner_id = 'owner' "
        "ORDER BY created_at DESC, id DESC LIMIT 51"))
    assert "sessions_list" in plan and "TEMP B-TREE" not in plan


def test_app_stores_merge_one_page_across_stores(tmp_path):
    stores = SessionStores(Database(tmp_path / "harness.sqlite3"), tmp_path / "apps")
    try:
        for i in range(4):
            stores.insert_session(_session(f"w{i}", created_at=BASE + 2 * i))
            stores.insert_session(_session(f"a{i}", "k-aaaa", created_at=BASE + 2 * i + 1))
        stores.update_session("w0", status="waiting_approval")
        app = stores.scope("k-aaaa")
        rows, cursor = app.session_page(3, include_active=True)
        assert [r["id"] for r in rows] == ["a3", "w3", "a2", "w0"]
        seen = [r["id"] for r in rows if r["id"] != "w0"]
        while cursor is not None:
            rows, cursor = app.session_page(3, before=cursor)
            seen += [r["id"] for r in rows]
        assert seen == ["a3", "w3", "a2", "w2", "a1", "w1", "a0", "w0"]
    finally:
        stores.close()


def test_route_returns_active_sessions_pages_and_filters_by_machine(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="hi")])
    with client:
        sids = _seed(m.db)
        for i in range(3):
            m.db.insert_session(_session(f"mac{i}", target="mac", created_at=BASE - 100 + i))

        first = client.get("/sessions", headers=OWNER)
        assert first.status_code == 200
        ids = [s["id"] for s in first.json()]
        assert len(ids) == len(set(ids)) == 52
        assert set(ids) == set(sids[10:]) | {"s000", "s001"}

        walked, before = [s["id"] for s in first.json()][:50], first.headers["X-Next-Before"]
        while before:
            page = client.get("/sessions", params={"before": before}, headers=OWNER)
            walked += [s["id"] for s in page.json()]
            before = page.headers.get("X-Next-Before")
        assert walked == sids[::-1] + ["mac2", "mac1", "mac0"]

        mac = client.get("/sessions", params={"target": "mac"}, headers=OWNER)
        assert [s["id"] for s in mac.json()] == ["mac2", "mac1", "mac0"] and "X-Next-Before" not in mac.headers
        for bad in ("nope", "1.5", ":s001", "inf:s001"):
            assert client.get("/sessions", params={"before": bad}, headers=OWNER).status_code == 400


def test_cross_origin_web_can_read_the_page_cursor(tmp_path):
    from fastapi.testclient import TestClient

    from harness.api import create_app
    from harness.manager import Manager
    from test_daemon import Script, make_cfg

    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="hi")]))
    _, token = m.db.main.create_api_key("web", "admin", "owner", origins=["https://web.example"])
    with TestClient(create_app(m)) as client:
        _seed(m.db, 5)
        response = client.get("/api/admin/v1/sessions", params={"limit": 2}, headers={
            "Authorization": "Bearer " + token, "Origin": "https://web.example", "X-Agent-Harness-Client": "web/2"})
        assert response.status_code == 200, response.text
        assert response.headers["Access-Control-Allow-Origin"] == "https://web.example"
        assert response.headers["X-Next-Before"] == f"{BASE + 3!r}:s003"
        assert "X-Next-Before" in response.headers["Access-Control-Expose-Headers"]


def test_a_zero_limit_is_an_empty_page_not_an_error(db):
    _seed(db, 3)
    assert db.session_page(0, owner_id="owner") == ([], None)
