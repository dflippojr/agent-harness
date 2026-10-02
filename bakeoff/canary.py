"""Nightly regression canary: a pinned task set run through the production Manager and runner (#265).

Unlike the other bake-off suites this goes through `harness.manager.Manager` (real system prompt, tool schemas,
compaction, masking, policy), never through `bakeoff/agent.py`, so a harness change shows up in the pass rate.
The daemon calls `CanaryRunner.run` from its nightly trigger (harness/canary.py). By hand, with the daemon stopped:

    python -m bakeoff.canary                       # the whole set, repeats from bakeoff/canary.yaml
    python -m bakeoff.canary --tasks sqlite_report --repeats 1

Hard tasks are written into the session's workspace and graded by their existing hidden checkers (Docker sandbox, as
in the bake-off). Web tasks replay the recorded web (web_suite.py) through a per-session WebTools.

The canary is a low-priority session: it starts only while the GPU slot is free, nobody is queued and the guard is
clear (checked before every task), and between turns it steps aside for any real session (Runner._gpu_gate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import yaml

from harness.canary import Report

from .tasks import Context, hash_tree, materialize
from .tasks_hard import HARD_TASKS
from .web_suite import DEFAULT_FIXTURE, TASKS as WEB_TASKS, ungrounded_quotes

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("canary.yaml")
HARD_PROJECT, WEB_PROJECT = "canary-hard", "canary-web"
MAX_RESTARTS = 2  # a task attempt that was suspended mid-way restarts, up to this many times
DONE = ("done", "failed", "cancelled")


def load_suite(path: Path = SUITE) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    hard = {t.id: t for t in HARD_TASKS}
    web = {t.id: t for t in WEB_TASKS}
    unknown = [i for i in raw.get("hard", []) if i not in hard] + [i for i in raw.get("web", []) if i not in web]
    if unknown:
        raise ValueError(f"{path.name}: unknown task ids {unknown}")
    return {"repeats": int(raw.get("repeats", 2)), "hard": [hard[i] for i in raw.get("hard", [])],
            "web": [web[i] for i in raw.get("web", [])]}


class CanaryRunner:
    """Runs the suite on a live Manager. `clock`/`sleep` are injectable so tests don't wait."""

    def __init__(self, manager, cfg, fixture: Path = DEFAULT_FIXTURE, suite: dict | None = None,
                 poll_seconds: float = 5, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep,
                 grade_hard: Callable | None = None, prepare_hard: Callable | None = None):
        self.m, self.cfg, self.fixture = manager, cfg, fixture
        self.suite = suite or load_suite(Path(ROOT / cfg.suite) if cfg.suite else SUITE)
        self.poll, self.clock, self.sleep = poll_seconds, clock, sleep
        self._grade_hard = grade_hard or grade_hard_task
        self._prepare_hard = prepare_hard or prepare_hard_task

    # may a run start now
    def can_start(self) -> bool:
        m, g = self.m, self.m.guard
        if not m.scheduler.idle() or m.runner.generating or m.runner.gate.busy or m.runner.gate.exclusive:
            return False
        if m.images is not None and (m.images.gpu_taken or m.images.phase != "idle"):
            return False
        return g is None or (g.state == "clear" and not g.manual)

    async def _wait_until_free(self, deadline: float) -> bool:
        while not self.can_start():
            if self.clock() >= deadline:
                return False
            await self.sleep(self.poll)
        return True

    # the suite
    async def run(self, sha: str, only: list[str] | None = None) -> Report:
        from harness.config import Project, WebConfig
        from harness.web_tools import WebTools
        cap = self.clock() + self.cfg.total_cap_seconds
        plan = [(t, "hard") for t in self.suite["hard"] if not only or t.id in only]
        plan += [(t, "web") for t in self.suite["web"] if not only or t.id in only]
        projects = self.m.cfg.projects
        projects[HARD_PROJECT] = Project(name=HARD_PROJECT, web=False, memory_library=False, images=False,
                                         session_search=False)
        projects[WEB_PROJECT] = Project(name=WEB_PROJECT, web=True, memory_library=False, images=False,
                                        session_search=False)
        web = WebTools(WebConfig(enabled=True, page_chars=self.m.cfg.web.page_chars, fixture_dir=str(self.fixture)))
        outcomes: list[dict] = []
        try:
            if not await self._wait_until_free(self.clock() + self.cfg.start_wait_seconds):
                return Report("blocked")
            for task, kind in plan:
                for repeat in range(self.suite["repeats"]):
                    if self.clock() >= cap or not await self._wait_until_free(cap):
                        outcomes.append(_missing(task.id, repeat, "timeout"))
                        continue
                    outcomes.append(await self._attempt(task, kind, repeat, web, cap))
        finally:
            projects.pop(HARD_PROJECT, None)
            projects.pop(WEB_PROJECT, None)
        timed_out = any(o["status"] == "timeout" for o in outcomes)
        return Report("timeout" if timed_out else "complete", outcomes)

    async def _attempt(self, task, kind: str, repeat: int, web, cap: float) -> dict:
        res: dict = {}
        for restart in range(MAX_RESTARTS + 1):
            res = await self._once(task, kind, repeat, web, cap)
            res["restarts"] = restart
            if not res.pop("suspended"):
                break
            if restart == MAX_RESTARTS:  # restart budget used and still suspended: not a valid pass or fail
                res["status"], res["ok"], res["note"] = "suspended", False, "suspended on every attempt"
                break
            if self.clock() >= cap or not await self._wait_until_free(cap):
                res["status"], res["ok"] = "timeout", False
                break
        return res

    async def _once(self, task, kind: str, repeat: int, web, cap: float) -> dict:
        m = self.m
        started = self.clock()
        project = HARD_PROJECT if kind == "hard" else WEB_PROJECT
        s = m.create(task.prompt, project=project, title=f"canary {task.id} #{repeat}")
        sid = s["id"]
        # Everything up to the first await runs before the session's task does: the workspace is ready when it starts.
        m.scheduler.low_priority.add(sid)
        if kind == "web":
            m.runner.web_overrides[sid] = web
        else:
            baseline = self._prepare_hard(task, Path(s["workspace"]))
            run = dict(m.db.get_session(sid)["run"], max_turns=task.max_turns)
            m.db.update_session(sid, run=run)
        limit = task.wall_limit if kind == "hard" else 1500
        try:
            while m.db.get_session(sid)["status"] not in DONE:
                await self.sleep(1 if self.poll > 1 else self.poll)
                if self.clock() - started > limit:
                    await m.cancel(sid)
                    break
            final = m.db.get_session(sid)
            suspended = bool(m.runner.yields.pop(sid, 0)) or any(
                e["type"] == "gpu_paused" for e in m.db.events(sid))
            if kind == "hard":
                ok, note = await asyncio.to_thread(self._grade_hard, task, Path(final["workspace"]), final["answer"],
                                                   baseline)
            else:
                ok, note = task.check(final["answer"])
                results = [e["data"] for e in m.db.events(sid) if e["type"] == "tool_result"]
                made_up = ungrounded_quotes(final["answer"], [r["output"] for r in results])
                if made_up:
                    ok, note = False, f"{note}; quotes not in any fetched text"
            totals = final["totals"]
            return {"task": task.id, "repeat": repeat, "ok": bool(ok and final["status"] == "done"), "note": note,
                    "status": final["status"], "turns": totals.get("turns", 0),
                    "prompt_tokens": totals.get("prompt_tokens", 0), "seconds": round(self.clock() - started, 1),
                    "suspended": suspended}
        finally:
            m.scheduler.low_priority.discard(sid)
            m.runner.web_overrides.pop(sid, None)


def _missing(task_id: str, repeat: int, status: str) -> dict:
    return {"task": task_id, "repeat": repeat, "ok": False, "note": status, "status": status, "turns": 0,
            "prompt_tokens": 0, "seconds": 0, "restarts": 0}


# hard tasks: fixtures in, hidden checker out (Docker sandbox, as in the bake-off)
def prepare_hard_task(task, ws: Path) -> dict[str, str]:
    from .sandbox import Sandbox
    materialize(ws, task.files())
    if task.setup is not None:
        with Sandbox(ws) as sandbox:
            task.setup(sandbox)
    return hash_tree(ws)


def grade_hard_task(task, ws: Path, answer: str, baseline: dict[str, str]) -> tuple[bool, str]:
    from .sandbox import Sandbox
    try:
        with Sandbox(ws) as sandbox:
            return task.check(Context(ws, sandbox, answer, baseline))
    except Exception as e:  # a broken checker or Docker must not take the whole run down
        return False, f"checker error: {e}"


# by hand, daemon stopped
async def run_standalone(task_ids: list[str], repeats: int | None, fixture: Path) -> Path:
    from harness import config as config_mod
    from harness.manager import Manager

    base = config_mod.load()
    tmp = Path(tempfile.mkdtemp(prefix="canary-"))
    cfg = config_mod.Config(host="127.0.0.1", port=0, data_dir=tmp, repos_dir=tmp / "repos",
                            default_model=base.default_model, models=base.models, sandbox=base.sandbox,
                            projects={"scratch": config_mod.Project(name="scratch")}, canary=base.canary,
                            web=config_mod.WebConfig(enabled=True, page_chars=base.web.page_chars,
                                                     fixture_dir=str(fixture)))
    m = Manager(cfg)
    await m.start(maintenance=False)
    try:
        suite = load_suite()
        if repeats:
            suite["repeats"] = repeats
        report = await CanaryRunner(m, cfg.canary, fixture, suite).run("manual", task_ids or None)
    finally:
        await m.stop()
        m.db.close()
        shutil.rmtree(tmp, ignore_errors=True)
    out = ROOT / "runs" / f"canary-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"status": report.status, "outcomes": report.outcomes}, indent=1), encoding="utf-8")
    passed = sum(o["ok"] for o in report.outcomes)
    print(f"{report.status}: {passed}/{len(report.outcomes)} passed · results in {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bakeoff.canary")
    parser.add_argument("--tasks", default="")
    parser.add_argument("--repeats", type=int, default=0)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)
    asyncio.run(run_standalone([t for t in args.tasks.split(",") if t], args.repeats or None, args.fixture))
    return 0


if __name__ == "__main__":
    sys.exit(main())
