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


def test_only_finished_graded_attempts_are_evidence():
    out = [{"task": "a", "repeat": i, "ok": False, "status": st}
           for i, st in enumerate(("timeout", "suspended", "blocked", "cancelled"))]
    out += [{"task": "a", "repeat": 9, "ok": True, "status": "done"},
            {"task": "b", "repeat": 0, "ok": False, "status": "failed"},
            {"task": "c", "repeat": 0, "ok": False, "status": canary.WALL_LIMIT}]
    assert [o["status"] for o in canary.valid(out)] == ["done", "failed", canary.WALL_LIMIT]
    assert canary.pass_rate(out) == 1 / 3 and canary.totals(out)["excluded"] == 4
    assert canary.regressed_tasks(out[:4], [{"outcomes": [{"task": "a", "ok": True, "status": "done"}]}]) == []


class ConfirmSuite(ScriptedSuite):
    """A confirmed-looking drop (17/20 vs 100%) whose confirmation rerun comes back as `again(only)`."""

    def __init__(self, again):
        super().__init__([17])
        self.again = again

    async def __call__(self, sha, only):
        if only is None:
            return await super().__call__(sha, only)
        self.calls.append((sha, only))
        return self.again(only)


def test_a_confirmation_rerun_that_times_out_never_alerts(tmp_path):
    notes: list[dict] = []
    # the rerun hit total_cap_seconds: one failing attempt finished, the rest were recorded as timeout
    suite = ConfirmSuite(lambda only: Report("timeout", [{**_outcomes(1)[0], "task": only[0], "ok": False}] + [
        {**_outcomes(1)[0], "task": t, "ok": False, "status": "timeout"} for t in only]))
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20, 20, 20])
    row = run(c.run_for("1" * 40))
    assert suite.calls[1][1] == ["t7", "t8", "t9"]
    assert notes == [] and not row["alerted"] and row["status"] == "complete"
    assert not any(o.get("confirm") for o in row["outcomes"]) and row["pass_rate"] == 0.85


def test_a_complete_confirmation_without_a_finished_attempt_of_every_task_never_alerts(tmp_path):
    notes: list[dict] = []
    suite = ConfirmSuite(lambda only: Report("complete", [
        {**_outcomes(1)[0], "task": t, "ok": False, "status": "done" if t != only[-1] else "cancelled"}
        for t in only]))
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20, 20, 20])
    row = run(c.run_for("2" * 40))
    assert notes == [] and not row["alerted"]


def test_a_confirmation_with_valid_evidence_alerts_once_and_ignores_unfinished_attempts(tmp_path):
    notes: list[dict] = []
    suite = ConfirmSuite(lambda only: Report("complete", [
        {**_outcomes(1)[0], "task": t, "ok": False, "status": st} for t in only for st in ("failed", "suspended")]))
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20, 20, 20])
    row = run(c.run_for("3" * 40))
    assert len(notes) == 1 and row["alerted"] == 1
    # t7..t9's 6 first-run attempts are replaced by 3 failed + 3 suspended; the suspended ones are left out
    assert len(row["outcomes"]) == 20 and row["attempts"] == 17 and row["pass_rate"] == 14 / 17


def test_a_confirmation_rerun_that_raises_finishes_the_row_with_the_first_run_and_no_alert(tmp_path):
    notes: list[dict] = []

    def docker_down(only):
        raise RuntimeError("Cannot connect to the Docker daemon")  # prepare_hard_task in the rerun
    suite = ConfirmSuite(docker_down)
    c, _ = _canary(tmp_path, suite, notes)
    _seed(c, [20, 20, 20, 20, 20])
    row = run(c.run_for("4" * 40))
    assert len(suite.calls) == 2 and notes == [] and not row["alerted"]
    assert row["status"] == "complete" and row["finished_at"] is not None and row["pass_rate"] == 0.85
    assert len(row["outcomes"]) == 20 and not any(o.get("confirm") for o in row["outcomes"])
    assert len(run(c.run_for("4" * 40))["outcomes"]) == 20 and len(suite.calls) == 2  # never rerun for this SHA


def test_a_shutdown_during_the_confirmation_still_finishes_the_row_with_the_first_run(tmp_path):
    notes: list[dict] = []

    def shutdown(only):
        raise asyncio.CancelledError
    c, _ = _canary(tmp_path, ConfirmSuite(shutdown), notes)
    _seed(c, [20, 20, 20, 20, 20])
    try:
        run(c.run_for("5" * 40))
        raise AssertionError("a shutdown must still stop the nightly")
    except asyncio.CancelledError:
        pass
    row = c.store.get("5" * 40)
    assert notes == [] and not row["alerted"] and row["status"] == "complete" and row["pass_rate"] == 0.85


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


def test_next_slot_rejects_anything_but_hh_mm():
    for bad in ("3", "25:00", "03:60", "x:y", 185, None):
        try:
            canary.next_slot(datetime(2026, 10, 2, 1, 0), bad)
            raise AssertionError(f"{bad!r} accepted")
        except ValueError:
            pass


def test_a_bad_slot_time_disables_the_nightly_with_a_log_instead_of_killing_the_daemon(tmp_path, caplog):
    seen: list[str] = []

    class Fake:
        async def run_for(self, sha):
            seen.append(sha)

    async def body():
        n = Nightly(Fake(), CanaryConfig(enabled=True, at=185), sha=lambda: "abc")
        n.start()
        await asyncio.wait_for(n._task, 1)  # the loop ends on its own, without raising
        await n.stop()
    with caplog.at_level("ERROR", logger="harness.canary"):
        run(body())
    assert not seen and "canary disabled" in caplog.text


def test_canary_at_is_normalised_or_rejected_when_the_config_loads(tmp_path):
    import shutil
    import yaml
    from pathlib import Path
    from harness import config
    assert config._load_canary(yaml.safe_load("at: 3:05")).at == "03:05"   # YAML 1.1 sexagesimal int 185
    assert config._load_canary(yaml.safe_load("at: 23:59")).at == "23:59"
    assert config._load_canary({"at": "3:05"}).at == "03:05"
    assert config._load_canary(None).at == "03:00"
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    assert config._load_canary({"enabled": True, "fixture_dir": str(tmp_path)}).suite == "bakeoff/canary.yaml"
    for raw in ({"at": "25:00"}, {"at": 1440}, {"at": "noon"}, {"at": True}, {"repeats": 0},
                {"min_prior_runs": 6, "baseline_runs": 5}, {"total_cap_seconds": "soon"}, {"drop_points": 0}):
        try:
            config._load_canary(raw)
            raise AssertionError(f"{raw} accepted")
        except ValueError as e:
            assert "canary." in str(e)
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(Path(__file__).resolve().parent.parent / "config", cfg_dir)
    with (cfg_dir / "harness.yaml").open("a", encoding="utf-8") as f:
        f.write("\ncanary:\n  at: 3:05\n")
    assert config.load(cfg_dir, data_dir=tmp_path / "data").canary.at == "03:05"


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
    (tmp_path / "manifest.json").write_text('{"searches": {}, "pages": {}}', encoding="utf-8")
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


def _hard_night(tmp_path, chat, on_sleep, run_body, prepare=None):
    """One hard task (no Docker: setup and checker stubbed) through a real Manager and Canary.run_for."""
    from bakeoff.canary import CanaryRunner
    from bakeoff.tasks_hard import HARD_TASKS
    from harness.manager import Manager
    from test_daemon import make_cfg
    task = next(t for t in HARD_TASKS if t.id == "merge_conflict")

    async def body():
        cfg = make_cfg(tmp_path)
        m = Manager(cfg, chat=chat)
        await m.start(maintenance=False)
        t = {"now": 0.0}

        async def sleep(_):
            await on_sleep(m)
            for _ in range(100):  # a real poll lasts seconds: let the session get the slot (or end) first
                if not m.tasks or m.scheduler.holder in m.tasks:
                    break
                await asyncio.sleep(0.005)
            t["now"] += task.wall_limit + 1  # every poll crosses the task's wall-clock limit
        try:
            runner = CanaryRunner(m, cfg.canary, tmp_path, {"repeats": 1, "hard": [task], "web": []},
                                  clock=lambda: t["now"], sleep=sleep,
                                  prepare_hard=(lambda tk, ws: prepare(m) or {}) if prepare else lambda tk, ws: {},
                                  grade_hard=lambda tk, ws, answer, base: (answer == "done", "graded"))
            c = Canary(CanaryStore(m.db), runner.run, cfg.canary, lambda n: None)
            row = await c.run_for("f00d" * 10)
            await run_body(m, row)
            assert not m.tasks and m.scheduler.holder is None and not m.scheduler.low_priority
        finally:
            await m.stop()
            m.db.close()
    run(body())


def test_a_task_that_finishes_in_the_expiry_window_is_recorded_and_the_night_completes(tmp_path):
    from harness.llm import Completion
    from test_daemon import Script

    async def finish(m):  # the session ends during the poll sleep that crosses the limit
        await asyncio.gather(*m.tasks.values(), return_exceptions=True)

    async def check(m, row):
        assert row["status"] == "complete" and row["attempts"] == 1 and row["pass_rate"] == 1.0
        assert row["outcomes"][0]["status"] == "done" and row["outcomes"][0]["ok"]
    _hard_night(tmp_path, Script([Completion(content="done")]), finish, check)


def _blocked_chat(gate: asyncio.Event):
    from harness.llm import Completion

    async def chat(model, messages, tools, *args, **kw):
        await gate.wait()
        return Completion(content="done", prompt_tokens=10, completion_tokens=1)
    return chat


def test_a_cancel_that_races_the_finish_records_the_real_outcome(tmp_path):
    gate, cancels, armed = asyncio.Event(), [], []

    async def nothing(m):
        if not armed:  # first poll: arm the race; Manager.cancel finds the session already done (409)
            armed.append(True)
            real = m.cancel

            async def racing(sid):
                cancels.append(sid)
                gate.set()
                await asyncio.gather(*m.tasks.values(), return_exceptions=True)
                return await real(sid)
            m.cancel = racing

    async def check(m, row):
        assert len(cancels) == 1 and row["status"] == "complete"
        assert row["outcomes"][0]["status"] == "done" and row["outcomes"][0]["ok"]
    _hard_night(tmp_path, _blocked_chat(gate), nothing, check)


def test_an_attempt_stopped_at_the_wall_limit_is_a_graded_fail(tmp_path):
    async def nothing(m):
        await asyncio.sleep(0)

    async def check(m, row):
        o = row["outcomes"][0]
        assert o["status"] == canary.WALL_LIMIT and not o["ok"] and "wall-clock limit" in o["note"]
        assert row["status"] == "complete" and row["attempts"] == 1 and row["pass_rate"] == 0.0
    _hard_night(tmp_path, _blocked_chat(asyncio.Event()), nothing, check)


def test_a_task_that_never_gets_the_gpu_before_the_cap_is_excluded_not_failed(tmp_path):
    def real_wins_the_slot(m):  # right after create, before the canary session's first acquire
        m.scheduler.holder = "real-session"

    async def nothing(m):
        await asyncio.sleep(0)

    async def check(m, row):
        o = row["outcomes"][0]
        assert o["status"] == "suspended" and not o["ok"] and o["seconds"] == 0 and "GPU" in o["note"]
        assert row["attempts"] == 0 and row["pass_rate"] is None and not row["alerted"]
        m.scheduler.release("real-session")
    _hard_night(tmp_path, _blocked_chat(asyncio.Event()), nothing, check, prepare=real_wins_the_slot)


def test_time_an_image_batch_has_the_gpu_is_not_run_time(tmp_path):
    async def image_batch(m):  # images.py took the GPU over (gate.exclusive) while the canary holds the slot
        m.runner.gate.exclusive_active = True
        await asyncio.sleep(0)

    async def check(m, row):
        o = row["outcomes"][0]
        assert o["status"] == "suspended" and o["seconds"] == 0 and row["attempts"] == 0
        m.runner.gate.exclusive_active = False
    _hard_night(tmp_path, _blocked_chat(asyncio.Event()), image_batch, check)


def test_a_missing_web_fixture_stops_the_run_before_any_session(tmp_path):
    m = FakeManager()
    runner, _ = _runner(m, tmp_path)
    runner.fixture = tmp_path / "nowhere"
    try:
        run(runner.run("sha"))
        raise AssertionError("every web task would fail against an empty replay")
    except canary.CanaryConfigError as e:
        assert "manifest.json" in str(e)
    assert not m.created and m.cfg.projects == {}


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


# a crash mid-canary and a missing web fixture (#316)
def _crashed_canary(tmp_path, outcomes: list[dict] | None = None):
    """A database as `kill -9` leaves it mid-canary: one canary session running, one queued, a real one running,
    and the commit's row claimed."""
    from harness.manager import Manager
    from test_daemon import make_cfg
    cfg = make_cfg(tmp_path)

    async def body():
        m = Manager(cfg)
        for sid, project, status in (("c-hard", "canary-hard", "running"), ("c-web", "canary-web", "queued"),
                                     ("real", "scratch", "waiting_approval")):
            m.db.insert_session({"id": sid, "title": sid, "project": project, "target": "t", "model": "fake",
                                 "status": status, "workspace": str(tmp_path / sid), "created_at": 1.0,
                                 "updated_at": 1.0, "context": []})
        store = CanaryStore(m.db)
        store.begin("f" * 40, 1.0)
        if outcomes:
            store.first_run("f" * 40, outcomes)
        m.db.close()
    run(body())
    return cfg


def _restart(cfg, chat):
    from harness.manager import Manager

    async def body():
        m = Manager(cfg, chat=chat)
        spawned = []
        m._spawn = lambda sid, recovered=False: spawned.append(sid)  # what start resumes; nothing actually runs
        await m.start()
        try:
            await asyncio.sleep(0.05)
            return ({sid: m.db.get_session(sid)["status"] for sid in ("c-hard", "c-web", "real")}, spawned,
                    CanaryStore(m.db).get("f" * 40), dict(m.runner.web_overrides))
        finally:
            await m.stop()
            m.db.close()
    return run(body())


def test_a_restart_cancels_canary_sessions_instead_of_resuming_them_and_finishes_the_row(tmp_path):
    async def chat(*a, **k):
        raise AssertionError("a canary session was resumed")
    status, spawned, row, overrides = _restart(_crashed_canary(tmp_path), chat)
    assert status["c-hard"] == status["c-web"] == "cancelled"
    assert spawned == ["real"]  # real sessions still resume
    assert not overrides  # the web canary never ran again, so no WebTools of any kind, live or recorded
    assert row["status"] == "blocked" and row["finished_at"] and "restarted" in row["note"]


def test_a_restart_during_the_confirmation_finishes_the_row_with_the_first_run_and_no_alert(tmp_path):
    async def chat(*a, **k):
        raise AssertionError("a canary session was resumed")
    _, _, row, _ = _restart(_crashed_canary(tmp_path, _outcomes(12)), chat)
    assert row["status"] == "complete" and row["passes"] == 12 and row["attempts"] == 20
    assert not row["alerted"] and "first-run" in row["note"]


def test_an_empty_fixture_dir_skips_the_commit_once_with_one_error_log(tmp_path, caplog):
    from bakeoff.canary import CanaryRunner
    m = FakeManager()
    empty = tmp_path / "fixture"
    empty.mkdir()
    suite = {"repeats": 1, "hard": [], "web": [__import__("bakeoff.web_suite", fromlist=["TASKS"]).TASKS[2]]}

    async def run_suite(sha, only):
        return await CanaryRunner(m, CanaryConfig(enabled=True), empty, suite).run(sha, only)
    c = Canary(CanaryStore(_db(tmp_path)), run_suite, CanaryConfig(enabled=True), [].append, clock=Clock())
    with caplog.at_level("ERROR", logger="harness.canary"):
        row = run(c.run_for("a" * 40))
        for _ in range(3):  # the following nights
            assert run(c.run_for("a" * 40))["status"] == "skipped"
    assert row["status"] == "skipped" and "manifest.json" in row["note"] and row["tries"] == 1
    assert not m.created and m.cfg.projects == {}
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_a_missing_suite_file_is_a_config_error_too(tmp_path):
    from bakeoff.canary import load_suite
    from harness.canary import CanaryConfigError
    try:
        load_suite(tmp_path / "nope.yaml")
        raise AssertionError("loaded a missing suite")
    except CanaryConfigError as e:
        assert "nope.yaml" in str(e)


def _doctor_lines(cfg):
    from harness import doctor

    class Rec(doctor.Report):
        def __init__(self):
            super().__init__()
            self.lines = []

        def ok(self, name, detail=""):
            self.lines.append(("ok", name, detail))

        def fail(self, name, detail):
            super().fail(name, detail)
            self.lines.append(("fail", name, detail))
    r = Rec()
    doctor.check_canary(r, cfg)
    return r.lines


def test_a_missing_web_fixture_disables_only_the_canary_and_doctor_fails(tmp_path, caplog):
    from types import SimpleNamespace
    from harness import config
    with caplog.at_level("ERROR", logger="harness.config"):
        cfg = config._load_canary({"enabled": True, "fixture_dir": str(tmp_path)})  # no raise: the daemon starts
    assert cfg.enabled is False
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "canary.fixture_dir" in errors[0] and "manifest.json" in errors[0]
    lines = _doctor_lines(SimpleNamespace(canary=cfg))
    assert lines[0][0] == "fail" and cfg.disabled_reason in lines[0][2]
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    ok = config._load_canary({"enabled": True, "fixture_dir": str(tmp_path)})
    assert ok.enabled is True and ok.fixture_dir == str(tmp_path) and not _doctor_lines(SimpleNamespace(canary=ok))
    assert config._load_canary({"fixture_dir": str(tmp_path / "nope")}).enabled is False  # off: not checked


def test_a_missing_suite_file_disables_only_the_canary_and_doctor_fails(tmp_path, caplog):
    from types import SimpleNamespace
    from harness import config
    with caplog.at_level("ERROR", logger="harness.config"):
        cfg = config._load_canary({"enabled": True, "suite": "nope/missing.yaml"})
    assert cfg.enabled is False
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "canary.suite" in errors[0] and "missing.yaml" in errors[0]
    assert _doctor_lines(SimpleNamespace(canary=cfg))[0][0] == "fail"
