"""Phase 7 tests: session search (7a), memory writes (7b), agent profile (7c), scheduled jobs (7d).
Scripted model, no GPU or Docker; memory tests use a local bare git repo."""

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


# 7d: scheduled jobs
from datetime import datetime  # noqa: E402

from harness.config import JobsConfig  # noqa: E402
from harness.jobs import Cron, CronError, JobScheduler, parse_status  # noqa: E402
from harness.notify import Notifier  # noqa: E402


def ts(*parts) -> float:
    return datetime(*parts).timestamp()


@pytest.mark.parametrize("expr,after,expected", [
    ("0 8 * * *", (2026, 9, 15, 7, 59, 30), (2026, 9, 15, 8, 0)),
    ("0 8 * * *", (2026, 9, 15, 8, 0, 0), (2026, 9, 16, 8, 0)),
    ("*/15 * * * *", (2026, 9, 15, 8, 1), (2026, 9, 15, 8, 15)),
    ("0 9 * * mon-fri", (2026, 9, 18, 10, 0), (2026, 9, 21, 9, 0)),   # Friday after 9 -> Monday
    ("0 10 * * 7", (2026, 9, 15, 0, 0), (2026, 9, 20, 10, 0)),        # 7 = Sunday
    ("30 6 1 * *", (2026, 9, 15, 0, 0), (2026, 10, 1, 6, 30)),
    ("0 8 13 * fri", (2026, 9, 15, 0, 0), (2026, 9, 18, 8, 0)),       # both day fields: either matches
    ("0 0 29 2 *", (2026, 3, 1, 0, 0), (2028, 2, 29, 0, 0)),          # leap day
    ("@weekly", (2026, 9, 15, 0, 0), (2026, 9, 20, 0, 0)),
])
def test_cron_next(expr, after, expected):
    assert Cron(expr).next_after(ts(*after)) == ts(*expected)


@pytest.mark.parametrize("bad", ["61 * * * *", "* * *", "0 8 * * funday", "5-1 * * * *", "*/0 * * * *", "0 0 31 2 *"])
def test_cron_rejects(bad):
    with pytest.raises(CronError):
        Cron(bad).next_after(ts(2026, 1, 1, 0, 0))


def test_parse_status():
    assert parse_status("All six services up.\nSTATUS: OK") == ("ok", "")
    assert parse_status("x\n**STATUS: ATTENTION: ntfy is down**") == ("attention", "ntfy is down")
    assert parse_status("STATUS: ATTENTION earlier\n...\nSTATUS: OK") == ("ok", "")
    assert parse_status("no status here") == ("", "")
    from harness.jobs import summary
    report = "## Services\n| name | state |\n|---|---|\n| ntfy | up |\n\nAll 6 services are running with 0 restarts.\n\nSTATUS: OK"
    assert summary(report) == "All 6 services are running with 0 restarts."
    assert summary("x" * 500).endswith("…") and len(summary("x" * 500)) == 300


def jobs_cfg(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.jobs = JobsConfig(enabled=True, poll_seconds=3600)  # tests drive tick() themselves
    return cfg


def add_job(m, **fields):
    from harness.jobs import new_job_id, validate
    job = validate({"name": "Morning check", "prompt": "check things", "cron": "0 8 * * *", **fields},
                   m.cfg.projects, m.cfg.models)
    job["id"] = new_job_id()
    job["next_run_at"] = fields.get("next_run_at", time.time() - 5)
    m.db.insert_job(job)
    return m.db.get_job(job["id"])


def test_due_job_runs_once_skips_overlap_and_records_status(tmp_path):
    cfg = jobs_cfg(tmp_path)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="All services running.\nSTATUS: OK")]))
        await m.start(maintenance=False)
        job = add_job(m)
        now = time.time()
        started = m.jobs.tick(now)
        assert len(started) == 1
        s = m.db.get_session(started[0])
        assert s["job_id"] == job["id"] and s["title"].startswith("⏰ Morning check")
        assert "STATUS: OK" in s["context"][1]["content"]
        after = m.db.get_job(job["id"])
        assert after["last_session_id"] == s["id"] and after["next_run_at"] > now
        assert m.jobs.tick(now) == []  # not due again
        done = await wait_status(m, s["id"], "done")
        assert done["job_status"] == "ok"
        finished = events(m, s["id"], "run_finished")[0]
        assert finished["job_status"] == "ok" and finished["job_id"] == job["id"]

        # overlap: a due job whose last run is still active skips the slot
        m.db.update_job(job["id"], next_run_at=time.time() - 1)
        m.db.update_session(s["id"], status="running")
        assert m.jobs.tick(time.time()) == []
        skipped = m.db.get_job(job["id"])
        assert "still going" in skipped["last_skip"] and skipped["next_run_at"] > time.time()
        m.db.update_session(s["id"], status="done")
        await m.stop()
    asyncio.run(body())


def test_catch_up_after_downtime(tmp_path):
    cfg = jobs_cfg(tmp_path)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="ok\nSTATUS: OK")]))
        recent = add_job(m, name="recent", next_run_at=time.time() - 600, catch_up_minutes=60)
        stale = add_job(m, name="stale", next_run_at=time.time() - 86400, catch_up_minutes=60)
        await m.start(maintenance=False)  # start() runs the catch-up pass
        assert m.db.get_job(stale["id"])["next_run_at"] > time.time()
        assert "missed" in m.db.get_job(stale["id"])["last_skip"]
        started = m.jobs.tick(time.time())
        assert [m.db.get_session(sid)["job_id"] for sid in started] == [recent["id"]]
        for sid in started:
            await wait_status(m, sid, "done")
        await m.stop()
    asyncio.run(body())


def test_job_notifications_quiet_unless_attention(tmp_path):
    cfg = jobs_cfg(tmp_path)
    cfg.notify.enabled = True
    m = Manager(cfg, chat=Script([Completion(content="x")]))
    n = Notifier(cfg, m.db)
    quiet = add_job(m, name="quiet", notify="attention")
    low = add_job(m, name="low", notify="low")
    loud = add_job(m, name="loud", notify="always")
    sid = "s1"
    m.db.insert_session({"id": sid, "project": "scratch", "target": "tower", "model": "fake", "title": "t",
                         "status": "done", "workspace": "", "created_at": 0, "updated_at": 0, "context": []})

    def build(job, **data):
        return n.build({"session_id": sid, "type": "run_finished",
                        "data": {"status": "done", "stop_reason": "final_message", "answer": "fine", "job_id": job["id"],
                                 "job_status": "ok", "job_reason": "", **data}})

    assert build(quiet) is None
    assert build(low)["priority"] == 2 and build(loud)["priority"] == 3
    attention = build(quiet, job_status="attention", job_reason="ntfy is down")
    assert attention["priority"] == 4 and attention["title"] == "Needs attention: quiet" and attention["message"] == "ntfy is down"
    assert build(quiet, job_status="")["title"].startswith("Done (no status line)")
    assert build(quiet, status="failed", stop_reason="internal_error")["priority"] == 4
    assert build(quiet, status="cancelled") is None


def test_jobs_api(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    cfg = jobs_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="checked\nSTATUS: ATTENTION: disk nearly full")]))
    with TestClient(create_app(m)) as client:
        assert client.get("/jobs/preview", params={"cron": "0 8 * * *"}).json()["ok"]
        assert "outside" in client.get("/jobs/preview", params={"cron": "99 8 * * *"}).json()["error"]
        bad = client.post("/jobs", json={"name": "x", "prompt": "y", "cron": "nope"})
        assert bad.status_code == 400
        assert client.post("/jobs", json={"name": "x", "prompt": "y", "cron": "0 8 * * *", "project": "nope"}).status_code == 400
        job = client.post("/jobs", json={"name": "Disk check", "prompt": "check disk", "cron": "0 8 * * *",
                                         "notify": "attention"}).json()
        assert job["enabled"] is True and job["next_run_at"] > time.time()
        s = client.post(f"/jobs/{job['id']}/run").json()
        assert client.post(f"/jobs/{job['id']}/run").status_code in (201, 409)
        wait_until(lambda: client.get(f"/sessions/{s['id']}").json()["status"] == "done")
        detail = wait_until(lambda: (lambda d: d if d["recent"] and d["recent"][-1]["job_status"] else None)(
            client.get(f"/jobs/{job['id']}").json()))
        assert detail["recent"][-1]["job_status"] == "attention"
        assert detail["next_run_at"] == job["next_run_at"]  # run now doesn't move the schedule
        listed = client.get("/sessions").json()
        assert any(x.get("job_status") == "attention" for x in listed)
        updated = client.put(f"/jobs/{job['id']}", json={"name": "Disk check", "prompt": "check disk",
                                                         "cron": "0 9 * * 1-5", "enabled": False}).json()
        assert updated["cron"] == "0 9 * * 1-5" and updated["enabled"] is False
        assert client.delete(f"/jobs/{job['id']}").status_code == 204
        assert client.get(f"/jobs/{job['id']}").status_code == 404


# 7e: documents and the web fixture
import io  # noqa: E402
import zipfile  # noqa: E402

import httpx  # noqa: E402

from harness.config import WebConfig  # noqa: E402
from harness.fileops import ToolError  # noqa: E402
from harness.web_fixture import Fixture  # noqa: E402
from harness.web_tools import WebTools  # noqa: E402


def make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of Helvetica text per page (no PDF library needed)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content_id = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_docx(paragraphs: list[tuple[str, str]], table: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = ""
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body += f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"
    body += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
                                + "</w:tr>" for row in table) + "</w:tbl>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
        z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:cp="x" xmlns:dc="y"><dc:title>Rink schedule</dc:title>'
                                        "</cp:coreProperties>")
    return buf.getvalue()


def public(host, port):
    async def resolve(h, p):
        return ["93.184.216.34"]
    return resolve(host, port)


def test_fetch_reads_pdf_and_docx(tmp_path):
    pdf = make_pdf(["Transformer base model uses 8 attention heads", "d_model is 512 on page two"])
    docx = make_docx([("Heading1", "Open skate"), ("", "Sundays 10:00 at the Kent rink")], [["Day", "Time"], ["Sun", "10:00"]])
    blank = make_pdf(["", ""])

    def handler(request):
        if request.url.path == "/paper":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        if request.url.path == "/scan":
            return httpx.Response(200, content=blank, headers={"content-type": "application/pdf"})
        if request.url.path == "/octet":  # a PDF served with a generic type
            return httpx.Response(200, content=pdf, headers={"content-type": "application/octet-stream"})
        if request.url.path == "/schedule.docx":
            return httpx.Response(200, content=docx, headers={"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    web = WebTools(WebConfig(enabled=True, max_bytes=100, max_document_bytes=10**6), resolver=public,
                   transport=httpx.MockTransport(handler))

    async def body():
        text = await web.web_fetch("https://example.com/paper")
        assert "--- page 1 of 2 ---" in text and "8 attention heads" in text and "d_model is 512" in text
        found = await web.web_fetch("https://example.com/paper", find="d_model")
        assert "page 2 of 2" in found
        assert "8 attention heads" in await web.web_fetch("https://example.com/octet")
        with pytest.raises(ToolError, match="no text layer"):
            await web.web_fetch("https://example.com/scan")
        doc = await web.web_fetch("https://example.com/schedule.docx")
        assert "# Rink schedule" in doc and "# Open skate" in doc and "Sundays 10:00" in doc and "Sun | 10:00" in doc
        with pytest.raises(ToolError, match="only HTML, text, PDF"):
            await web.web_fetch("https://example.com/logo.png")
    asyncio.run(body())


def test_fixture_records_and_replays_without_network(tmp_path):
    root = tmp_path / "fixture"
    fx = Fixture(root)
    fx.add_search("llama.cpp sleep idle seconds", {"results": [
        {"url": "https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md", "title": "llama.cpp server",
         "content": "HTTP server", "score": 2}]})
    fx.add_page("https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md",
                "https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md", "text/plain",
                b"--sleep-idle-seconds: /health, /props, /models and /metrics don't wake the server")
    fx.add_page("https://arxiv.org/pdf/1706.03762", "https://arxiv.org/pdf/1706.03762v7", "application/pdf",
                make_pdf(["Attention Is All You Need", "h = 8 parallel attention layers"]))
    fx.save()

    def no_network(request):
        raise AssertionError(f"network used: {request.url}")

    web = WebTools(WebConfig(enabled=True, fixture_dir=str(root)))
    assert web.transport is not None and web.fixture is not None

    async def body():
        out = await web.web_search("llama.cpp server sleep idle")   # not recorded verbatim: closest query
        assert "llama.cpp server" in out
        assert "No results" in await web.web_search("best pizza in akron")
        page = await web.web_fetch("https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md")
        assert "/metrics don't wake" in page
        pdf = await web.web_fetch("https://arxiv.org/pdf/1706.03762")  # redirect recorded
        assert "final URL: https://arxiv.org/pdf/1706.03762v7" in pdf and "h = 8" in pdf
        with pytest.raises(ToolError, match="HTTP 404"):
            await web.web_fetch("https://example.org/not-recorded")
    asyncio.run(body())
    assert web.fixture.misses == ["search: best pizza in akron", "fetch: https://example.org/not-recorded"]
