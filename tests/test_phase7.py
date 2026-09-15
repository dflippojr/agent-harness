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


def wait_until(fn, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


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


# 7b: memory library writes
import subprocess  # noqa: E402

import pytest  # noqa: E402

from harness.config import MemoryLibraryConfig  # noqa: E402
from harness.memory_library import MemoryLibrary, sensitive_hits  # noqa: E402
from harness.policy import ALLOW, ASK, DENY, Policy  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def library_remote(tmp_path):
    """A bare 'GitHub' repo with a small library, plus the daemon's config pointing at it."""
    seed_dir = tmp_path / "seed"
    (seed_dir / "categories" / "sport").mkdir(parents=True)
    (seed_dir / "categories" / "health").mkdir(parents=True)
    (seed_dir / "categories" / "sport" / "memory.md").write_text("# Sport\n\n### 2026-08-01\n- Disc golf twice a week.\n",
                                                                  encoding="utf-8")
    (seed_dir / "categories" / "health" / "memory.md").write_text("# Health\n- private\n", encoding="utf-8")
    (seed_dir / "agent-profile.md").write_text("# Agent profile\n- Prefers short answers.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "master", str(seed_dir)], check=True)
    git(seed_dir, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    git(seed_dir, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "seed")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed_dir), str(bare)], check=True)
    return bare, MemoryLibraryConfig(enabled=True, repo=str(bare), clone_dir=str(tmp_path / "clone"), refresh_minutes=0,
                                     categories=["sport"], writes=True, profile_path="agent-profile.md",
                                     profile_max_chars=300)


def remote_file(tmp_path, bare, path):
    check = tmp_path / f"check{len(list(tmp_path.iterdir()))}"
    subprocess.run(["git", "clone", "-q", str(bare), str(check)], check=True)
    return (check / path).read_text(encoding="utf-8"), git(check, "log", "-1", "--format=%s%n%b")


def test_policy_always_asks_for_memory_writes():
    allow_all = Policy([{"tool": "*", "action": "allow"}])
    assert allow_all.decide("memory_edit", {"path": "x"}).action == ASK
    assert allow_all.decide("run_shell", {"command": "ls"}).action == ALLOW
    deny = Policy([{"tool": "memory_write", "action": "deny", "reason": "not here"}])
    assert deny.decide("memory_write", {}).action == DENY
    assert Policy().decide("memory_edit", {}).reason == "changes your memory library"
    assert sensitive_hits("+++ b/x\n+- Started a new medication\n- old line with salary\n") == ["health"]
    assert sensitive_hits("+- Bought a disc golf bag\n") == []


def test_memory_edit_needs_approval_then_commits_and_pushes(tmp_path):
    bare, lib_cfg = library_remote(tmp_path)
    cfg = make_cfg(tmp_path)
    cfg.memory_library = lib_cfg
    steps = [
        Completion(tool_calls=[call("memory_edit", 0, path="categories/sport/memory.md",
                                    old_text="- Disc golf twice a week.",
                                    new_text="- Disc golf twice a week.\n\n### 2026-09-15\n- Started league play on Sundays.",
                                    summary="Note Sunday league play")]),
        Completion(tool_calls=[call("memory_edit", 1, path="categories/health/memory.md", old_text="- private",
                                    new_text="- diagnosis", summary="sneak")]),
        Completion(tool_calls=[call("memory_write", 2, path="agent-profile.md", content="x" * 400, summary="too long")]),
        Completion(content="Saved the note."),
    ]

    async def body():
        m = Manager(cfg, chat=Script(steps))
        await m.start(maintenance=False)
        s = m.create("remember that I started Sunday league play")
        assert "memory_edit" in m.db.get_session(s["id"])["context"][0]["content"]
        await wait_status(m, s["id"], "waiting_approval")
        approval = m.db.pending_approvals(s["id"])[0]
        assert approval["reason"] == "changes your memory library"
        assert approval["detail"].startswith("Note Sunday league play\n\n") and "+- Started league play" in approval["detail"]
        assert "Sunday" not in remote_file(tmp_path, bare, "categories/sport/memory.md")[0]  # nothing before approval
        m.decide(s["id"], approval["id"], approve=True)
        await wait_status(m, s["id"], "done", timeout=30)
        results = events(m, s["id"], "tool_result")
        assert results[0]["ok"] and "pushed" in results[0]["output"]
        # sensitive category and oversize profile: refused without asking the user
        assert not results[1]["ok"] and "categories/health/memory.md isn't a" in results[1]["output"]
        assert not results[2]["ok"] and "limit is 300" in results[2]["output"]
        assert len(m.db.approvals(s["id"])) == 1
        text, message = remote_file(tmp_path, bare, "categories/sport/memory.md")
        assert "Started league play on Sundays." in text
        assert message.startswith("Note Sunday league play") and s["id"] in message
        await m.stop()
    asyncio.run(body())


def test_memory_write_denied_or_stale_is_not_saved(tmp_path):
    bare, lib_cfg = library_remote(tmp_path)
    lib = MemoryLibrary(lib_cfg, db=None)

    class Approvals:
        def __init__(self):
            self.rows = {}

        def approval_for_call(self, sid, call_id):
            return self.rows.get(call_id)

    lib.db = Approvals()
    args = {"path": "categories/sport/gear.md", "content": "# Gear\n- Innova bag\n", "summary": "Add gear list"}

    async def body():
        detail, warning = await lib.preview("memory_write", args)
        assert warning == "" and "+- Innova bag" in detail
        with pytest.raises(Exception, match="wasn't approved"):
            await lib.call("memory_write", args, session={"id": "s1"}, call_id="c1")
        lib.db.rows["c1"] = {"status": "denied", "detail": detail}
        with pytest.raises(Exception, match="wasn't approved"):
            await lib.call("memory_write", args, session={"id": "s1"}, call_id="c1")
        # someone else pushes a different gear file after the proposal: the approved diff no longer matches
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
        (other / "categories" / "sport" / "gear.md").write_text("# Gear\n- Discmania bag\n", encoding="utf-8")
        git(other, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
        git(other, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "other")
        git(other, "push", "-q")
        lib.db.rows["c1"] = {"status": "approved", "detail": detail}
        with pytest.raises(Exception, match="changed after this change was proposed"):
            await lib.call("memory_write", args, session={"id": "s1"}, call_id="c1")
        assert git(lib.root, "status", "--porcelain") == ""
        # a warning for sensitive-looking additions to an allowed file
        _, warning = await lib.preview("memory_edit", {"path": "agent-profile.md", "old_text": "- Prefers short answers.",
                                                       "new_text": "- Prefers short answers.\n- Takes medication daily.",
                                                       "summary": "x"})
        assert "health" in warning
    asyncio.run(body())


# 7c: frozen agent profile
def test_profile_frozen_per_session_and_not_given_to_apps(tmp_path):
    bare, lib_cfg = library_remote(tmp_path)
    cfg = make_cfg(tmp_path)
    cfg.memory_library = lib_cfg
    steps = [Completion(tool_calls=[call("memory_edit", 0, path="agent-profile.md", old_text="- Prefers short answers.",
                                         new_text="- Prefers short answers.\n- Uses the personal GitHub account.",
                                         summary="Add GitHub account")]),
             Completion(content="Noted.")]

    async def body():
        m = Manager(cfg, chat=Script(steps))
        await m.start(maintenance=False)
        await m.runner.memory.refresh(force=True)
        first = m.create("remember my GitHub account")
        system = m.db.get_session(first["id"])["context"][0]["content"]
        assert "User profile (agent-profile.md" in system and "Prefers short answers." in system
        assert "personal GitHub" not in system
        await wait_status(m, first["id"], "waiting_approval")
        m.decide(first["id"], None, approve=True)
        await wait_status(m, first["id"], "done", timeout=30)
        # the running session's prompt didn't change; the next session sees the edit
        assert m.db.get_session(first["id"])["context"][0]["content"] == system
        second = m.create("what account?")
        assert "Uses the personal GitHub account." in m.db.get_session(second["id"])["context"][0]["content"]
        app = m.create("from an app", app={"id": "k-1", "name": "shop"})
        assert "User profile" not in m.db.get_session(app["id"])["context"][0]["content"]
        await m.stop()
    asyncio.run(body())


def test_memory_api(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app
    bare, lib_cfg = library_remote(tmp_path)
    cfg = make_cfg(tmp_path)
    cfg.memory_library = lib_cfg
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    with TestClient(create_app(m)) as client:
        data = wait_until(lambda: (lambda d: d if d.get("profile") else None)(client.get("/memory").json()))
        assert data["enabled"] and data["writes"] and "Prefers short answers." in data["profile"]
        assert data["profile_max_chars"] == 300
