"""A canary that stops running is noticed (#732): slot ledger, silence gauges, silence alarm, indefinite-hold reminder
and the alert-test trigger."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from harness import canary
from harness.api import create_app
from harness.canary import Canary, CanaryStore, Report
from harness.config import CanaryConfig, GpuGuardConfig
from harness.db import Database
from harness.metrics import _canary_metrics, _Out
from harness.scheduler import GpuScheduler
from harness_modules.local_model.service import HOLD_REMINDER_SECONDS, MANUAL_HOLD_FILE, GpuGuard, hold_reminder
from harness_modules.notifications.service import DeliveryError

from test_canary import FakeManager, ScriptedSuite, _outcomes, _runner
from test_module_notifications import enabled_manager, make
from test_phase5 import FakeControl, FakeDetect

DAY = 86400
NIGHT = datetime(2026, 10, 10, 3, 0).timestamp()  # tower-local 03:00


class Nights:
    """The canary's clock: `t` moves only when the test says a night (or an hour) has passed."""

    def __init__(self):
        self.t = NIGHT

    def __call__(self) -> float:
        return self.t


class BlockedSuite:
    def __init__(self, reason: str):
        self.reason, self.calls = reason, 0

    async def __call__(self, sha: str, only):
        self.calls += 1
        return Report("blocked", reason=self.reason)


def _canary(tmp_path, suite, notes, clock):
    return Canary(CanaryStore(Database(tmp_path / "h.sqlite3")), suite, CanaryConfig(enabled=True), notes.append,
                  topic="t", clock=clock)


def _gauges(store: CanaryStore, guard=None) -> dict[str, float]:
    out = _Out()
    _canary_metrics(SimpleNamespace(db=store.db, cfg=SimpleNamespace(canary=CanaryConfig()), guard=guard), out)
    values = {}
    for line in out.lines:
        if line.startswith(("harness_canary_last", "harness_canary_consecutive", "harness_gpu_manual")):
            name, value = line.split(" ")
            values[name] = float(value)
    return values


def run(coro):
    return asyncio.run(coro)


# the silence alarm
def test_blocked_blocked_skipped_sends_exactly_one_alert_with_the_reasons(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    suite = BlockedSuite("the GPU is held by hand with no end (resume: harness resources resume)")
    c = _canary(tmp_path, suite, notes, clock)
    assert run(c.nightly("a" * 40))["status"] == "blocked"
    clock.t += DAY
    assert run(c.nightly("b" * 40))["status"] == "blocked"   # a new deploy, still held
    assert notes == []
    clock.t += DAY
    assert run(c.nightly("b" * 40))["status"] == "skipped"   # its retry couldn't start either
    assert len(notes) == 1
    alert = notes[0]
    assert alert["topic"] == "t" and alert["title"] == "Agent canary has not run for 3 nights"
    assert "since 2026-10-10" in alert["message"] and "held by hand with no end" in alert["message"]
    assert "harness resources resume" in alert["message"]
    # the reason is on the canary_results row too
    assert "held by hand" in c.store.get("b" * 40)["note"]
    assert _gauges(c.store)["harness_canary_consecutive_unrun_slots"] == 3


def test_the_alarm_repeats_at_most_once_a_day_while_silent(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, BlockedSuite("the GPU queue was busy"), notes, clock)
    for night in range(3):
        run(c.nightly(f"{night:040x}"))
        clock.t += DAY
    assert len(notes) == 1
    clock.t -= DAY - 3600                                    # an extra slot the same day (a restart, say)
    run(c.nightly("e" * 40))
    assert len(notes) == 1
    clock.t += DAY
    run(c.nightly("f" * 40))                                  # the next night, still silent
    assert len(notes) == 2 and notes[1]["title"] == "Agent canary has not run for 5 nights"


def test_an_already_skipped_commit_keeps_the_streak_and_says_why(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    suite = BlockedSuite("the resource guard is paused: Hades.exe")
    c = _canary(tmp_path, suite, notes, clock)
    run(c.nightly("a" * 40))
    clock.t += DAY
    run(c.nightly("a" * 40))                                  # retried, then skipped
    clock.t += DAY
    run(c.nightly("a" * 40))                                  # nothing runs: the commit is final
    assert suite.calls == 2 and len(notes) == 1
    assert "aaaaaaaa was already skipped (the resource guard is paused: Hades.exe)" in notes[0]["message"]
    assert "the next deployed commit runs" in notes[0]["message"]


def test_a_missing_fixture_counts_as_unrun_with_its_reason(tmp_path):
    async def missing(sha, only):
        raise canary.CanaryConfigError("canary web fixture /x has no manifest.json")
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, missing, notes, clock)
    for night in range(3):
        run(c.nightly(f"{night:040x}"))
        clock.t += DAY
    assert len(notes) == 1 and "has no manifest.json" in notes[0]["message"]


def test_a_real_complete_row_resets_the_counters(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, BlockedSuite("the GPU queue was busy"), notes, clock)
    for night in range(3):
        run(c.nightly(f"{night:040x}"))
        clock.t += DAY
    assert len(notes) == 1
    before = _gauges(c.store)
    assert before["harness_canary_consecutive_unrun_slots"] == 3
    assert math.isinf(before["harness_canary_last_complete_age_seconds"])

    c.run_suite = ScriptedSuite([18])
    assert run(c.nightly("c" * 40))["status"] == "complete"
    after = _gauges(c.store)
    assert after["harness_canary_consecutive_unrun_slots"] == 0
    assert not math.isinf(after["harness_canary_last_complete_age_seconds"])
    # the next block starts a new count: no alert until three more
    c.run_suite = BlockedSuite("the GPU queue was busy")
    for night in range(2):
        clock.t += DAY
        run(c.nightly(f"d{night:039x}"))
    assert len(notes) == 1 and _gauges(c.store)["harness_canary_consecutive_unrun_slots"] == 2


def test_a_slot_on_a_commit_that_already_ran_is_not_silence(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, ScriptedSuite([18]), notes, clock)
    for _ in range(4):
        run(c.nightly("a" * 40))                              # no deploy for four nights
        clock.t += DAY
    assert notes == [] and _gauges(c.store)["harness_canary_consecutive_unrun_slots"] == 0


def test_a_slot_with_no_deployed_commit_is_unrun(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, ScriptedSuite([]), notes, clock)
    for _ in range(3):
        run(c.nightly(""))
        clock.t += DAY
    assert len(notes) == 1 and "no deployed commit" in notes[0]["message"]



class ShutdownSuite:
    """The daemon stops while the slot waits for the GPU."""

    async def __call__(self, sha: str, only):
        raise asyncio.CancelledError


def test_slots_a_shutdown_interrupts_are_recorded_and_the_next_start_alerts(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, ShutdownSuite(), notes, clock)
    for night in range(3):
        try:
            run(c.nightly(f"{night:040x}"))
            raise AssertionError("the shutdown was swallowed")
        except asyncio.CancelledError:
            pass
        clock.t += DAY
    streak = c.store.unrun_streak()
    assert [s["outcome"] for s in streak] == ["blocked"] * 3
    assert all(s["reason"] == "the daemon stopped during the slot" for s in streak)
    assert notes == []                                         # the notifier was stopping too
    run(c.silence_alarm())                                     # the next start
    assert len(notes) == 1 and "the daemon stopped during the slot" in notes[0]["message"]
    run(c.silence_alarm())                                     # a second restart the same day
    assert len(notes) == 1


def test_a_shutdown_that_leaves_the_row_running_is_not_counted_again_at_the_next_start(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, ShutdownSuite(), notes, clock)
    sha = "c" * 40

    async def claimed_then_cancelled(_sha: str):
        c.store.begin(_sha, clock.t)                           # the claim committed, the cleanup never ran
        raise asyncio.CancelledError

    c.run_for = claimed_then_cancelled
    try:
        run(c.nightly(sha))
        raise AssertionError("the shutdown was swallowed")
    except asyncio.CancelledError:
        pass
    assert len(c.store.unrun_streak()) == 1
    assert c.store.interrupted(clock.t + 60) == []             # nothing is left running for the next start
    assert len(c.store.unrun_streak()) == 1


def test_a_shutdown_during_the_first_read_of_the_slot_is_still_recorded(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, ShutdownSuite(), notes, clock)
    real_get = c.store.get
    calls = []

    def get(sha):
        calls.append(sha)
        if len(calls) == 1:
            raise asyncio.CancelledError                       # the shutdown lands on the slot's first read
        return real_get(sha)

    c.store.get = get
    try:
        run(c.nightly("e" * 40))
        raise AssertionError("the shutdown was swallowed")
    except asyncio.CancelledError:
        pass
    assert [(s["outcome"], s["reason"]) for s in c.store.unrun_streak()] == [
        ("skipped", "the daemon stopped during the slot")]


def test_a_shutdown_never_overwrites_a_result_that_finished(tmp_path):
    store = CanaryStore(Database(tmp_path / "h.sqlite3"))
    sha = "f" * 40
    store.begin(sha, NIGHT)
    store.finish(sha, "complete", _outcomes(18), NIGHT + 60)
    store.abandon(sha, NIGHT + 120, "the daemon stopped during the slot")
    row = store.get(sha)
    assert row["status"] == "complete" and row["outcomes"]
    store.begin("9" * 40, NIGHT)
    store.abandon("9" * 40, NIGHT + 120, "the daemon stopped during the slot")
    assert store.get("9" * 40)["status"] == "blocked"


def test_a_claim_that_commits_after_the_shutdown_recorded_its_slot_is_not_counted_again(tmp_path):
    store = CanaryStore(Database(tmp_path / "h.sqlite3"))
    sha = "d" * 40
    store.record_slot(NIGHT + 5, sha, "skipped", "no result recorded")   # the shutdown found no row yet
    store.begin(sha, NIGHT)                                              # ... then the queued claim committed
    assert len(store.interrupted(NIGHT + DAY)) == 1
    assert len(store.unrun_streak()) == 1
    assert store.get(sha)["status"] == "blocked"


def test_a_slot_a_crash_interrupted_is_recorded_at_the_next_start(tmp_path):
    store = CanaryStore(Database(tmp_path / "h.sqlite3"))
    store.begin("a" * 40, NIGHT)                               # the daemon died mid-run
    store.interrupted(NIGHT + 60)
    assert [(s["sha"], s["outcome"], s["reason"]) for s in store.unrun_streak()] == [
        ("a" * 40, "blocked", "daemon restarted mid-run")]
    store.begin("b" * 40, NIGHT + DAY)
    store.first_run("b" * 40, _outcomes(18))                   # ... or during the confirmation rerun
    store.interrupted(NIGHT + DAY + 60)
    assert store.unrun_streak() == []


def test_the_nightly_loop_checks_for_silence_when_it_starts(tmp_path):
    notes: list[dict] = []
    clock = Nights()
    c = _canary(tmp_path, ShutdownSuite(), notes, clock)
    for night in range(3):
        c.store.record_slot(clock.t, f"{night:040x}", "blocked", "the GPU queue was busy")
        clock.t += DAY

    async def body():
        n = canary.Nightly(c, CanaryConfig(enabled=True), sha=lambda: "", now=lambda: datetime(2026, 10, 13, 4, 0))
        n.start()
        for _ in range(100):
            if notes:
                break
            await asyncio.sleep(0.01)
        await n.stop()
    run(body())
    assert len(notes) == 1 and notes[0]["title"] == "Agent canary has not run for 3 nights"

# gauges
def test_gauges_without_any_run_or_hold(tmp_path):
    store = CanaryStore(Database(tmp_path / "h.sqlite3"))
    out = _Out()
    _canary_metrics(SimpleNamespace(db=store.db, cfg=SimpleNamespace(canary=CanaryConfig()), guard=None), out)
    assert "harness_canary_last_complete_age_seconds +Inf" in out.lines
    assert "harness_canary_consecutive_unrun_slots 0" in out.lines
    assert "harness_gpu_manual_hold_age_seconds 0" in out.lines


def test_complete_rows_without_a_graded_attempt_do_not_count_as_a_result(tmp_path):
    store = CanaryStore(Database(tmp_path / "h.sqlite3"))
    store.begin("a" * 40, 1.0)
    suspended = [{**o, "status": "suspended"} for o in _outcomes(0, total=2)]
    store.finish("a" * 40, "complete", suspended, 2.0)
    assert store.last_complete_at() is None
    store.begin("b" * 40, 3.0)
    store.finish("b" * 40, "complete", _outcomes(18), time.time() - 120)
    assert 119 <= _gauges(store)["harness_canary_last_complete_age_seconds"] < 200


def _guard(tmp_path, **cfg) -> GpuGuard:
    return GpuGuard(GpuGuardConfig(enabled=True, resume_after_seconds=999, **cfg), None, GpuScheduler(),
                    lambda: False, detect=FakeDetect(), control=FakeControl(), data_dir=tmp_path)


def test_hold_age_gauge_follows_the_manual_hold(tmp_path):
    store = CanaryStore(Database(tmp_path / "h.sqlite3"))
    guard = _guard(tmp_path)
    assert _gauges(store, guard)["harness_gpu_manual_hold_age_seconds"] == 0
    guard.pause()
    guard.manual_since = time.time() - 3600
    assert 3599 <= _gauges(store, guard)["harness_gpu_manual_hold_age_seconds"] < 3700
    guard.resume()
    assert _gauges(store, guard)["harness_gpu_manual_hold_age_seconds"] == 0


# the indefinite-hold reminder
def test_an_indefinite_hold_over_a_day_old_is_reminded_once_a_day(tmp_path):
    guard = _guard(tmp_path)
    guard.pause()
    start = guard.manual_since
    assert not guard.hold_reminder_due(start + HOLD_REMINDER_SECONDS - 1)
    assert guard.hold_reminder_due(start + HOLD_REMINDER_SECONDS + 1)
    assert not guard.hold_reminder_due(start + HOLD_REMINDER_SECONDS + 3600)
    assert guard.hold_reminder_due(start + 2 * HOLD_REMINDER_SECONDS + 2)
    guard.pause()                                             # pausing again doesn't restart the clock
    assert guard.manual_since == start
    guard.resume()
    assert guard.manual_since is None and not guard.hold_reminder_due(start + 10 * DAY)


def test_a_timed_hold_is_never_reminded(tmp_path):
    guard = _guard(tmp_path)
    guard.pause(duration_seconds=DAY)
    assert not guard.hold_reminder_due(guard.manual_since + 2 * DAY)


def test_the_reminder_survives_a_restart_without_repeating_and_old_hold_files_use_their_age(tmp_path):
    async def body():
        guard = _guard(tmp_path, poll_seconds=3600)
        guard.pause()
        start = guard.manual_since
        assert guard.hold_reminder_due(start + HOLD_REMINDER_SECONDS + 1)
        again = _guard(tmp_path, poll_seconds=3600)
        again.start()
        try:
            assert again.manual and again.manual_since == start
            assert not again.hold_reminder_due(start + HOLD_REMINDER_SECONDS + 7200)
        finally:
            await again.stop()

        # a hold saved before #732 (no start recorded): its age is the file's
        path = tmp_path / MANUAL_HOLD_FILE
        path.write_text(json.dumps({"manual": True, "manual_until": None, "manual_duration_seconds": None}),
                        encoding="utf-8")
        week_ago = time.time() - 7 * DAY
        os.utime(path, (week_ago, week_ago))
        old = _guard(tmp_path, poll_seconds=3600)
        old.start()
        try:
            assert abs(old.manual_since - week_ago) < 2 and old.hold_reminder_due()
            assert json.loads(path.read_text(encoding="utf-8"))["since"] == old.manual_since
        finally:
            await old.stop()
    run(body())


def test_the_guard_loop_sends_the_reminder_with_its_age_and_the_resume_command(tmp_path):
    async def body():
        guard = _guard(tmp_path, poll_seconds=0.02)
        sent: list[float] = []
        guard.on_hold_reminder = sent.append
        guard.pause()
        guard.manual_since -= 3 * DAY + 2 * 3600
        guard._persist_hold()                                 # start() restores the hold from its file
        guard.start()
        try:
            for _ in range(100):
                if sent:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)                          # later checks the same day send nothing
        finally:
            await guard.stop()
        assert len(sent) == 1 and 3 * DAY < sent[0] < 4 * DAY
    run(body())
    payload = hold_reminder(3 * DAY + 2 * 3600, NIGHT, "t")
    assert payload["topic"] == "t" and payload["title"] == "GPU held by hand for 3 days 2 h"
    assert "since 2026-10-10 03:00" in payload["message"] and "harness resources resume" in payload["message"]


def test_the_runtime_sends_the_reminder_through_the_notifier(tmp_path):
    m = enabled_manager(tmp_path)
    m.cfg.modules.gpu_guard = True
    rt = m.modules.get("local_model")
    m.guard = _guard(tmp_path)
    m.guard.pause()
    m.notifier.queue = asyncio.Queue()
    rt._hold_reminder(2 * DAY)
    queued = m.notifier.queue.get_nowait()["payload"]
    assert queued["topic"] == "t" and queued["title"] == "GPU held by hand for 2 days 0 h"


# block reasons
def test_the_runner_says_what_blocked_it(tmp_path):
    async def body():
        m = FakeManager()
        runner, _ = _runner(m, tmp_path)
        m.guard.manual = True
        m.guard.manual_until = None
        report = await runner.run("sha")
        assert report.status == "blocked" and "held by hand with no end" in report.reason
        m.guard.manual = False
        m.guard.state = "paused"
        m.guard.reasons = [{"detail": "Hades.exe"}]
        assert runner.blocker() == "the resource guard is paused: Hades.exe"
        m.guard.state = "clear"
        await m.scheduler.acquire("real")
        assert runner.blocker() == "the GPU queue was busy"
        m.scheduler.release("real")
        assert runner.blocker() == "" and runner.can_start()
    run(body())


# the alert test (replaces the staging manual check)
def test_alert_test_returns_the_sample_when_notifications_are_off(tmp_path):
    m = make(tmp_path)
    with TestClient(create_app(m)) as client:
        body = client.post("/canary/alert-test", json={"kind": "silence"}).json()
        assert body["sent"] is False and body["reason"] == "notifications are disabled"
        assert body["notification"]["title"] == "Test: Agent canary has not run for 3 nights"
        assert client.post("/canary/alert-test", json={"kind": "nope"}).status_code == 400


def test_alert_test_publishes_or_reports_the_refusal(tmp_path):
    m = enabled_manager(tmp_path)
    sent = []

    async def accepted(client, payload):
        sent.append(payload)
    m.notifier.publish = accepted
    with TestClient(create_app(m)) as client:
        body = client.post("/canary/alert-test", json={"kind": "regression"}).json()
        assert body["sent"] is True and sent[0]["title"].startswith("Test: Agent canary regressed on")
        assert "compare/ffffffff" in sent[0]["click"]
        assert client.post("/canary/alert-test").json()["notification"]["title"].startswith("Test: Agent canary has")

        async def refused(client, payload):
            raise DeliveryError("ntfy answered 401")
        m.notifier.publish = refused
        response = client.post("/canary/alert-test")
        assert response.status_code == 502 and response.json()["detail"] == "ntfy answered 401"
