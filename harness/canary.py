"""Nightly agent regression canary (#265, docs/canary-evals.md).

A small pinned task set (bakeoff/canary.yaml) runs through the production Manager/runner once a night on the
deployed commit. Results are one row per commit in `canary_results`; a drop in pass rate against the median of the
previous runs sends one ntfy notification. Advisory only: nothing here blocks a deploy.

This module holds the parts that don't need the model: storage, the regression rule, the nightly trigger, the
"one run per commit" bookkeeping and the silence alarm (#732). `bakeoff/canary.py` runs the tasks and is handed in as
`run_suite`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from .db import finish_then_cancel

log = logging.getLogger("harness.canary")

REPO = "dflippojr/agent-harness"
FINAL = ("complete", "timeout", "skipped")  # blocked is the only status the next nightly slot retries
UNRUN = ("blocked", "skipped")  # a nightly slot that produced no result (#732)
OPEN = "pending"  # a slot's outcome from its start until it is recorded; a crash leaves it for the next start
RESTARTED = "the daemon restarted during the slot"
SILENT_SLOTS = 3  # this many unrun slots in a row sends the silence alarm
SHORT_SHA = 8
# The canary's sessions live in these projects (bakeoff/canary.py). They are never resumed after a restart (#316).
HARD_PROJECT, WEB_PROJECT = "canary-hard", "canary-web"
PROJECTS = (HARD_PROJECT, WEB_PROJECT)


class CanaryConfigError(Exception):
    """The suite can't run on this config (a missing suite file or web fixture): the commit is skipped, not retried."""


def deployed_sha(root: Path | None = None) -> str:
    """The commit this daemon runs: what the deployer told it, else the checkout's HEAD."""
    sha = os.environ.get("HARNESS_BUILD_COMMIT", "").strip()
    if sha:
        return sha
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root or Path(__file__).resolve().parent.parent,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class Report:
    """What one canary pass over the suite produced."""
    status: str                                         # complete | timeout | blocked
    outcomes: list[dict] = field(default_factory=list)  # {task, repeat, ok, status, turns, prompt_tokens, seconds, restarts}
    reason: str = ""                                    # blocked: what kept the GPU (a hold, the guard, a busy queue)


WALL_LIMIT = "wall_limit"  # the canary stopped the attempt at the task's wall-clock limit: a graded fail
# The only attempts that are evidence (pass rate, regression rule, confirmation): finished and graded. Everything else
# (timeout, suspended, blocked, cancelled by someone other than the canary) is neither a pass nor a fail.
EVIDENCE = ("done", "failed", WALL_LIMIT)


def valid(outcomes: list[dict]) -> list[dict]:
    return [o for o in outcomes if o.get("status") in EVIDENCE]


def totals(outcomes: list[dict]) -> dict:
    return {"passes": sum(1 for o in outcomes if o["ok"]), "attempts": len(valid(outcomes)),
            "excluded": len(outcomes) - len(valid(outcomes)),
            "turns": sum(o.get("turns", 0) for o in outcomes),
            "prompt_tokens": sum(o.get("prompt_tokens", 0) for o in outcomes),
            "wall_seconds": round(sum(o.get("seconds", 0) for o in outcomes), 1)}


def pass_rate(outcomes: list[dict]) -> float:
    ok = valid(outcomes)
    return sum(1 for o in ok if o["ok"]) / len(ok) if ok else 0.0


def _row(row) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["outcomes"] = json.loads(out.get("outcomes") or "[]")
    return out


async def _write(fn, *args):
    """A CanaryStore write from the event loop. It commits even if a shutdown cancels the caller meanwhile, and the
    cancel is raised after it: through a bare `asyncio.to_thread`, a cancel while the job still waits for a worker
    drops the write (a slot never recorded, an alert sent again)."""
    return await finish_then_cancel(asyncio.to_thread(fn, *args))


class CanaryStore:
    """Writes run on the database's writer thread, one transaction each; reads use a pooled read connection.
    Every method blocks the calling thread: event-loop code calls reads through `asyncio.to_thread` and writes
    through `_write`."""

    def __init__(self, db):
        self.db = db

    def get(self, sha: str) -> dict | None:
        def read():
            return self.db.conn.execute("SELECT * FROM canary_results WHERE sha = ?", (sha,)).fetchone()
        return _row(self.db.read(read))

    def begin(self, sha: str, now: float) -> int:
        """Record a start try for `sha` (one row per commit); returns how many tries it has had."""
        def claim():
            self.db.conn.execute(
                "INSERT INTO canary_results (sha, started_at, status) VALUES (?, ?, 'running') "
                "ON CONFLICT(sha) DO UPDATE SET status = 'running', tries = tries + 1, started_at = excluded.started_at",
                (sha, now))
            return self.db.conn.execute("SELECT tries FROM canary_results WHERE sha = ?", (sha,)).fetchone()[0]
        return self.db.write(claim)

    def abandon(self, sha: str, now: float, note: str) -> None:
        """Mark a row blocked only if it is still `running`: the write is queued behind a completion that is in
        flight, so it can't overwrite a result."""
        def mark():
            self.db.conn.execute("UPDATE canary_results SET status = 'blocked', finished_at = ?, note = ? "
                                 "WHERE sha = ? AND status = 'running'", (now, note, sha))
        self.db.write(mark)

    def first_run(self, sha: str, outcomes: list[dict]) -> None:
        """Keep a completed first run while the confirmation reruns, so a restart can still finish the row with it."""
        def keep():
            self.db.conn.execute("UPDATE canary_results SET outcomes = ? WHERE sha = ? AND status = 'running'",
                                 (json.dumps(outcomes), sha))
        self.db.write(keep)

    def finish(self, sha: str, status: str, outcomes: list[dict], now: float, baseline_sha: str = "",
               baseline_rate: float | None = None, alerted: bool = False, note: str = "") -> None:
        t = totals(outcomes)
        rate = pass_rate(outcomes) if valid(outcomes) else None

        def record():
            self.db.conn.execute(
                "UPDATE canary_results SET status=?, finished_at=?, outcomes=?, passes=?, attempts=?, pass_rate=?, "
                "turns=?, prompt_tokens=?, wall_seconds=?, baseline_sha=?, baseline_rate=?, alerted=?, note=? WHERE sha=?",
                (status, now, json.dumps(outcomes), t["passes"], t["attempts"], rate, t["turns"], t["prompt_tokens"],
                 t["wall_seconds"], baseline_sha, baseline_rate, int(alerted), note, sha))
        self.db.write(record)

    def interrupted(self, now: float) -> list[dict]:
        """At daemon start, finish every row a crash left `running` (#316): with the first run's results if it had
        completed (no alert: an alert is never sent unconfirmed), else `blocked`, so the next slot may retry. Then
        record each slot a crash left open (#732) from how its commit's row ended. Both steps only touch what is
        still unfinished, so a crash during this recovery is finished by the next start."""
        def read():
            return self.db.conn.execute("SELECT * FROM canary_results WHERE status = 'running'").fetchall()
        rows = [row for r in self.db.read(read) if (row := _row(r)) is not None]
        for row in rows:
            if row["outcomes"]:
                note = "daemon restarted during the confirmation rerun: first-run results, no alert"
                self.finish(row["sha"], "complete", row["outcomes"], now, note=note)
            else:
                self.finish(row["sha"], "blocked", [], now, note="daemon restarted mid-run")
        self._close_open_slots(now)
        return rows

    def _close_open_slots(self, now: float) -> None:
        def read():
            return self.db.conn.execute("SELECT * FROM canary_slots WHERE outcome = ? ORDER BY id", (OPEN,)).fetchall()
        for slot in self.db.read(read):
            sha = slot["sha"]
            row = self.get(sha) if sha else None
            # a row that didn't finish during the slot is as it was before it: the crash came before the claim
            untouched = row is None or (row["finished_at"] or 0) < slot["at"]
            outcome, reason = slot_outcome(sha, row if untouched else None, row, RESTARTED if sha and untouched else "")
            self.record_slot(now, sha, outcome, reason)

    def completed_before(self, sha: str, limit: int) -> list[dict]:
        """The newest `limit` completed runs other than `sha`, newest first."""
        def read():
            return self.db.conn.execute(
                "SELECT * FROM canary_results WHERE status = 'complete' AND pass_rate IS NOT NULL AND sha != ? ORDER BY started_at DESC LIMIT ?",
                (sha, limit)).fetchall()
        return [_row(r) for r in self.db.read(read)]

    def latest(self, limit: int) -> list[dict]:
        def read():
            return self.db.conn.execute(
                "SELECT * FROM canary_results WHERE finished_at IS NOT NULL AND pass_rate IS NOT NULL "
                "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in self.db.read(read)]

    def last_complete_at(self) -> float | None:
        """When the newest real result finished: a `complete` row with a pass rate (some attempt was graded)."""
        def read():
            return self.db.conn.execute(
                "SELECT MAX(finished_at) FROM canary_results WHERE status = 'complete' AND pass_rate IS NOT NULL"
            ).fetchone()[0]
        return self.db.read(read)

    # the nightly slots (#732)
    def open_slot(self, at: float, sha: str) -> int:
        """Record a slot as it starts, before anything else it writes. It stays `pending` until `record_slot`, so a
        crash between the slot's writes leaves it for the next start's `interrupted` instead of losing it."""
        def record():
            return self.db.conn.execute("INSERT INTO canary_slots (at, sha, outcome) VALUES (?, ?, ?)",
                                        (at, sha, OPEN)).lastrowid
        return self.db.write(record)

    def record_slot(self, at: float, sha: str, outcome: str, reason: str) -> int:
        """Record how a slot ended: on the open slot for `sha` if there is one, else as a new row."""
        def record():
            slot = self.db.conn.execute("SELECT id FROM canary_slots WHERE outcome = ? AND sha = ? ORDER BY id DESC "
                                        "LIMIT 1", (OPEN, sha)).fetchone()
            if slot is None:
                return self.db.conn.execute("INSERT INTO canary_slots (at, sha, outcome, reason) VALUES (?, ?, ?, ?)",
                                            (at, sha, outcome, reason)).lastrowid
            self.db.conn.execute("UPDATE canary_slots SET at = ?, outcome = ?, reason = ? WHERE id = ?",
                                 (at, outcome, reason, slot[0]))
            return slot[0]
        return self.db.write(record)

    def mark_slot_alerted(self, slot_id: int, now: float) -> None:
        def mark():
            self.db.conn.execute("UPDATE canary_slots SET alerted_at = ? WHERE id = ?", (now, slot_id))
        self.db.write(mark)

    def unrun_streak(self) -> list[dict]:
        """The newest slots that ended blocked or skipped, newest first, up to the last slot that did run. A slot
        still open neither counts nor ends the streak."""
        def read():
            return self.db.conn.execute("SELECT * FROM canary_slots WHERE outcome != ? ORDER BY id DESC LIMIT 400",
                                        (OPEN,)).fetchall()
        streak = []
        for row in self.db.read(read):
            if row["outcome"] not in UNRUN:
                break
            streak.append(dict(row))
        return streak


@dataclass
class Verdict:
    alert: bool
    baseline_rate: float | None = None
    baseline_sha: str = ""
    reason: str = ""


def judge(rate: float, prior: list[dict], *, min_prior: int, drop_points: float) -> Verdict:
    """The regression rule: against the median pass rate of the prior completed runs (`prior`, newest first),
    a drop of `drop_points` or more is a regression. Fewer than `min_prior` runs means no verdict yet."""
    if len(prior) < min_prior:
        return Verdict(False, reason=f"only {len(prior)} prior runs (need {min_prior})")
    baseline = statistics.median(r["pass_rate"] for r in prior)
    nearest = min(prior, key=lambda r: (abs(r["pass_rate"] - baseline), -r["started_at"]))
    drop = (baseline - rate) * 100
    # a hair of tolerance so 3 of 20 attempts (exactly 15 points) isn't lost to float error
    return Verdict(drop >= drop_points - 1e-9, baseline, nearest["sha"],
                   f"{rate:.0%} vs baseline {baseline:.0%} ({drop:+.0f} points)")


def regressed_tasks(outcomes: list[dict], prior: list[dict]) -> list[str]:
    """Tasks that failed this run and did better in the previous runs (their mean pass rate is higher)."""
    out = []
    for task in dict.fromkeys(o["task"] for o in outcomes):
        now = [o["ok"] for o in valid(outcomes) if o["task"] == task]
        if not now or all(now):
            continue
        before = [o["ok"] for r in prior for o in valid(r["outcomes"]) if o["task"] == task]
        if before and sum(before) / len(before) > sum(now) / len(now):
            out.append(task)
    return out


def _unconfirmed(again: Report | None, tasks: list[str]) -> str:
    """Why a confirmation rerun is no evidence ("" if it is): it must complete and finish every rerun task."""
    if again is None:
        return "no task to rerun"
    if again.status != "complete":
        return f"confirmation rerun {again.status}"
    missing = [t for t in tasks if not any(o["task"] == t for o in valid(again.outcomes))]
    return f"confirmation rerun finished no attempt of {', '.join(missing)}" if missing else ""


def compare_url(baseline_sha: str, sha: str) -> str:
    return f"https://github.com/{REPO}/compare/{baseline_sha}...{sha}"


RunSuite = Callable[[str, "list[str] | None"], Awaitable[Report]]  # (sha, only these tasks | None) -> Report


class Canary:
    """Decides whether a run happens, records it, applies the regression rule. `run_suite` does the model work."""

    def __init__(self, store: CanaryStore, run_suite: RunSuite, cfg, notify: Callable[[dict], None],
                 topic: str = "", clock: Callable[[], float] = time.time):
        self.store, self.run_suite, self.cfg, self.notify = store, run_suite, cfg, notify
        self.topic, self.clock = topic, clock

    async def run_for(self, sha: str) -> dict | None:
        """One nightly slot for `sha`. Never more than one finished run per commit; a run that couldn't start is
        retried once at the next slot, then recorded as skipped."""
        if not sha:
            return None
        row = await asyncio.to_thread(self.store.get, sha)
        if row and row["status"] in FINAL:
            return row
        tries = await _write(self.store.begin, sha, self.clock())
        # Every way out after the claim finishes the row, with the best state reached so far: a crash or shutdown
        # in the first run leaves it blocked (the next slot may retry); after that, the first run's results stand.
        status, outcomes, verdict, note = "blocked", [], Verdict(False), ""
        try:
            report = await self.run_suite(sha, None)
            if report.status == "blocked":
                status, note = "blocked" if tries < 2 else "skipped", report.reason or "the GPU was busy"
            else:
                status, outcomes = report.status, report.outcomes
                if report.status == "complete":
                    await _write(self.store.first_run, sha, outcomes)
                    outcomes, verdict = await self._judge(sha, report)
        except CanaryConfigError as e:
            # the same config fails the same way every night: final for this commit, logged once (the row is final)
            status, note = "skipped", str(e)
            log.error("canary %s skipped: %s", sha[:SHORT_SHA], e)
        finally:
            await _write(self.store.finish, sha, status, outcomes, self.clock(), verdict.baseline_sha,
                         verdict.baseline_rate, verdict.alert, note)
        if verdict.alert:
            self._alert(sha, pass_rate(outcomes), verdict)
        return await asyncio.to_thread(self.store.get, sha)

    async def nightly(self, sha: str) -> dict | None:
        """One nightly slot: `run_for`, then the slot's outcome in `canary_slots` and the silence alarm (#732)."""
        before = None
        try:
            await _write(self.store.open_slot, self.clock(), sha)
            before = await asyncio.to_thread(self.store.get, sha) if sha else None
            row = await self.run_for(sha)
        except asyncio.CancelledError:
            # a shutdown mid-slot: run_for has finished the row. Record the slot before stopping, so restarts at slot
            # time can't hide a silence; the notifier is stopping too, so the alarm waits for the next start.
            row = await asyncio.to_thread(self.store.get, sha) if sha else None
            if row and row["status"] == "running":
                # cancelled before run_for could finish its row: finish it here, so the next start's interrupted()
                # doesn't record this slot a second time
                await _write(self.store.abandon, sha, self.clock(), "the daemon stopped during the slot")
                row = await asyncio.to_thread(self.store.get, sha)
            await self._end_slot(sha, before, row, "the daemon stopped during the slot", alarm=False)
            raise
        except Exception as e:
            row = await asyncio.to_thread(self.store.get, sha) if sha else None
            await self._end_slot(sha, before, row, f"the canary failed: {e!r}")
            raise
        await self._end_slot(sha, before, row)
        return row

    async def _end_slot(self, sha: str, before: dict | None, row: dict | None, error: str = "",
                        alarm: bool = True) -> None:
        outcome, reason = slot_outcome(sha, before, row, error)
        await _write(self.store.record_slot, self.clock(), sha, outcome, reason)
        if alarm:
            await self.silence_alarm()

    async def silence_alarm(self) -> None:
        """Send the silence alarm if the last SILENT_SLOTS slots went unrun, at most once a calendar day (tower-local)
        while the silence lasts. Checked after each slot and when the daemon starts."""
        now = self.clock()
        streak = await asyncio.to_thread(self.store.unrun_streak)
        if len(streak) < SILENT_SLOTS:
            return
        today = datetime.fromtimestamp(now).date()
        if any(s["alerted_at"] and datetime.fromtimestamp(s["alerted_at"]).date() == today for s in streak):
            return
        self.notify(silence_alert(streak, self.topic))
        await _write(self.store.mark_slot_alerted, streak[0]["id"], now)

    async def _judge(self, sha: str, report: Report) -> tuple[list[dict], Verdict]:
        prior = await asyncio.to_thread(self.store.completed_before, sha, self.cfg.baseline_runs)
        rule = {"min_prior": self.cfg.min_prior_runs, "drop_points": self.cfg.drop_points}
        outcomes = report.outcomes
        if not valid(outcomes):
            return outcomes, Verdict(False, reason="no attempt finished")
        verdict = judge(pass_rate(outcomes), prior, **rule)
        if not verdict.alert:
            return outcomes, verdict
        # confirmation: rerun only the regressed tasks; with none (e.g. a new task failing without history), every
        # task that failed this run. Their new results replace the old ones. An alert is never sent unconfirmed.
        tasks = regressed_tasks(outcomes, prior) or list(dict.fromkeys(o["task"] for o in valid(outcomes) if not o["ok"]))
        try:
            again = await self.run_suite(sha, tasks) if tasks else None
        except Exception as e:  # e.g. Docker down in a hard task's setup: no confirmation, so no alert
            again, error = None, f"confirmation rerun failed: {e!r}"
        else:
            error = ""
        unusable = error or _unconfirmed(again, tasks)
        if unusable:
            log.warning("canary %s: %s, alert suppressed (%s)", sha[:SHORT_SHA], unusable, verdict.reason)
            return outcomes, Verdict(False, reason=unusable)
        outcomes = [o for o in outcomes if o["task"] not in tasks] + [{**o, "confirm": True} for o in again.outcomes]
        return outcomes, judge(pass_rate(outcomes), prior, **rule)

    def _alert(self, sha: str, rate: float, verdict: Verdict) -> None:
        assert verdict.baseline_rate is not None  # judge() sets the baseline on every alerting verdict
        self.notify(regression_alert(sha, rate, verdict.baseline_sha, verdict.baseline_rate, self.topic))


def slot_outcome(sha: str, before: dict | None, row: dict | None, error: str = "") -> tuple[str, str]:
    """How a nightly slot ended, from the commit's row before the slot (`before`) and after it (`row`)."""
    if row is None or row["status"] == "running":
        return "skipped", error or ("no deployed commit" if not sha else "no result recorded")
    if before and before["status"] in FINAL:
        # nothing ran: the commit already has its one row. Only a skipped one leaves the canary silent.
        outcome = before["status"]
        return outcome, (f"{sha[:SHORT_SHA]} was already skipped ({before['note'] or 'could not start'}); "
                         "the next deployed commit runs" if outcome == "skipped" else f"{sha[:SHORT_SHA]} already ran")
    return row["status"], error or row["note"]


def silence_alert(streak: list[dict], topic: str) -> dict:
    """The silence alarm's ntfy payload: `streak` is the unrun slots, newest first (CanaryStore.unrun_streak)."""
    since = datetime.fromtimestamp(streak[-1]["at"]).strftime("%Y-%m-%d")
    reasons = list(dict.fromkeys(s["reason"] or s["outcome"] for s in streak[:SILENT_SLOTS]))
    return {"topic": topic, "title": f"Agent canary has not run for {len(streak)} nights",
            "message": (f"The last {len(streak)} nightly slots (since {since}) ended blocked or skipped, so no "
                        "agent regression would be noticed.\n" + "\n".join(f"- {r}" for r in reasons)),
            "priority": 4, "tags": ["warning"]}


def regression_alert(sha: str, rate: float, baseline_sha: str, baseline_rate: float, topic: str) -> dict:
    url = compare_url(baseline_sha, sha)
    return {"topic": topic, "title": f"Agent canary regressed on {sha[:SHORT_SHA]}",
            "message": (f"Pass rate {rate:.0%} vs baseline {baseline_rate:.0%} "
                        f"(drop of {(baseline_rate - rate) * 100:.0f} points).\n{url}"),
            "priority": 4, "tags": ["warning"], "click": url}


def sample_alert(kind: str, topic: str, now: float) -> dict:
    """A sample of an alert for the manual alert test (POST /canary/alert-test): the real payload builder on made-up
    input, titled as a test so it is never mistaken for a real alert."""
    if kind == "silence":
        streak = [{"at": now - i * 86400, "outcome": outcome, "reason": reason}
                  for i, (outcome, reason) in enumerate((
                      ("skipped", "the GPU is held by hand with no end (test sample)"),
                      ("blocked", "the resource guard is paused: game running (test sample)"),
                      ("blocked", "the GPU queue was busy (test sample)")))]
        payload = silence_alert(streak, topic)
    elif kind == "regression":
        payload = regression_alert("0" * 40, 0.5, "f" * 40, 0.8, topic)
    else:
        raise ValueError(f"unknown alert kind {kind!r}; use silence or regression")
    return {**payload, "title": f"Test: {payload['title']}", "tags": ["test_tube"]}


def parse_at(at) -> tuple[int, int]:
    """`canary.at` as (hour, minute); ValueError unless it is an "HH:MM" string of a real time of day."""
    parts = at.split(":") if isinstance(at, str) else []
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f"canary.at must be a time of day as \"HH:MM\", got {at!r}")
    hour, minute = (int(p) for p in parts)
    if hour > 23 or minute > 59:
        raise ValueError(f"canary.at must be a time of day as \"HH:MM\", got {at!r}")
    return hour, minute


def next_slot(now: datetime, at: str) -> datetime:
    hour, minute = parse_at(at)
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot if slot > now else slot + timedelta(days=1)


class Nightly:
    """Calls `canary.nightly(deployed sha)` once a night at `cfg.at`, tower-local time."""

    def __init__(self, canary: Canary, cfg, sha: Callable[[], str] = deployed_sha,
                 now: Callable[[], datetime] = datetime.now):
        self.canary, self.cfg, self.sha, self.now = canary, cfg, sha, now
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.cfg.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="canary-nightly")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        try:
            await self.canary.silence_alarm()  # slots a shutdown or crash interrupted were recorded without one (#732)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("canary silence check failed")
        while True:
            try:
                wait = (next_slot(self.now(), self.cfg.at) - self.now()).total_seconds()
            except Exception:  # config.load rejects a bad `at`; a value set some other way must not kill the daemon
                log.exception("canary disabled: can't schedule the nightly run at %r", self.cfg.at)
                return
            await asyncio.sleep(max(1.0, wait))
            try:
                await self.canary.nightly(self.sha())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nightly canary failed")
