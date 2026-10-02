"""Nightly agent regression canary (#265, docs/canary-evals.md).

A small pinned task set (bakeoff/canary.yaml) runs through the production Manager/runner once a night on the
deployed commit. Results are one row per commit in `canary_results`; a drop in pass rate against the median of the
previous runs sends one ntfy notification. Advisory only: nothing here blocks a deploy.

This module holds the parts that don't need the model: storage, the regression rule, the nightly trigger and the
"one run per commit" bookkeeping. `bakeoff/canary.py` runs the tasks and is handed in as `run_suite`.
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

log = logging.getLogger("harness.canary")

REPO = "dflippojr/agent-harness"
FINAL = ("complete", "timeout", "skipped")  # blocked is the only status the next nightly slot retries
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


class CanaryStore:
    def __init__(self, db):
        self.db = db

    def get(self, sha: str) -> dict | None:
        with self.db.lock:
            row = self.db.conn.execute("SELECT * FROM canary_results WHERE sha = ?", (sha,)).fetchone()
        return _row(row)

    def begin(self, sha: str, now: float) -> int:
        """Record a start try for `sha` (one row per commit); returns how many tries it has had."""
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO canary_results (sha, started_at, status) VALUES (?, ?, 'running') "
                "ON CONFLICT(sha) DO UPDATE SET status = 'running', tries = tries + 1, started_at = excluded.started_at",
                (sha, now))
            return self.db.conn.execute("SELECT tries FROM canary_results WHERE sha = ?", (sha,)).fetchone()[0]

    def first_run(self, sha: str, outcomes: list[dict]) -> None:
        """Keep a completed first run while the confirmation reruns, so a restart can still finish the row with it."""
        with self.db.lock:
            self.db.conn.execute("UPDATE canary_results SET outcomes = ? WHERE sha = ? AND status = 'running'",
                                 (json.dumps(outcomes), sha))

    def finish(self, sha: str, status: str, outcomes: list[dict], now: float, baseline_sha: str = "",
               baseline_rate: float | None = None, alerted: bool = False, note: str = "") -> None:
        t = totals(outcomes)
        rate = pass_rate(outcomes) if valid(outcomes) else None
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE canary_results SET status=?, finished_at=?, outcomes=?, passes=?, attempts=?, pass_rate=?, "
                "turns=?, prompt_tokens=?, wall_seconds=?, baseline_sha=?, baseline_rate=?, alerted=?, note=? WHERE sha=?",
                (status, now, json.dumps(outcomes), t["passes"], t["attempts"], rate, t["turns"], t["prompt_tokens"],
                 t["wall_seconds"], baseline_sha, baseline_rate, int(alerted), note, sha))

    def interrupted(self, now: float) -> list[dict]:
        """At daemon start, finish every row a crash left `running` (#316): with the first run's results if it had
        completed (no alert: an alert is never sent unconfirmed), else `blocked`, so the next slot may retry."""
        with self.db.lock:
            rows = [_row(r) for r in self.db.conn.execute(
                "SELECT * FROM canary_results WHERE status = 'running'").fetchall()]
        for row in rows:
            if row["outcomes"]:
                self.finish(row["sha"], "complete", row["outcomes"], now,
                            note="daemon restarted during the confirmation rerun: first-run results, no alert")
            else:
                self.finish(row["sha"], "blocked", [], now, note="daemon restarted mid-run")
        return rows

    def completed_before(self, sha: str, limit: int) -> list[dict]:
        """The newest `limit` completed runs other than `sha`, newest first."""
        with self.db.lock:
            rows = self.db.conn.execute(
                "SELECT * FROM canary_results WHERE status = 'complete' AND pass_rate IS NOT NULL AND sha != ? ORDER BY started_at DESC LIMIT ?",
                (sha, limit)).fetchall()
        return [_row(r) for r in rows]

    def latest(self, limit: int) -> list[dict]:
        with self.db.lock:
            rows = self.db.conn.execute(
                "SELECT * FROM canary_results WHERE finished_at IS NOT NULL AND pass_rate IS NOT NULL "
                "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]


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
        row = self.store.get(sha)
        if row and row["status"] in FINAL:
            return row
        tries = self.store.begin(sha, self.clock())
        # Every way out after the claim finishes the row, with the best state reached so far: a crash or shutdown
        # in the first run leaves it blocked (the next slot may retry); after that, the first run's results stand.
        status, outcomes, verdict, note = "blocked", [], Verdict(False), ""
        try:
            report = await self.run_suite(sha, None)
            if report.status == "blocked":
                status = "blocked" if tries < 2 else "skipped"
            else:
                status, outcomes = report.status, report.outcomes
                if report.status == "complete":
                    self.store.first_run(sha, outcomes)
                    outcomes, verdict = await self._judge(sha, report)
        except CanaryConfigError as e:
            # the same config fails the same way every night: final for this commit, logged once (the row is final)
            status, note = "skipped", str(e)
            log.error("canary %s skipped: %s", sha[:SHORT_SHA], e)
        finally:
            self.store.finish(sha, status, outcomes, self.clock(), verdict.baseline_sha, verdict.baseline_rate,
                              verdict.alert, note)
        if verdict.alert:
            self._alert(sha, pass_rate(outcomes), verdict)
        return self.store.get(sha)

    async def _judge(self, sha: str, report: Report) -> tuple[list[dict], Verdict]:
        prior = self.store.completed_before(sha, self.cfg.baseline_runs)
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
        url = compare_url(verdict.baseline_sha, sha)
        self.notify({"topic": self.topic, "title": f"Agent canary regressed on {sha[:SHORT_SHA]}",
                     "message": (f"Pass rate {rate:.0%} vs baseline {verdict.baseline_rate:.0%} "
                                 f"(drop of {(verdict.baseline_rate - rate) * 100:.0f} points).\n{url}"),
                     "priority": 4, "tags": ["warning"], "click": url})


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
    """Calls `canary.run_for(deployed sha)` once a night at `cfg.at`, tower-local time."""

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
        while True:
            try:
                wait = (next_slot(self.now(), self.cfg.at) - self.now()).total_seconds()
            except Exception:  # config.load rejects a bad `at`; a value set some other way must not kill the daemon
                log.exception("canary disabled: can't schedule the nightly run at %r", self.cfg.at)
                return
            await asyncio.sleep(max(1.0, wait))
            try:
                await self.canary.run_for(self.sha())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nightly canary failed")
