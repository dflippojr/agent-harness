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


def totals(outcomes: list[dict]) -> dict:
    return {"passes": sum(1 for o in outcomes if o["ok"]), "attempts": len(outcomes),
            "turns": sum(o.get("turns", 0) for o in outcomes),
            "prompt_tokens": sum(o.get("prompt_tokens", 0) for o in outcomes),
            "wall_seconds": round(sum(o.get("seconds", 0) for o in outcomes), 1)}


def pass_rate(outcomes: list[dict]) -> float:
    return sum(1 for o in outcomes if o["ok"]) / len(outcomes) if outcomes else 0.0


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

    def finish(self, sha: str, status: str, outcomes: list[dict], now: float, baseline_sha: str = "",
               baseline_rate: float | None = None, alerted: bool = False) -> None:
        t = totals(outcomes)
        rate = pass_rate(outcomes) if outcomes else None
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE canary_results SET status=?, finished_at=?, outcomes=?, passes=?, attempts=?, pass_rate=?, "
                "turns=?, prompt_tokens=?, wall_seconds=?, baseline_sha=?, baseline_rate=?, alerted=? WHERE sha=?",
                (status, now, json.dumps(outcomes), t["passes"], t["attempts"], rate, t["turns"], t["prompt_tokens"],
                 t["wall_seconds"], baseline_sha, baseline_rate, int(alerted), sha))

    def completed_before(self, sha: str, limit: int) -> list[dict]:
        """The newest `limit` completed runs other than `sha`, newest first."""
        with self.db.lock:
            rows = self.db.conn.execute(
                "SELECT * FROM canary_results WHERE status = 'complete' AND sha != ? ORDER BY started_at DESC LIMIT ?",
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
        now = [o["ok"] for o in outcomes if o["task"] == task]
        if all(now):
            continue
        before = [o["ok"] for r in prior for o in r["outcomes"] if o["task"] == task]
        if before and sum(before) / len(before) > sum(now) / len(now):
            out.append(task)
    return out


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
        try:
            report = await self.run_suite(sha, None)
        except BaseException:
            self.store.finish(sha, "blocked", [], self.clock())  # a crash or shutdown: the next slot may retry
            raise
        if report.status == "blocked":
            self.store.finish(sha, "blocked" if tries < 2 else "skipped", [], self.clock())
            return self.store.get(sha)
        outcomes, verdict = report.outcomes, Verdict(False)
        if report.status == "complete":
            outcomes, verdict = await self._judge(sha, report)
        self.store.finish(sha, report.status, outcomes, self.clock(), verdict.baseline_sha, verdict.baseline_rate,
                          verdict.alert)
        if verdict.alert:
            self._alert(sha, pass_rate(outcomes), verdict)
        return self.store.get(sha)

    async def _judge(self, sha: str, report: Report) -> tuple[list[dict], Verdict]:
        prior = self.store.completed_before(sha, self.cfg.baseline_runs)
        rule = {"min_prior": self.cfg.min_prior_runs, "drop_points": self.cfg.drop_points}
        outcomes = report.outcomes
        verdict = judge(pass_rate(outcomes), prior, **rule)
        if not verdict.alert:
            return outcomes, verdict
        tasks = regressed_tasks(outcomes, prior)
        if tasks:  # confirmation: rerun only the regressed tasks; their new results replace the old ones
            again = await self.run_suite(sha, tasks)
            if again.status == "blocked" or not again.outcomes:
                return outcomes, Verdict(False, reason="confirmation rerun could not run")
            outcomes = [o for o in outcomes if o["task"] not in tasks] + [{**o, "confirm": True} for o in again.outcomes]
            verdict = judge(pass_rate(outcomes), prior, **rule)
        return outcomes, verdict

    def _alert(self, sha: str, rate: float, verdict: Verdict) -> None:
        url = compare_url(verdict.baseline_sha, sha)
        self.notify({"topic": self.topic, "title": f"Agent canary regressed on {sha[:SHORT_SHA]}",
                     "message": (f"Pass rate {rate:.0%} vs baseline {verdict.baseline_rate:.0%} "
                                 f"(drop of {(verdict.baseline_rate - rate) * 100:.0f} points).\n{url}"),
                     "priority": 4, "tags": ["warning"], "click": url})


def next_slot(now: datetime, at: str) -> datetime:
    hour, minute = (int(x) for x in at.split(":"))
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
            await asyncio.sleep(max(1.0, (next_slot(self.now(), self.cfg.at) - self.now()).total_seconds()))
            try:
                await self.canary.run_for(self.sha())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nightly canary failed")
