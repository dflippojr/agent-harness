"""Phase 7 tests: session search (7a). Scripted model, no GPU or Docker."""

from __future__ import annotations

import asyncio
import json
import time

from harness.config import Project, SearchConfig
from harness.db import Database
from harness.llm import Completion
from harness.manager import Manager
from harness.search import compact_transcript, fts_query, search

from test_daemon import Script, call, events, make_cfg, wait_status


def search_cfg(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.search = SearchConfig(enabled=True)
    return cfg


def test_fts_query_quotes_everything():
    assert fts_query('plex-webhook "stale data" grafana*') == '"stale data" "plex-webhook" "grafana"*'
    assert fts_query("how did we fix the plex webhook", any_term=True) == '"fix" OR "plex" OR "webhook"'
    assert fts_query('NEAR(a b) OR x" AND') == '"NEAR" "a" "b" "OR" "x" "AND"'
    assert fts_query("   ") == ""


def seed(db: Database, sid: str, project: str, title: str, texts: list[tuple[str, dict]], created: float = 0) -> None:
    import time
    now = created or time.time()
    db.insert_session({"id": sid, "project": project, "target": "tower", "model": "fake", "title": title,
                       "status": "done", "workspace": "", "created_at": now, "updated_at": now, "context": [],
                       "answer": ""})
    db.insert_event(sid, "session_created", {"title": title, "project": project})
    for type_, data in texts:
        db.insert_event(sid, type_, data)


def test_search_ranks_groups_and_falls_back(tmp_path):
    db = Database(tmp_path / "h.sqlite3")
    seed(db, "aaa1", "plex-webhook", "Fix stale Grafana data", [
        ("user_message", {"content": "The plex dashboard shows stale data"}),
        ("assistant", {"content": "Checking Prometheus", "tool_calls": [
            {"id": "c", "function": {"name": "prometheus_query", "arguments": '{"query": "up{job=\\"plex\\"}"}'}}]}),
        ("tool_result", {"name": "prometheus_query", "ok": True, "output": "plex_webhook target down since 09:00"}),
        ("status", {"status": "done", "answer": "The scrape target was renamed; fixed the job name."}),
    ])
    seed(db, "bbb2", "homelab", "Restart ntfy", [
        ("user_message", {"content": "ntfy is down, restart it"}),
        ("tool_result", {"name": "container_logs", "ok": True, "output": "ntfy exited with code 137 (OOM)"}),
    ])
    found = search(db, "stale grafana")
    assert [r["id"] for r in found["results"]] == ["aaa1"] and found["mode"] == "all"
    assert "\x02" in found["results"][0]["passages"][0]["text"]
    # nothing has every word: fall back to any word, stopwords dropped
    loose = search(db, "how did we fix the ntfy OOM last time")
    assert loose["mode"] == "any" and loose["results"][0]["id"] == "bbb2"
    assert search(db, "restart", project="plex-webhook")["results"] == []
    assert [r["id"] for r in search(db, "restart")["results"]] == ["bbb2"]
    assert search(db, '"target was renamed"')["results"][0]["id"] == "aaa1"  # phrase
    assert search(db, "ntfy", exclude="bbb2")["results"] == []
    text = compact_transcript(db, "aaa1")
    assert "## User" in text and "call prometheus_query" in text and "renamed" in text


def test_index_backfills_existing_events_once(tmp_path):
    path = tmp_path / "h.sqlite3"
    db = Database(path)
    seed(db, "old1", "scratch", "Rotate the backup folder", [("user_message", {"content": "prune old snapshots"})])
    db.conn.execute("DELETE FROM search_index")  # as if the events predate the index
    db.conn.execute("DELETE FROM meta")
    db.close()
    db = Database(path)
    assert search(db, "snapshots")["results"][0]["id"] == "old1"
    rows = db.conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
    db.close()
    assert Database(path).conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == rows


def test_agent_tools_find_earlier_sessions_but_not_their_own(tmp_path):
    cfg = search_cfg(tmp_path)
    cfg.projects["private"] = Project(name="private", session_search=False)

    def reader(messages):
        import re
        found = [m for m in messages if m["role"] == "tool"]
        sid = re.search(r"^([0-9a-f]{10}) ·", found[-1]["content"], re.M).group(1) if found else ""
        return Completion(tool_calls=[call("session_read", 1, session_id=sid, find="off-by-one")])

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="Fixed the off-by-one in invoice totals.")]))
        await m.start(maintenance=False)
        first = m.create("fix the invoice totals bug")
        await wait_status(m, first["id"], "done")
        await m.stop()

        m = Manager(cfg, chat=Script([
            Completion(tool_calls=[call("session_search", 0, query="invoice off-by-one")]),
            reader,
            Completion(content="Last time it was an off-by-one."),
        ]))
        await m.start(maintenance=False)
        second = m.create("what was wrong with invoice totals last time?")
        await wait_status(m, second["id"], "done")
        results = events(m, second["id"], "tool_result")
        assert results[0]["ok"] and first["id"] in results[0]["output"]
        assert second["id"] not in results[0]["output"].split("Read one")[0]
        assert results[1]["ok"] and "off-by-one" in results[1]["output"]
        assert "session_search" in m.db.get_session(second["id"])["context"][0]["content"]

        hidden = m.create("x", project="private")
        s = m.db.get_session(hidden["id"])
        assert "session_search" not in {t["function"]["name"] for t in m.runner.tool_schemas(s, m.runner.workspace(s))}
        await m.stop()
    asyncio.run(body())


def test_search_api(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    cfg = search_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="Tuned the Grafana refresh to 5 s.")]))
    with TestClient(create_app(m)) as client:
        sid = client.post("/sessions", json={"prompt": "make the grafana countdown smoother"}).json()["id"]
        for _ in range(200):
            if client.get(f"/sessions/{sid}").json()["status"] == "done":
                break
            time.sleep(0.02)
        data = client.get("/search", params={"q": "grafana refresh"}).json()
        assert data["results"][0]["id"] == sid and data["results"][0]["passages"]
        assert json.dumps(data)  # serializable
        m.cfg.search.enabled = False
        assert client.get("/search", params={"q": "x"}).status_code == 400
