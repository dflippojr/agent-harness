"""Per-turn checkpoints (#261) through the Manager: take, rewind, fork, hosted, quota, cleanup. No Docker or GPU."""

from __future__ import annotations

import asyncio
import os
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


def test_rewind_restores_a_detached_head_without_moving_the_branch(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid, ws, branch = s["id"], Path(s["workspace"]), "agent/" + s["id"]
        sh(ws, "checkout", "-q", "--detach")
        (ws / "detached.txt").write_text("on a detached HEAD\n")
        sh(ws, "add", "detached.txt")
        sh(ws, "commit", "-qm", "detached work")
        detached = sh(ws, "rev-parse", "HEAD")
        event = await asyncio.to_thread(m.runner.checkpointer.take, sid)
        ckpt = m.runner.checkpointer.checkpoint(sid, event["turn"])
        assert ckpt["head"] == detached and ckpt["branch"] == ""          # detached: no branch, just the commit
        sh(ws, "checkout", "-q", "-f", branch)
        tip = sh(ws, "rev-parse", branch)

        await m.rewind(sid, event["turn"])
        assert sh(ws, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"       # detached again, at the recorded commit
        assert sh(ws, "rev-parse", "HEAD") == detached
        assert sh(ws, "rev-parse", branch) == tip                           # the session branch did not move
        assert (ws / "detached.txt").read_text() == "on a detached HEAD\n"

        await m.rewind(sid, 2)                                              # an attached checkpoint re-attaches
        assert sh(ws, "rev-parse", "--abbrev-ref", "HEAD") == branch and not (ws / "detached.txt").exists()

    asyncio.run(body())


def test_over_quota_skip_leaves_the_rewound_past_checkpoints_redoable(tmp_path, monkeypatch):
    async def body():
        m, s = await started(tmp_path)
        sid, ws = s["id"], Path(s["workspace"])
        await m.rewind(sid, 1)
        (ws / "after-rewind.txt").write_text("new work\n")
        monkeypatch.setattr("harness.principal.OWNER_USER_ID", "nobody")      # measure this account like a member's
        monkeypatch.setattr(m.db, "account_by_id", lambda uid: {"disk_quota_bytes": 100})
        monkeypatch.setattr("harness.storage.account_usage_bytes", lambda cfg, uid: 10 ** 9)
        event = await asyncio.to_thread(m.runner.checkpointer.take, sid)
        monkeypatch.undo()
        assert event["status"] == "skipped" and "quota" in event["reason"]
        assert [c["turn"] for c in m.db.checkpoints(sid, hidden=True)] == [2]   # nothing existing was touched
        assert m.db.get_session(sid)["turn_seq"] == 1
        await m.rewind(sid, 2)                                                  # redo still works
        assert (ws / "extra.txt").read_text() == "late\n"

        await m.rewind(sid, 1)                                                  # with room, the new turn 2 replaces it
        (ws / "after-rewind.txt").write_text("new work\n")
        event = await asyncio.to_thread(m.runner.checkpointer.take, sid)
        assert event["turn"] == 2 and not m.db.checkpoints(sid, hidden=True)
        await m.rewind(sid, 1)
        await m.rewind(sid, 2)
        assert (ws / "after-rewind.txt").exists() and not (ws / "extra.txt").exists()

    asyncio.run(body())


def test_nested_repositories_are_checkpointed_as_files_and_rewound(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        sid, ws, cp = s["id"], Path(s["workspace"]), m.runner.checkpointer
        make_repo(ws / "libs" / "clone")                                   # a clone with history
        subprocess.run(["git", "init", "-q", str(ws / "fresh")], check=True)
        (ws / "fresh" / "notes.txt").write_text("no commit yet\n")          # git add alone refuses this one
        event = await asyncio.to_thread(cp.take, sid)
        assert event["turn"] == 3
        sha = cp.checkpoint(sid, 3)["sha"]
        store = cp.store(s)
        assert store._git(None, None, "show", f"{sha}:libs/clone/app.py").out == "VALUE = 1\n"
        assert store._git(None, None, "show", f"{sha}:fresh/notes.txt").out == "no commit yet\n"

        (ws / "libs" / "clone" / "app.py").write_text("edited\n")
        await m.rewind(sid, 3)                                              # a file inside the clone is restored
        assert (ws / "libs" / "clone" / "app.py").read_text() == "VALUE = 1\n"
        assert (ws / "libs" / "clone" / ".git").is_dir()

        await m.rewind(sid, 2)                                              # before the clone: it is gone
        assert not (ws / "libs").exists() and not (ws / "fresh").exists()
        assert (ws / "extra.txt").exists()

        await m.rewind(sid, 3)                                              # after it: its files come back
        assert (ws / "libs" / "clone" / "app.py").read_text() == "VALUE = 1\n"
        assert (ws / "fresh" / "notes.txt").read_text() == "no commit yet\n"
        fork = await m.fork(sid, 3, "carry on")
        assert (Path(fork["workspace"]) / "libs" / "clone" / "app.py").read_text() == "VALUE = 1\n"
        await finished(m, fork["id"])

    asyncio.run(body())


def test_a_turn_that_only_generates_an_image_is_checkpointed(tmp_path):
    from test_phase6 import image_manager
    steps = [Completion(tool_calls=[call("generate_image", 0, prompt="app icon", filename="assets/icon")]),
             Completion(content="made the icon")]

    async def body():
        m, _, _ = image_manager(tmp_path, steps=steps)
        await m.start(maintenance=False)
        sid = m.create("make an icon")["id"]
        await wait_status(m, sid, "done", timeout=20)
        assert [c["turn"] for c in m.db.checkpoints(sid)] == [1]           # the PNG is in the workspace
        store = m.runner.checkpointer.store(m.db.get_session(sid))
        sha = m.db.checkpoints(sid)[0]["sha"]
        assert "assets/icon.png" in store._git(None, None, "ls-tree", "-r", "--name-only", sha).out
        await m.stop()

    asyncio.run(body())


async def later_work(tmp_path):
    """A session two turns in, then a commit, an edit, a new file and a staged one: everything a rewind to
    checkpoint 1 must change, and must leave exactly as it was when it fails."""
    m, s = await started(tmp_path)
    sid, ws = s["id"], Path(s["workspace"])
    (ws / "committed.txt").write_text("committed later\n")
    sh(ws, "add", "committed.txt")
    sh(ws, "commit", "-qm", "later commit")                             # the branch moved past checkpoint 1
    (ws / "app.py").write_text("VALUE = 3\n")
    (ws / "b.txt").write_text("later\n")
    sh(ws, "add", "b.txt")

    def state():
        files = {p.relative_to(ws).as_posix(): p.read_text() for p in ws.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(ws).parts}
        row = m.db.get_session(sid)
        return (files, sh(ws, "symbolic-ref", "HEAD"), sh(ws, "rev-parse", "HEAD"),
                sh(ws, "diff", "--cached", "--name-only"), sh(ws, "status", "--porcelain"), row["context"],
                row["turn_seq"], [c["turn"] for c in m.db.checkpoints(sid)], len(events(m, sid, "rewound")))
    return m, sid, ws, state


def test_rewind_that_fails_midway_puts_everything_back(tmp_path, monkeypatch):
    async def body():
        m, sid, ws, state = await later_work(tmp_path)
        before = state()
        unlink = Path.unlink

        def locked_unlink(self, *args, **kwargs):                       # passes the probe, then cannot be removed
            if self.name == "extra.txt":
                raise PermissionError(13, "The process cannot access the file", str(self))
            return unlink(self, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", locked_unlink)

        with pytest.raises(HarnessError) as e:
            await m.rewind(sid, 1)
        assert e.value.status == 409
        assert "extra.txt" in str(e.value) and "nothing was rewound" in str(e.value)
        assert state() == before                                         # files, branch, index, context, turn_seq
        store = m.runner.checkpointer.store(m.db.get_session(sid))
        assert checkpoints.UNDO_PREFIX not in store._git(None, None, "for-each-ref").out

        monkeypatch.setattr(Path, "unlink", unlink)                      # unlocked: the same rewind goes through
        await m.rewind(sid, 1)
        assert not (ws / "extra.txt").exists() and m.db.get_session(sid)["turn_seq"] == 1

    asyncio.run(body())


def test_rewind_refuses_up_front_when_a_file_is_locked(tmp_path, monkeypatch):
    async def body():
        m, sid, ws, state = await later_work(tmp_path)
        before = state()
        rename, touched = os.rename, []

        def locked_rename(src, dst, *args, **kwargs):                   # Windows: open without delete sharing
            if Path(src).name == "extra.txt":
                raise PermissionError(13, "The process cannot access the file", str(src))
            return rename(src, dst, *args, **kwargs)
        monkeypatch.setattr(os, "rename", locked_rename)
        monkeypatch.setattr(checkpoints, "reset_branch", lambda *a: touched.append(a))

        with pytest.raises(HarnessError) as e:
            await m.rewind(sid, 1)
        assert e.value.status == 409 and "extra.txt" in str(e.value)
        assert touched == [] and state() == before                       # refused before touching anything

    asyncio.run(body())


def objects(store) -> list[str]:
    """Every object in the checkpoint repository, reachable or not."""
    return store._git(None, None, "cat-file", "--batch-all-objects", "--batch-check").out.splitlines()


def test_unchanged_hosted_runs_write_no_objects(tmp_path):
    async def body():
        m, s = await started(tmp_path)
        cp = m.runner.checkpointer
        store = cp.store(s)
        before = objects(store)
        for _ in range(20):                                              # a hosted run that changed nothing
            stats: dict = {}
            assert cp.take(s["id"], only_if_changed=True, stats=stats) is None
            assert stats["skipped"] == "unchanged"
        assert objects(store) == before
        (Path(s["workspace"]) / "new.txt").write_text("changed\n")         # a change is still recorded
        assert cp.take(s["id"], only_if_changed=True)["turn"] == 3

    asyncio.run(body())


def test_unrecorded_snapshot_objects_are_reclaimed(tmp_path, monkeypatch):
    async def body():
        m, s = await started(tmp_path)
        cp = m.runner.checkpointer
        store = cp.store(s)
        store.reclaim()
        before = objects(store)
        (Path(s["workspace"]) / "new.txt").write_text("never kept\n")      # new blob, tree and commit

        def fail(*args, **kwargs):
            raise checkpoints.GitError("cannot pack the context")
        monkeypatch.setattr(checkpoints.Store, "pack_context", fail)
        assert cp.take(s["id"])["status"] == "skipped"
        assert objects(store) == before

    asyncio.run(body())


def assert_not_checkpointed(m, s):
    """The run succeeded, each mutating turn says it was not checkpointed, and nothing of a checkpoint remains."""
    sid = s["id"]
    assert s["status"] == "done"
    assert [e.get("status") for e in events(m, sid, "checkpoint")] == ["skipped", "skipped"]
    assert m.db.checkpoints(sid, hidden=None) == [] and not s["turn_seq"]
    store = m.runner.checkpointer.store(s)
    assert checkpoints.REF_PREFIX not in store._git(None, None, "for-each-ref").out
    assert not any(store.contexts.iterdir())
    assert not [o for o in objects(store) if " commit " in o]


def test_a_failed_context_write_does_not_fail_the_turn(tmp_path, monkeypatch):
    write_bytes = Path.write_bytes

    def disk_full(self, data):
        if self.name.endswith(".json.gz"):
            write_bytes(self, data[:10])                                 # a partial file, then the error
            raise OSError(28, "No space left on device", str(self))
        return write_bytes(self, data)
    monkeypatch.setattr(Path, "write_bytes", disk_full)

    async def body():
        m, s = await started(tmp_path)
        assert_not_checkpointed(m, s)

    asyncio.run(body())


def test_a_failed_checkpoint_record_does_not_fail_the_turn(tmp_path, monkeypatch):
    import sqlite3
    from harness.checkpointer import Checkpointer
    record = Checkpointer._record

    def locked(self, *args):
        record(self, *args)                                              # rolled back with the transaction
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(Checkpointer, "_record", locked)

    async def body():
        m, s = await started(tmp_path)
        assert_not_checkpointed(m, s)

    asyncio.run(body())


def test_a_failed_quota_check_after_keeping_does_not_fail_the_turn(tmp_path, monkeypatch):
    from harness.checkpointer import Checkpointer

    def broken(*args):
        raise OSError(5, "I/O error measuring the account")
    monkeypatch.setattr(Checkpointer, "_within_quota", broken)

    async def body():
        m, s = await started(tmp_path)
        assert_not_checkpointed(m, s)

    asyncio.run(body())


def test_a_write_that_trips_the_quota_stop_is_still_checkpointed(tmp_path, monkeypatch):
    from harness.runner import Runner

    async def over(self, sid):                                       # the write put the session over its quota
        await self.aset_status(sid, "failed", stop_reason="quota_exceeded: test")
        return True
    monkeypatch.setattr(Runner, "_over_quota", over)
    script = Script([Completion(tool_calls=[call("write_file", 0, path="big.bin", content="x" * 100)]),
                     Completion(content="never reached")])

    async def body():
        m, s = await started(tmp_path, script=script)
        sid = s["id"]
        assert s["status"] == "failed" and (Path(s["workspace"]) / "big.bin").exists()
        assert [c["turn"] for c in m.db.checkpoints(sid)] == [1]
        assert [e.get("turn") for e in events(m, sid, "checkpoint")] == [1]
        sha = m.db.checkpoints(sid)[0]["sha"]
        store = m.runner.checkpointer.store(s)
        assert "big.bin" in store._git(None, None, "ls-tree", "-r", "--name-only", sha).out

    asyncio.run(body())


def test_a_mutating_call_the_policy_blocked_is_not_checkpointed(tmp_path, monkeypatch):
    from harness.policy import DENY, Decision
    from harness.runner import Runner
    monkeypatch.setattr(Runner, "_decide", lambda self, s, name, args: Decision(DENY, "test"))
    script = Script([Completion(tool_calls=[call("write_file", 0, path="app.py", content="VALUE = 2\n")]),
                     Completion(content="All done.")])

    async def body():
        m, s = await started(tmp_path, script=script)
        assert s["status"] == "done"
        assert m.db.checkpoints(s["id"], hidden=None) == [] and events(m, s["id"], "checkpoint") == []

    asyncio.run(body())


def test_a_failed_take_after_a_rewind_leaves_the_rewound_past_checkpoint_redoable(tmp_path, monkeypatch):
    write_bytes = Path.write_bytes

    def disk_full(self, data):
        if self.name.endswith(".json.gz"):
            raise OSError(28, "No space left on device", str(self))
        return write_bytes(self, data)

    async def body():
        m, s = await started(tmp_path)
        sid, ws = s["id"], Path(s["workspace"])
        cp = m.runner.checkpointer
        store = cp.store(s)
        await m.rewind(sid, 1)
        hidden = m.db.checkpoints(sid, hidden=True)
        (ws / "after-rewind.txt").write_text("new work\n")
        monkeypatch.setattr(Path, "write_bytes", disk_full)          # the new turn 2's context cannot be written
        event = await asyncio.to_thread(cp.take, sid)
        monkeypatch.undo()
        assert event["status"] == "skipped"
        assert m.db.checkpoints(sid, hidden=True) == hidden and m.db.get_session(sid)["turn_seq"] == 1
        refs = store._git(None, None, "for-each-ref", "--format=%(refname) %(objectname)").out.split("\n")
        assert f"{checkpoints.ref_name(sid, 2)} {hidden[0]['sha']}" in refs
        assert sorted(p.name for p in store.contexts.iterdir()) == ["1.json.gz", "2.json.gz"]
        (ws / "after-rewind.txt").unlink()
        await m.rewind(sid, 2)                                       # redo still works
        assert (ws / "extra.txt").read_text() == "late\n"

    asyncio.run(body())
