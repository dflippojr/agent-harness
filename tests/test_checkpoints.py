"""Per-turn checkpoints (#261) through the Manager: take, rewind, fork, hosted, quota, cleanup. No Docker or GPU."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

import pytest

from harness import checkpoints, storage
from harness.llm import Completion
from harness.manager import HarnessError, Manager

from test_daemon import Script, call, events, make_cfg, wait_status
from test_phase3 import finished, make_repo, project_cfg, sh


def two_edits() -> Script:
    return Script([
        Completion(tool_calls=[call("write_file", 0, path="app.py", content="VALUE = 2\n")]),
        Completion(tool_calls=[call("write_file", 0, path="extra.txt", content="late\n")]),
        Completion(content="All done."),
    ])


async def started(tmp_path, project="proj", script=None):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))
    m = Manager(cfg, chat=script or two_edits())
    await m.start(maintenance=False)
    s = await finished(m, m.create("go", project=project)["id"])
    return m, s


def test_checkpoint_per_mutating_turn_and_rewind(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid, ws = s["id"], Path(s["workspace"])
        assert [c["turn"] for c in m.db.checkpoints(sid)] == [1, 2]
        assert s["turn_seq"] == 2
        assert len(events(m, sid, "checkpoint")) == 2
        head_before = sh(ws, "rev-parse", "agent/" + sid)
        index_before = sh(ws, "diff", "--cached", "--name-only")
        assert (ws / "extra.txt").exists()
        assert checkpoints.REF_PREFIX not in sh(ws, "for-each-ref")        # hidden refs live outside the workspace
        assert sh(ws, "rev-parse", "agent/" + sid) == head_before and index_before == ""

        rewound = await m.rewind(sid, 1)
        assert (ws / "app.py").read_text() == "VALUE = 2\n"
        assert not (ws / "extra.txt").exists()
        assert len([x for x in rewound["context"] if x["role"] == "assistant"]) == 1
        assert [c["turn"] for c in m.db.checkpoints(sid)] == [1]            # later ones hidden, not deleted
        assert [c["turn"] for c in m.db.checkpoints(sid, hidden=True)] == [2]
        assert events(m, sid, "rewound")[-1]["turn"] == 1
        assert len(events(m, sid, "tool_call")) >= 2                        # the event log keeps the history

        again = await m.rewind(sid, 2)                                      # redo while still retained
        assert (ws / "extra.txt").exists() and len(again["context"]) > len(rewound["context"])

    asyncio.run(body())


def test_next_mutating_turn_replaces_hidden_checkpoints(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        await m.rewind(sid, 1)
        await m.send(sid, "again")
        s = await finished(m, sid)
        turns = [c["turn"] for c in m.db.checkpoints(sid, hidden=None)]
        assert turns == sorted(set(turns)) and not m.db.checkpoints(sid, hidden=True)
        assert s["turn_seq"] == turns[-1]

    asyncio.run(body())


def test_fork_runs_independently_with_its_own_branch(tmp_path):
    async def body():
        script = two_edits()
        m, s = await started(tmp_path, script=script)
        sid = s["id"]
        fork = await m.fork(sid, 1, "do something else")
        fid = fork["id"]
        fork = await finished(m, fid)
        assert fork["parent_id"] == sid and fork["fork_turn"] == 1
        assert fork["branch"] == f"agent/{fid}" != s["branch"]
        assert fork["base_commit"] == s["base_commit"] and fork["workspace"] != s["workspace"]
        fws, pws = Path(fork["workspace"]), Path(s["workspace"])
        assert (pws / "extra.txt").exists()
        assert (fws / "app.py").read_text() == "VALUE = 2\n"
        assert events(m, fid, "forked")[0]["parent"] == sid
        assert sh(pws, "rev-parse", "HEAD") == sh(pws, "rev-parse", "agent/" + sid)
        assert any(x.get("content") == "do something else" for x in fork["context"])
        # the parent can be reviewed on its own
        assert (await m.review(sid, "discard"))["review"] == "discarded"
        assert m.db.get_session(fid)["workspace_removed"] == 0

    asyncio.run(body())


def test_scratch_session_checkpoints_and_rewinds(tmp_path):
    async def body():
        script = Script([
            Completion(tool_calls=[call("write_file", 0, path="a.txt", content="one\n")]),
            Completion(tool_calls=[call("write_file", 0, path="a.txt", content="two\n")]),
            Completion(content="ok"),
        ])
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start(maintenance=False)
        s = await finished(m, m.create("go")["id"])
        ws = Path(s["workspace"])
        assert not (ws / ".git").exists()
        await m.rewind(s["id"], 1)
        assert (ws / "a.txt").read_text() == "one\n"
        assert not (ws / ".git").exists()
        fork = await finished(m, (await m.fork(s["id"], 1, "next"))["id"])
        first = m.db.checkpoints(fork["id"])[0]          # the fork's own copy of the checkpoint it started from
        store = m.runner.checkpointer.store(fork)
        assert store._git(None, None, "show", f"{first['sha']}:a.txt").out == "one\n"

    asyncio.run(body())


def test_agent_tampering_with_git_refs_cannot_touch_checkpoints(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        ws = Path(s["workspace"])
        subprocess.run(["git", "-C", str(ws), "for-each-ref", "--format=%(refname)"], check=True)
        for ref in ("refs/heads", "refs/harness"):
            target = ws / ".git" / ref
            if target.is_dir():
                for child in target.rglob("*"):
                    if child.is_file():
                        child.unlink()
        await m.rewind(s["id"], 1)         # head recorded with the checkpoint is still in the object store
        assert sh(ws, "rev-parse", "--abbrev-ref", "HEAD") == "agent/" + s["id"]

    asyncio.run(body())


def test_hosted_session_is_fork_only(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        m.db.update_session(sid, backend="claude")
        listing = m.checkpoints(sid)
        assert listing["can_rewind"] is False and listing["can_fork"] is True
        with pytest.raises(HarnessError) as e:
            await m.rewind(sid, 1)
        assert e.value.status == 409
        fork = m.db.get_session((await m.fork(sid, 1, "carry on"))["id"])
        assert "Summary of the earlier conversation" in fork["context"][1]["content"]
        assert not fork["run"].get("backend_session_id")
        assert "no model call" in events(m, fork["id"], "forked")[0]["summary_note"]

    asyncio.run(body())


def test_cap_prunes_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.checkpointer.CAP", 1)

    async def body():
        m, s = await started(tmp_path)
        assert [c["turn"] for c in m.db.checkpoints(s["id"])] == [2]
        store = m.runner.checkpointer.store(s)
        assert not store._git(None, None, "rev-parse", "--verify", "-q",
                              checkpoints.ref_name(s["id"], 1), check=False).out.strip()

    asyncio.run(body())


def test_over_quota_prunes_then_skips_without_failing_the_turn(tmp_path, monkeypatch):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        cp = m.runner.checkpointer
        usage = {"bytes": 10}
        monkeypatch.setattr("harness.storage.account_usage_bytes", lambda cfg, uid: usage["bytes"])
        monkeypatch.setattr("harness.checkpointer.CAP", 50)
        member = {**s, "owner_id": "member1"}
        monkeypatch.setattr(m.db, "account_by_id", lambda uid: {"disk_quota_bytes": 100})
        store = cp.store(member)
        # Over quota, but pruning every older checkpoint would fit: it prunes and keeps the new one.
        usage["bytes"] = 150
        monkeypatch.setattr("harness.fileops.dir_size", lambda p: 100)
        monkeypatch.setattr(store, "reclaim", lambda: usage.update(bytes=50), raising=False)
        assert cp._within_quota(member, store, sid, keep=2) is True
        # Even an empty store would not fit: skipped, never raises.
        monkeypatch.setattr("harness.fileops.dir_size", lambda p: 10)
        usage["bytes"] = 500
        assert cp._within_quota(member, store, sid, keep=2) is False

    asyncio.run(body())


def test_disk_view_and_usage_count_checkpoints_and_cleanup_removes_them(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        base = storage.checkpoints_dir(m.cfg, "owner") / sid
        assert base.is_dir()
        usage = await m.maintenance.usage()
        assert usage["checkpoints"][0]["session"] == sid and usage["checkpoints_mb"] >= 0
        measured = storage.account_usage_bytes(m.cfg, "owner")
        assert measured >= sum(p.stat().st_size for p in base.rglob("*") if p.is_file())
        m.maintenance.remove_workspace(sid)
        assert not base.exists() and not m.db.checkpoints(sid, hidden=None)

    asyncio.run(body())


def test_rewind_and_fork_refuse_active_and_runner_sessions(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        m.db.update_session(sid, status="running")
        for action in (m.rewind(sid, 1), m.fork(sid, 1, "again")):
            with pytest.raises(HarnessError) as e:
                await action
            assert e.value.status == 409 and "idle" in str(e.value)
        m.db.update_session(sid, status="done", target="mac")
        with pytest.raises(HarnessError) as e:
            await m.rewind(sid, 1)
        assert e.value.status == 409 and "tower" in str(e.value)
        assert m.checkpoints(sid)["can_rewind"] is False

    asyncio.run(body())


def test_rewind_after_compaction_restores_the_saved_context(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid = s["id"]
        saved = checkpoints.Store(storage.checkpoints_dir(m.cfg, "owner") / sid).load_context(1)
        m.db.update_session(sid, context=[s["context"][0], {"role": "user", "content": "[summary of everything]"}])
        rewound = await m.rewind(sid, 1)
        assert rewound["context"] == saved and "[summary of everything]" not in str(rewound["context"])

    asyncio.run(body())


def test_take_reports_a_reason_code_never_the_reason_text(tmp_path, monkeypatch):
    async def body():
        m, s = await started(tmp_path)
        secret = str(Path(s["workspace"]) / "private-path")

        def fail(*args, **kwargs):
            raise checkpoints.GitError(f"git add failed in {secret}")
        monkeypatch.setattr(checkpoints.Store, "snapshot", fail)
        stats: dict = {}
        event = m.runner.checkpointer.take(s["id"], stats=stats)
        assert event["status"] == "skipped" and secret in event["reason"]     # the user sees the detail
        assert stats == {"turn": 3, "skipped": "snapshot_failed"}            # the trace span does not

    asyncio.run(body())


def test_fork_writes_through_the_writer_without_blocking_the_loop(tmp_path, monkeypatch):
    async def body():
        m, s = await started(tmp_path)
        loop_thread, blocking = threading.current_thread(), []
        real = type(m.db).write

        def spy(db, fn, *args, **kwargs):
            if threading.current_thread() is loop_thread:
                blocking.append(getattr(fn, "__name__", repr(fn)))
            return real(db, fn, *args, **kwargs)
        monkeypatch.setattr(type(m.db), "write", spy)
        fork = await m.fork(s["id"], 1, "carry on")
        monkeypatch.undo()
        assert blocking == []                                               # #294: awrite from async code
        assert [c["turn"] for c in m.db.checkpoints(fork["id"])] == [1]
        assert events(m, fork["id"], "forked")[-1]["turn"] == 1
        await finished(m, fork["id"])

    asyncio.run(body())
