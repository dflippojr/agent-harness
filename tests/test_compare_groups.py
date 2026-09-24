"""Issue #166: compare groups start one session per backend choice and pick or discard as a unit."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from harness import projects
from harness.config import BackendConfig, ModelConfig, Project
from harness.manager import HarnessError, Manager
from harness.llm import Completion
from harness.runner import ACTIVE

from test_daemon import Script, call, make_cfg, wait_status
from test_phase3 import make_repo, sh


def make_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.projects["repo"] = Project(name="repo", repo="https://example.invalid/r.git")
    for name in ("claude", "codex"):
        cfg.backends[name] = BackendConfig(enabled=True, model="m-" + name)
    m = Manager(cfg, chat=Script([Completion(content="x")]))
    m._spawn = lambda sid: None
    return m


def run(coro):
    return asyncio.run(coro)


CHOICES = [{"backend": "claude"}, {"backend": "codex"}]


def test_group_starts_one_session_per_choice(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("do it", CHOICES, "repo"))
    assert [r["backend"] for r in view["members"]] == ["claude", "codex"]
    ids = [r["id"] for r in view["members"]]
    assert {m.db.get_session(i)["compare_group"] for i in ids} == {view["group"]}
    assert len({m.db.get_session(i)["workspace"] for i in ids}) == 2


def test_group_size_project_and_owner_rules(tmp_path):
    m = make_manager(tmp_path)
    for bad in ([], CHOICES[:1], CHOICES * 3):
        with pytest.raises(HarnessError) as e:
            run(m.create_compare("p", bad, "repo"))
        assert e.value.status == 400
    with pytest.raises(HarnessError):
        run(m.create_compare("p", CHOICES, "scratch"))
    with pytest.raises(HarnessError) as e:
        run(m.create_compare("p", CHOICES, "repo", owner_id="someone"))
    assert e.value.status == 403


def test_reported_limit_refuses_but_unknown_usage_allows(tmp_path):
    m = make_manager(tmp_path)
    m.db.set_backend_usage("codex", {"status": "rejected"})
    with pytest.raises(HarnessError) as e:
        run(m.create_compare("p", CHOICES, "repo"))
    assert e.value.status == 429
    assert m.db.list_sessions() == []
    m.db.set_backend_usage("codex", {})
    run(m.create_compare("p", CHOICES, "repo"))


def test_pick_reviews_winner_and_discards_rest(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    calls = []

    async def fake_review(sid, action):
        calls.append((sid, action))
        m.db.update_session(sid, review={"merge": "merged", "push": "pushed", "discard": "discarded"}[action])

    m.review = fake_review
    a, b = (r["id"] for r in view["members"])
    out = asyncio.run(m.compare_pick(view["group"], a, "merge", True))
    assert calls == [(a, "merge"), (b, "discard")]
    assert [r["review"] for r in out["members"]] == ["merged", "discarded"]
    with pytest.raises(HarnessError):
        asyncio.run(m.compare_pick(view["group"], "nope", "merge", False))


def test_failed_later_member_discards_the_earlier_ones(tmp_path):
    m = make_manager(tmp_path)
    m.cfg.backends["codex"].enabled = False
    discarded = []

    async def fake_review(sid, action):
        discarded.append((sid, action))
        m.db.update_session(sid, review="discarded")

    m.review = fake_review
    with pytest.raises(HarnessError):
        run(m.create_compare("p", CHOICES, "repo"))
    (s,) = m.db.list_sessions()
    s = m.db.get_session(s["id"])
    assert discarded == [(s["id"], "discard")]
    assert s["status"] == "cancelled" and s["compare_group"] == ""


def test_rollback_covers_non_harness_errors_and_failed_discard(tmp_path):
    m = make_manager(tmp_path)
    real, calls = m.create, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*a, **kw)

    async def bad_review(sid, action):
        raise HarnessError(409, "never checked out")

    m.create, m.review = flaky, bad_review
    with pytest.raises(RuntimeError):
        run(m.create_compare("p", CHOICES, "repo"))
    (s,) = m.db.list_sessions()
    s = m.db.get_session(s["id"])
    assert s["review"] == "discarded" and s["workspace_removed"] and s["compare_group"] == ""


def test_pick_retry_skips_merged_winner_and_reports_discard_failures(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    calls, fail = [], {"on": True}

    async def fake_review(sid, action):
        calls.append((sid, action))
        if action == "discard" and fail["on"]:
            raise HarnessError(500, "disk")
        m.db.update_session(sid, review={"merge": "merged", "discard": "discarded"}[action])

    m.review = fake_review
    with pytest.raises(HarnessError) as e:
        run(m.compare_pick(view["group"], a, "merge", True))
    assert b in str(e.value) and m.db.get_session(a)["review"] == "merged"
    fail["on"] = False
    out = run(m.compare_pick(view["group"], a, "merge", True))
    assert calls == [(a, "merge"), (b, "discard"), (b, "discard")]
    assert [r["review"] for r in out["members"]] == ["merged", "discarded"]


@pytest.mark.parametrize("action", ["merge", "push"])
def test_pick_that_does_not_complete_discards_nobody(tmp_path, action):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    calls = []

    async def fake_review(sid, act):
        calls.append((sid, act))
        # a conflicted merge returns normally with review="" ; a failed push raises
        if act == "push":
            raise HarnessError(502, "push rejected")
        m.db.update_session(sid, review="", review_detail="merge conflict in a.py")

    m.review = fake_review
    with pytest.raises(HarnessError) as e:
        run(m.compare_pick(view["group"], a, action, True))
    assert calls == [(a, action)]
    if action == "merge":
        assert "did not complete" in str(e.value) and "conflict" in str(e.value)
    assert m.db.get_session(b)["review"] != "discarded"


def test_pick_after_conflict_can_succeed_on_retry(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    state = {"ok": False}

    async def fake_review(sid, act):
        if act == "merge":
            m.db.update_session(sid, review="merged" if state["ok"] else "")
        else:
            m.db.update_session(sid, review="discarded")

    m.review = fake_review
    with pytest.raises(HarnessError):
        run(m.compare_pick(view["group"], a, "merge", True))
    state["ok"] = True
    out = run(m.compare_pick(view["group"], a, "merge", True))
    assert [r["review"] for r in out["members"]] == ["merged", "discarded"]


def test_group_discard_continues_past_a_failure(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    seen = []

    async def fake_review(sid, action):
        seen.append(sid)
        if sid == a:
            raise HarnessError(500, "disk")
        m.db.update_session(sid, review="discarded")

    m.review = fake_review
    with pytest.raises(HarnessError):
        run(m.compare_discard(view["group"]))
    assert seen == [a, b]


def test_rollback_falls_back_when_discard_fails_for_a_checked_out_member(tmp_path):
    m = make_manager(tmp_path)
    real, calls = m.create, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*a, **kw)

    async def broken_review(sid, action):
        raise HarnessError(502, "remote unreachable")  # not the never-checked-out refusal

    m.create, m.review = flaky, broken_review
    with pytest.raises(RuntimeError):
        run(m.create_compare("p", CHOICES, "repo"))
    (s,) = m.db.list_sessions()
    s = m.db.get_session(s["id"])
    assert s["status"] == "cancelled" and s["review"] == "discarded" and s["workspace_removed"]
    assert s["compare_group"] == ""

    # and a failing cleanup still ungroups the member instead of aborting the rollback
    m = make_manager(tmp_path / "again")
    calls.clear()
    real = m.create

    def remove_fails(sid):
        raise OSError("locked")

    m.create, m.review = flaky, broken_review
    m.maintenance.remove_workspace = remove_fails
    with pytest.raises(RuntimeError):
        run(m.create_compare("p", CHOICES, "repo"))
    (s,) = m.db.list_sessions()
    assert m.db.get_session(s["id"])["compare_group"] == ""


def test_group_discard_reports_a_member_that_cannot_be_stopped(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    real_cancel = m.cancel

    async def stuck(ref):
        if ref == a:
            raise HarnessError(500, "stuck")
        return await real_cancel(ref)

    m.cancel = stuck
    with pytest.raises(HarnessError) as e:
        run(m.compare_discard(view["group"]))
    assert a in str(e.value) and "stuck" in str(e.value) and b not in str(e.value)
    # the one that could not be stopped is left alone and retryable; the other was still discarded
    assert m.db.get_session(a)["status"] == "queued" and m.db.get_session(a)["review"] == ""
    assert m.db.get_session(b)["review"] == "discarded"
    m.cancel = real_cancel
    out = run(m.compare_discard(view["group"]))
    assert [r["review"] for r in out["members"]] == ["discarded", "discarded"]


def test_compare_rejects_duplicates_unknown_groups_and_actions(tmp_path):
    m = make_manager(tmp_path)
    with pytest.raises(HarnessError, match="must differ"):
        run(m.create_compare("p", [{"backend": "claude"}, {"backend": "claude"}], "repo"))
    for call_ in (lambda: m.compare_view("nope"), lambda: run(m.compare_discard("nope"))):
        with pytest.raises(HarnessError) as e:
            call_()
        assert e.value.status == 404
    view = run(m.create_compare("p", CHOICES, "repo"))
    with pytest.raises(HarnessError) as e:
        run(m.compare_pick(view["group"], view["members"][0]["id"], "rebase", True))
    assert e.value.status == 400


# Real review()/cancel() against members that are still running. review() refuses a session in ACTIVE, so every path
# that discards a member has to end its run first; these tests use a real git project and real runner tasks.
class Gated:
    """Chat stub: model `fake` finishes with a one-line edit; every other model blocks until `release` is set."""

    def __init__(self):
        self.release = asyncio.Event()

    async def __call__(self, model, messages, tools, on_delta=None, max_tokens=None, extra=None, timeout=0,
                       on_progress=None):
        if tools is None:
            return Completion(content="SUMMARY: did things", prompt_tokens=100, completion_tokens=10)
        if getattr(model, "name", model) == "fake":
            n = sum(1 for m in messages if m["role"] == "assistant")
            return [Completion(tool_calls=[call("write_file", 0, path="app.py", content="VALUE = 2\n")]),
                    Completion(content="Changed VALUE.")][min(n, 1)]
        await self.release.wait()
        return Completion(content="late")


LOCAL = [{"backend": "local", "model": m} for m in ("fake", "fake2", "fake3")]


def real_manager(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = make_cfg(tmp_path)
    cfg.projects["repo"] = Project(name="repo", repo=str(src))
    for name in ("fake2", "fake3"):
        cfg.models[name] = ModelConfig(name=name, base_url="http://unused", context_tokens=65536)
    return src, Manager(cfg, chat=Gated())


async def one_runs_one_queues(m, *sids):
    """The GPU runs one local session at a time: one takes it, the other waits behind it."""
    for _ in range(500):
        if sorted(m.db.get_session(s)["status"] for s in sids) == ["queued", "running"]:
            return
        await asyncio.sleep(0.02)
    raise AssertionError([m.db.get_session(s)["status"] for s in sids])


async def running_group(tmp_path, monkeypatch):
    """`fake` finishes first; of the other two, one holds the GPU (running) and the other waits behind it (queued)."""
    src, m = real_manager(tmp_path)
    gate, real_prepare = threading.Event(), projects.prepare

    def prepare(project, ws, sid):
        if m.db.get_session(sid)["model"] != "fake":
            gate.wait(10)  # the losers are not checked out until the winner has finished
        return real_prepare(project, ws, sid)

    monkeypatch.setattr(projects, "prepare", prepare)
    await m.start(maintenance=False)
    view = await m.create_compare("bump the value", LOCAL, "repo")
    a, b, c = (r["id"] for r in view["members"])
    try:
        await wait_status(m, a, "done")
    finally:
        gate.set()
    await one_runs_one_queues(m, b, c)
    return src, m, view["group"], a, b, c


def assert_gone(src, m, *sids):
    """Each member was stopped and fully discarded: nothing active, no branch, no workspace, nothing to resume."""
    for sid in sids:
        s = m.db.get_session(sid)
        assert s["status"] not in ACTIVE and s["review"] == "discarded" and s["workspace_removed"], sid
        assert not Path(s["workspace"]).exists() and sh(src, "branch", "--list", s["branch"]) == "", sid
    assert m.db.sessions_with_status(*ACTIVE) == [] and m.scheduler.holder is None
    assert not m.runner.user_cancelled


def test_pick_finished_winner_cancels_and_discards_running_losers(tmp_path, monkeypatch):
    async def body():
        src, m, group, a, b, c = await running_group(tmp_path, monkeypatch)
        out = await m.compare_pick(group, a, "merge", True)
        assert (src / "app.py").read_text() == "VALUE = 2\n"
        assert [r["review"] for r in out["members"]] == ["merged", "discarded", "discarded"]
        assert [r["status"] for r in out["members"]][1:] == ["cancelled", "cancelled"]
        assert_gone(src, m, b, c)
        await m.stop()
    asyncio.run(body())


def test_group_discard_cancels_a_fully_running_group(tmp_path):
    async def body():
        src, m = real_manager(tmp_path)
        await m.start(maintenance=False)
        view = await m.create_compare("bump", LOCAL[1:], "repo")
        b, c = (r["id"] for r in view["members"])
        await one_runs_one_queues(m, b, c)
        out = await m.compare_discard(view["group"])
        assert [r["status"] for r in out["members"]] == ["cancelled", "cancelled"]
        assert_gone(src, m, b, c)
        await m.stop()
    asyncio.run(body())


def test_member_that_finishes_before_the_cancel_lands_is_still_discarded(tmp_path, monkeypatch):
    async def body():
        src, m, group, a, b, c = await running_group(tmp_path, monkeypatch)
        real_cancel, cancelled = m.cancel, []

        async def cancel_after_it_finished(ref):
            # the run ends on its own after the caller looked at its status but before cancel() does
            cancelled.append(ref)
            m.runner.chat.release.set()
            await wait_status(m, ref, "done")
            return await real_cancel(ref)  # 409: nothing to cancel

        m.cancel = cancel_after_it_finished
        out = await m.compare_pick(group, a, "merge", True)
        assert cancelled and [r["review"] for r in out["members"]] == ["merged", "discarded", "discarded"]
        assert_gone(src, m, b, c)
        await m.stop()
    asyncio.run(body())


def test_pick_of_a_running_winner_cancels_and_discards_nobody(tmp_path, monkeypatch):
    async def body():
        src, m, group, a, b, c = await running_group(tmp_path, monkeypatch)
        before = [m.db.get_session(s)["status"] for s in (a, b, c)]
        running = next(s for s in (b, c) if m.db.get_session(s)["status"] == "running")
        with pytest.raises(HarnessError) as e:
            await m.compare_pick(group, running, "merge", True)
        assert e.value.status == 409 and "still working" in str(e.value)
        assert [m.db.get_session(s)["status"] for s in (a, b, c)] == before
        assert [m.db.get_session(s)["review"] for s in (a, b, c)] == ["", "", ""]
        assert (src / "app.py").read_text() == "VALUE = 1\n"
        await m.stop()
    asyncio.run(body())


def test_discard_retry_after_a_failure_finishes_the_group(tmp_path, monkeypatch):
    async def body():
        src, m, group, a, b, c = await running_group(tmp_path, monkeypatch)
        real, fail = m.review, {"on": True}

        async def flaky(ref, action):
            if fail["on"] and ref == b:
                raise HarnessError(500, "disk")
            return await real(ref, action)

        m.review = flaky
        with pytest.raises(HarnessError) as e:
            await m.compare_pick(group, a, "merge", True)
        assert b in str(e.value) and m.db.get_session(a)["review"] == "merged"
        assert m.db.get_session(b)["status"] == "cancelled" and m.db.get_session(c)["review"] == "discarded"
        assert not m.db.get_session(b)["workspace_removed"]
        fail["on"] = False
        await m.compare_pick(group, a, "merge", True)
        assert_gone(src, m, b, c)
        await m.stop()
    asyncio.run(body())


def test_group_discard_before_members_are_checked_out(tmp_path):
    # never cloned, so review() has nothing to delete and refuses; the group must still be discardable
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    out = run(m.compare_discard(view["group"]))
    assert [(r["status"], r["review"]) for r in out["members"]] == [("cancelled", "discarded")] * 2
    for r in out["members"]:
        s = m.db.get_session(r["id"])
        assert s["workspace_removed"] and not Path(s["workspace"]).exists()


def release_on_cancel(m, gate):
    """Let a held clone go on only once a cancel is under way, so the cancel lands while the clone is in flight."""
    real_cancel = m.cancel

    async def cancel(ref):
        asyncio.get_running_loop().call_soon(gate.set)
        return await real_cancel(ref)

    m.cancel = cancel


def test_rollback_stops_members_that_never_ran_and_leaves_none_resumable(tmp_path):
    gate = threading.Event()

    def slow_prepare(project, ws, sid):
        gate.wait(10)  # a clone still in flight when the rollback cancels its member
        raise projects.GitError("interrupted")

    async def body():
        src, m = real_manager(tmp_path)
        await m.start(maintenance=False)
        real_prepare, projects.prepare = projects.prepare, slow_prepare
        release_on_cancel(m, gate)
        try:
            bad = LOCAL[:2] + [{"backend": "local", "model": "nope"}]
            with pytest.raises(HarnessError) as e:
                await m.create_compare("bump", bad, "repo")
            assert e.value.status == 400
            ids = [r["id"] for r in m.db.list_sessions()]
            assert len(ids) == 2 and {m.db.get_session(s)["compare_group"] for s in ids} == {""}
            assert_gone(src, m, *ids)
        finally:
            projects.prepare = real_prepare
            gate.set()
        await m.stop()
    asyncio.run(body())


def test_cancel_during_clone_leaves_no_workspace_or_branch(tmp_path, monkeypatch):
    cloning, gate = threading.Event(), threading.Event()

    async def body():
        src, m = real_manager(tmp_path)
        real_prepare = projects.prepare

        def prepare(project, ws, sid):
            cloning.set()
            gate.wait(10)
            return real_prepare(project, ws, sid)

        monkeypatch.setattr(projects, "prepare", prepare)
        release_on_cancel(m, gate)
        await m.start(maintenance=False)
        view = await m.create_compare("bump", LOCAL[1:], "repo")
        b, c = (r["id"] for r in view["members"])
        while not cloning.is_set():
            await asyncio.sleep(0.02)
        out = await m.compare_discard(view["group"])
        assert [(r["status"], r["review"]) for r in out["members"]] == [("cancelled", "discarded")] * 2
        assert gate.is_set()
        assert_gone(src, m, b, c)
        for sid in (b, c):
            # the clone that finished after the cancel was recorded, so discard removed its branch and workspace
            assert m.db.get_session(sid)["base_commit"], sid
        await m.stop()
    asyncio.run(body())


def test_concurrent_picks_and_discards_on_one_group_run_one_at_a_time(tmp_path, monkeypatch):
    async def body():
        src, m, group, a, b, c = await running_group(tmp_path, monkeypatch)
        first, *rest = await asyncio.gather(m.compare_pick(group, a, "merge", True),
                                            m.compare_pick(group, a, "push", True),
                                            m.compare_discard(group), return_exceptions=True)
        assert isinstance(first, dict), first
        assert [r["review"] for r in first["members"]] == ["merged", "discarded", "discarded"]
        for e in rest:
            assert isinstance(e, HarnessError) and e.status == 409 and e.code == "compare_busy", e
        assert (src / "app.py").read_text() == "VALUE = 2\n"
        assert_gone(src, m, b, c)
        # the guard is released when the pick ends; a later discard keeps the merged winner
        out = await m.compare_discard(group)
        assert [r["review"] for r in out["members"]] == ["merged", "discarded", "discarded"]
        await m.stop()
    asyncio.run(body())


def test_busy_guard_is_per_group(tmp_path):
    m = make_manager(tmp_path)
    one, two = (run(m.create_compare("p", CHOICES, "repo"))["group"] for _ in range(2))
    m.compare_busy.add(("owner", one))
    with pytest.raises(HarnessError) as e:
        run(m.compare_discard(one))
    assert e.value.code == "compare_busy"
    assert [r["review"] for r in run(m.compare_discard(two))["members"]] == ["discarded"] * 2


def test_ride_out_survives_repeated_cancels_then_reraises():
    from harness.runner import ride_out

    async def scenario():
        release = asyncio.Event()

        async def work():
            await release.wait()
            raise RuntimeError("setup failed")

        task = asyncio.ensure_future(work())
        waiter = asyncio.ensure_future(ride_out(task))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()  # a second cancel while still waiting
        await asyncio.sleep(0)
        assert not waiter.done() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert task.done()
        return task

    task = asyncio.run(scenario())
    assert task._log_traceback is False  # exception retrieved, cancel not masked by the failure


def test_ride_out_returns_when_not_cancelled():
    from harness.runner import ride_out

    async def scenario():
        task = asyncio.ensure_future(asyncio.sleep(0, result=1))
        await ride_out(task)
        return task.result()

    assert asyncio.run(scenario()) == 1
