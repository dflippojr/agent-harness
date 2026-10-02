"""Nightly agent regression canary (#265): store, regression rule, nightly trigger, yielding, metrics."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from harness import canary
from harness.canary import Canary, CanaryStore, Nightly, Report
from harness.config import CanaryConfig
from harness.db import Database
from harness.metrics import _canary_metrics, _Out
from harness.runner import Runner
from harness.scheduler import GpuScheduler


def _db(tmp_path) -> Database:
    return Database(tmp_path / "h.sqlite3")


def _outcomes(passes: int, total: int = 20, tasks: int = 10) -> list[dict]:
    return [{"task": f"t{i % tasks}", "repeat": i // tasks, "ok": i < passes, "status": "done", "turns": 2,
             "prompt_tokens": 100, "seconds": 1.0} for i in range(total)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 10
        return self.t


class ScriptedSuite:
    """A fake model that gets worse: the pass count of each full run comes from `passes`, reruns from `reruns`."""

    def __init__(self, passes: list[int], reruns: list[int] | None = None, status: str = "complete"):
        self.passes, self.reruns, self.status = list(passes), list(reruns or []), status
        self.calls: list[tuple[str, list[str] | None]] = []

    async def __call__(self, sha: str, only: list[str] | None) -> Report:
        self.calls.append((sha, only))
        if self.status != "complete":
            return Report(self.status)
        if only is None:
            return Report("complete", _outcomes(self.passes.pop(0)))
        out = _outcomes(self.reruns.pop(0), total=len(only) * 2, tasks=len(only))
        for i, o in enumerate(out):
            o["task"] = only[i % len(only)]
        return Report("complete", out)


def _canary(tmp_path, suite, notes):
    cfg = CanaryConfig(enabled=True)
    return Canary(CanaryStore(_db(tmp_path)), suite, cfg, notes.append, topic="t", clock=Clock()), cfg


def _seed(c: Canary, rates: list[int]) -> None:
    for i, passes in enumerate(rates):
        sha = f"{i:040x}"
        c.store.begin(sha, 100.0 + i)
        c.store.finish(sha, "complete", _outcomes(passes), 101.0 + i)


def run(coro):
    return asyncio.run(coro)


# regression rule
def test_no_alert_with_fewer_than_three_prior_results(tmp_path):
    notes: list[dict] = []
    c, _ = _canary(tmp_path, ScriptedSuite([2]), notes)
    _seed(c, [20, 20])
    row = run(c.run_for("f" * 40))
    assert row["status"] == "complete" and row["pass_rate"] == 0.1 and not row["alerted"]
    assert notes == []


def test_no_alert_for_a_drop_under_15_points(tmp_path):
    notes: list[dict] = []
    suite = ScriptedSuite([18])  # 90% vs a 100% baseline: 10 points
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20])
    run(c.run_for("a" * 40))
    assert notes == [] and suite.calls == [("a" * 40, None)]


def test_alert_on_a_confirmed_drop_of_15_points_with_compare_link(tmp_path):
    notes: list[dict] = []
    suite = ScriptedSuite([17], reruns=[2])  # 85% (exactly 15 points under 100%), the rerun stays bad
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20, 20, 20])
    # t0..t2 failed (indexes 17..19 of 20 attempts → tasks t7,t8,t9); history had them passing
    run(c.run_for("b" * 40))
    assert len(notes) == 1
    assert notes[0]["title"].endswith("bbbbbbbb")
    base = f"{4:040x}"  # the newest seeded run is the baseline representative
    assert f"compare/{base}...{'b' * 40}" in notes[0]["message"] and notes[0]["click"].startswith("https://github.com/")
    assert "85%" in notes[0]["message"] or "%" in notes[0]["message"]
    assert [only for _, only in suite.calls][0] is None and suite.calls[1][1] == ["t7", "t8", "t9"]
    assert c.store.get("b" * 40)["alerted"] == 1


def test_confirmation_rerun_that_recovers_cancels_the_alert(tmp_path):
    notes: list[dict] = []
    suite = ScriptedSuite([17], reruns=[6])  # all 6 reruns pass → 20/20 again
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20])
    row = run(c.run_for("c" * 40))
    assert notes == [] and row["pass_rate"] == 1.0 and not row["alerted"]


class NewTaskSuite:
    """First run: 17 attempts pass on known tasks, 3 attempts fail on a task with no history. Reruns use `rerun_ok`."""

    def __init__(self, rerun_ok: bool):
        self.rerun_ok, self.calls = rerun_ok, []

    async def __call__(self, sha, only):
        self.calls.append(only)
        if only is None:
            return Report("complete", _outcomes(17) [:17] + [{**_outcomes(1)[0], "task": "brand-new", "ok": False}] * 3)
        return Report("complete", [{**_outcomes(1)[0], "task": t, "ok": self.rerun_ok} for t in only for _ in range(3)])


def test_new_failing_task_without_history_is_confirmed_before_alerting(tmp_path):
    notes: list[dict] = []
    suite = NewTaskSuite(rerun_ok=False)
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20])
    run(c.run_for("d" * 40))
    assert suite.calls == [None, ["brand-new"]]  # no regressed task, so the failed one is the confirmation rerun
    assert len(notes) == 1 and c.store.get("d" * 40)["alerted"] == 1


def test_new_failing_task_that_recovers_on_rerun_does_not_alert(tmp_path):
    notes: list[dict] = []
    suite = NewTaskSuite(rerun_ok=True)
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20])
    row = run(c.run_for("e" * 40))
    assert suite.calls == [None, ["brand-new"]] and notes == [] and not row["alerted"]


def test_baseline_is_the_median_of_the_previous_five(tmp_path):
    prior = [{"pass_rate": r, "sha": str(i), "started_at": i} for i, r in enumerate([1.0, 0.9, 0.2, 0.95, 0.9])]
    v = canary.judge(0.75, prior, min_prior=3, drop_points=15)
    assert v.baseline_rate == 0.9 and v.alert  # 15 points below the median, one outlier doesn't move it
    assert not canary.judge(0.76, prior, min_prior=3, drop_points=15).alert


# one row per commit, nightly trigger
def test_one_row_per_sha_and_a_later_nightly_does_nothing(tmp_path):
    suite = ScriptedSuite([20, 20])
    c, _ = _canary(tmp_path, suite, [])
    run(c.run_for("d" * 40))
    again = run(c.run_for("d" * 40))
    assert again["status"] == "complete" and len(suite.calls) == 1
    count = c.store.db.conn.execute("SELECT COUNT(*) FROM canary_results").fetchone()[0]
    assert count == 1


def test_blocked_run_is_retried_once_next_night_then_skipped(tmp_path):
    suite = ScriptedSuite([], status="blocked")
    c, _ = _canary(tmp_path, suite, [])
    assert run(c.run_for("e" * 40))["status"] == "blocked"
    assert run(c.run_for("e" * 40))["status"] == "skipped"  # the retry also couldn't start
    run(c.run_for("e" * 40))
    assert len(suite.calls) == 2
    assert c.store.db.conn.execute("SELECT COUNT(*) FROM canary_results").fetchone()[0] == 1


def test_timeout_run_is_recorded_and_not_a_baseline(tmp_path):
    suite = ScriptedSuite([], status="blocked")
    c, _ = _canary(tmp_path, suite, [])
    c.store.begin("9" * 40, 1.0)
    c.store.finish("9" * 40, "timeout", _outcomes(5), 2.0)
    assert c.store.completed_before("0" * 40, 5) == []


def test_next_slot_is_0300_local():
    assert canary.next_slot(datetime(2026, 10, 2, 1, 0), "03:00") == datetime(2026, 10, 2, 3, 0)
    assert canary.next_slot(datetime(2026, 10, 2, 3, 0), "03:00") == datetime(2026, 10, 3, 3, 0)


def test_nightly_fires_at_the_slot_for_the_deployed_sha(tmp_path):
    seen: list[str] = []

    class Fake:
        async def run_for(self, sha):
            seen.append(sha)
            raise asyncio.CancelledError  # end the loop after one night

    clock = [datetime(2026, 10, 2, 2, 59, 59)]
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        clock[0] = datetime(2026, 10, 2, 3, 0, 0)
        await real_sleep(0)

    async def body():
        n = Nightly(Fake(), CanaryConfig(enabled=True), sha=lambda: "abc", now=lambda: clock[0])
        asyncio.sleep, orig = fake_sleep, asyncio.sleep
        try:
            try:
                await n._loop()
            except asyncio.CancelledError:
                pass
        finally:
            asyncio.sleep = orig

    run(body())
    assert seen == ["abc"] and slept == [1.0]


# yielding
def test_canary_waits_behind_real_sessions_and_a_real_arrival_gets_the_slot():
    async def body():
        sch = GpuScheduler()
        sch.low_priority.add("canary")
        await sch.acquire("real0")
        waiter = asyncio.create_task(sch.acquire("canary"))
        await asyncio.sleep(0)
        assert sch.holder == "real0" and not sch.idle()
        sch.release("real0")
        await waiter
        assert sch.holder == "canary"
        # a real session arrives while the canary holds the slot
        real = asyncio.create_task(sch.acquire("real1"))
        await asyncio.sleep(0)
        assert sch.holder == "canary" and sch.real_waiting()
        fake = SimpleNamespace(scheduler=sch, guard=None, yields={})

        async def _acquire(sid, front=False):
            await sch.acquire(sid, front=front)
        fake._acquire = _acquire
        gate = asyncio.create_task(Runner._gpu_gate(fake, "canary"))  # between turns: step aside
        await real
        assert sch.holder == "real1" and fake.yields == {"canary": 1}
        sch.release("real1")
        await gate
        assert sch.holder == "canary"
    run(body())


def test_a_real_session_queued_behind_the_canary_is_granted_first():
    async def body():
        sch = GpuScheduler()
        sch.low_priority.add("canary")
        await sch.acquire("real0")
        c = asyncio.create_task(sch.acquire("canary"))
        r = asyncio.create_task(sch.acquire("real1"))
        await asyncio.sleep(0)
        sch.release("real0")
        await asyncio.sleep(0)
        assert sch.holder == "real1"
        sch.release("real1")
        await c
        assert sch.holder == "canary" and r.done()
    run(body())



def _gate_host(sch: GpuScheduler) -> SimpleNamespace:
    host = SimpleNamespace(scheduler=sch, guard=None, yields={}, acquires=0)

    async def _acquire(sid, front=False):
        host.acquires += 1
        if host.acquires > 3:
            raise AssertionError("the gate is spinning on release/reacquire")
        await sch.acquire(sid, front=front)
    host._acquire = _acquire
    return host


def test_an_ineligible_member_waiter_neither_preempts_nor_starves_the_canary():
    async def body():
        capped = {"member2"}  # a household member at max_running: queued, never granted while capped
        sch = GpuScheduler(eligible=lambda sid: sid not in capped)
        sch.low_priority.add("canary")
        await sch.acquire("real0")
        c = asyncio.create_task(sch.acquire("canary"))
        member = asyncio.create_task(sch.acquire("member2"))
        await asyncio.sleep(0)
        sch.release("real0")
        await asyncio.sleep(0)
        assert sch.holder == "canary" and c.done()  # not starved behind a waiter that can't run
        assert not sch.real_waiting()
        host = _gate_host(sch)
        await asyncio.wait_for(Runner._gpu_gate(host, "canary"), 1)  # no preemption, no spin
        assert sch.holder == "canary" and host.yields == {} and host.acquires == 0
        # the member's cap frees up: now it is a real session that would be granted, and it goes first
        capped.clear()
        sch.recheck()
        assert sch.real_waiting()
        gate = asyncio.create_task(Runner._gpu_gate(host, "canary"))
        await member
        assert sch.holder == "member2" and host.yields == {"canary": 1}
        sch.release("member2")
        await gate
        assert sch.holder == "canary"
    run(body())

# run start conditions and suspend/restart, against a fake manager
class FakeManager:
    def __init__(self, guard_state="clear"):
        self.scheduler = GpuScheduler()
        self.guard = SimpleNamespace(state=guard_state, manual=False)
        self.images = None
        self.cfg = SimpleNamespace(projects={}, web=SimpleNamespace(page_chars=1000))
        self.runner = SimpleNamespace(generating=set(), gate=SimpleNamespace(busy=False, exclusive=False),
                                      yields={}, web_overrides={})
        self.created: list[str] = []
        self.sessions: dict[str, dict] = {}
        self.suspend_first = False


def _runner(m, tmp_path, **kw):
    from bakeoff.canary import CanaryRunner
    cfg = CanaryConfig(enabled=True, total_cap_seconds=100, start_wait_seconds=3)
    t = {"now": 0.0}

    async def sleep(s):
        t["now"] += max(s, 0.5)
        await asyncio.sleep(0)
    suite = {"repeats": 1, "hard": [], "web": [__import__("bakeoff.web_suite", fromlist=["TASKS"]).TASKS[2]]}
    return CanaryRunner(m, cfg, tmp_path, suite, poll_seconds=1, clock=lambda: t["now"], sleep=sleep, **kw), t


def test_run_does_not_start_while_a_session_holds_the_slot_or_the_guard_is_not_clear(tmp_path):
    async def body():
        m = FakeManager()
        runner, _ = _runner(m, tmp_path)
        await m.scheduler.acquire("real")           # a real session holds the slot
        assert not runner.can_start()
        assert (await runner.run("sha")).status == "blocked"
        m.scheduler.release("real")
        waiter = asyncio.create_task(m.scheduler.acquire("real2"))
        m.scheduler.paused = True                   # queue closed (guard paused it) with a session waiting
        await asyncio.sleep(0)
        assert not runner.can_start()
        waiter.cancel()
        m.scheduler.release("real2")
        m.scheduler.paused = False
        m.guard.state = "paused"
        assert not runner.can_start() and (await runner.run("sha")).status == "blocked"
        m.guard.state = "clear"
        assert runner.can_start()
    run(body())


def test_a_suspended_task_restarts_and_the_cap_records_timeout(tmp_path):
    async def body():
        m = FakeManager()
        runner, t = _runner(m, tmp_path)
        attempts = []

        async def once(task, kind, repeat, web, cap):
            attempts.append(len(attempts))
            return {"task": task.id, "repeat": repeat, "ok": len(attempts) > 1, "status": "done", "note": "",
                    "turns": 3, "prompt_tokens": 10, "seconds": 1, "suspended": len(attempts) == 1}
        runner._once = once
        report = await runner.run("sha")
        assert report.status == "complete" and len(attempts) == 2  # first attempt was suspended mid-task: restarted
        assert report.outcomes[0]["ok"] and report.outcomes[0]["restarts"] == 1
        t["now"] = 1000  # past the 100 s cap before the next run starts its tasks
        runner.cfg.total_cap_seconds = -1
        report = await runner.run("sha")
        assert report.status == "timeout" and report.outcomes[0]["status"] == "timeout"
    run(body())


def test_hard_setup_failure_stops_the_spawned_session_and_clears_low_priority(tmp_path):
    from bakeoff.canary import CanaryRunner
    from bakeoff.tasks_hard import HARD_TASKS
    from harness.llm import Completion
    from harness.manager import Manager
    from test_daemon import Script, make_cfg
    task = next(t for t in HARD_TASKS if t.id == "merge_conflict")
    seen: list[str] = []

    async def body():
        cfg = make_cfg(tmp_path)
        m = Manager(cfg, chat=Script([Completion(content="done")]))
        await m.start(maintenance=False)

        def docker_down(t, ws):
            seen.extend(m.scheduler.low_priority)  # the session was already spawned when setup runs
            raise RuntimeError("Cannot connect to the Docker daemon")
        try:
            runner = CanaryRunner(m, cfg.canary, tmp_path, {"repeats": 1, "hard": [task], "web": []},
                                  prepare_hard=docker_down)
            try:
                await runner.run("sha")
                raise AssertionError("setup error must reach the nightly (recorded blocked)")
            except RuntimeError:
                pass
            assert len(seen) == 1
            assert m.db.get_session(seen[0])["status"] == "cancelled"
            assert not m.tasks and m.scheduler.holder is None and not m.scheduler.low_priority
        finally:
            await m.stop()
            m.db.close()
    run(body())


def test_suspended_on_every_attempt_is_excluded_and_never_alerts(tmp_path):
    async def body():
        m = FakeManager()
        runner, _ = _runner(m, tmp_path)
        attempts = []

        async def once(task, kind, repeat, web, cap):
            attempts.append(1)
            return {"task": task.id, "repeat": repeat, "ok": True, "status": "done", "note": "", "turns": 3,
                    "prompt_tokens": 10, "seconds": 1, "suspended": True}
        runner._once = once
        report = await runner.run("sha")
        o = report.outcomes[0]
        assert len(attempts) == 3 and o["status"] == "suspended" and not o["ok"] and o["restarts"] == 2
        assert report.status == "complete"
        assert canary.pass_rate(report.outcomes) == 0.0 and canary.totals(report.outcomes)["attempts"] == 0
        assert canary.totals(report.outcomes)["excluded"] == 1
    run(body())


def test_suspended_outcomes_do_not_move_the_rate_or_trigger_alerts(tmp_path):
    ok = {"task": "a", "repeat": 0, "ok": True, "status": "done"}
    sus = {"task": "b", "repeat": 0, "ok": False, "status": "suspended"}
    assert canary.pass_rate([ok, sus]) == 1.0
    assert canary.regressed_tasks([sus], [{"outcomes": [{"task": "b", "ok": True, "status": "done"}]}]) == []
    store = CanaryStore(_db(tmp_path))
    store.begin("s1", 1.0)
    store.finish("s1", "complete", [sus], 2.0)
    row = store.get("s1")
    assert row["pass_rate"] is None and row["attempts"] == 0 and store.completed_before("x", 5) == []


def test_run_where_every_attempt_was_suspended_raises_no_alert(tmp_path):
    notes = []

    async def suite(sha, only):
        return Report("complete", [{"task": "a", "repeat": 0, "ok": False, "status": "suspended", "restarts": 2}])
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20])
    row = run(c.run_for("zzz"))
    assert not notes and row["pass_rate"] is None and not row["alerted"]


# metrics and storage
def test_metrics_export_latest_30_results_labelled_by_short_sha(tmp_path):
    db = _db(tmp_path)
    store = CanaryStore(db)
    for i in range(35):
        sha = f"{i:02d}" + "a" * 38
        store.begin(sha, 100.0 + i)
        store.finish(sha, "complete", _outcomes(18), 200.0 + i)
    m = SimpleNamespace(db=db, cfg=SimpleNamespace(canary=CanaryConfig()))
    out = _Out()
    _canary_metrics(m, out)
    text = "\n".join(out.lines)
    for name in ("pass_rate", "turns", "prompt_tokens", "wall_seconds"):
        assert f"# TYPE harness_canary_{name} gauge" in text
    rate_lines = [ln for ln in out.lines if ln.startswith("harness_canary_pass_rate{")]
    assert len(rate_lines) == 30
    assert 'harness_canary_pass_rate{sha="34aaaaaa"} 0.9' in text and 'sha="04aaaaaa"' not in text


def test_migration_creates_canary_results_for_fresh_and_existing_databases(tmp_path):
    db = _db(tmp_path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(canary_results)")}
    assert {"sha", "started_at", "outcomes", "pass_rate", "turns", "prompt_tokens", "wall_seconds", "status"} <= cols
    db.close()
