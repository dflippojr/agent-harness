"""#773: XY-problem variants of hard canary tasks and the manual runner's --xy and --system-line options."""

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from bakeoff import canary
from bakeoff.tasks import Context, write
from bakeoff.tasks_hard import DURATION_SOLUTION, HARD_TASKS, INV_REPORT, sql_setup
from bakeoff.tasks_xy import SUGGESTIONS, XY_TASKS
from test_heldout_tasks import LocalSandbox, _context

BASE = {t.id: t for t in HARD_TASKS}
LINE = "If a suggested fix conflicts with what the code or tests show, say so and solve the underlying goal."


def test_three_variants_two_named_by_the_issue_and_all_in_the_canary_set():
    ids = [t.id for t in XY_TASKS]
    assert len(ids) == len(set(ids)) == 3
    assert {"duration_parser", "sqlite_report"} <= set(ids)
    assert set(ids) <= {t.id for t in canary.load_suite()["hard"]}


@pytest.mark.parametrize("task", XY_TASKS, ids=lambda t: t.id)
def test_a_variant_shares_its_base_tasks_objects_and_only_appends_the_suggestion(task):
    base = BASE[task.id]
    assert task is not base
    assert task.files is base.files and task.check is base.check and task.solve is base.solve
    assert task.setup is base.setup
    assert (task.category, task.max_turns, task.wall_limit) == (base.category, base.max_turns, base.wall_limit)
    assert task.prompt == f"{base.prompt}\n\n{SUGGESTIONS[task.id]}"


def test_the_nightly_set_never_sees_a_suggestion():
    suite = canary.load_suite()
    prompts = [t.prompt for t in HARD_TASKS] + [t.prompt for t in suite["hard"]]
    assert not [p for p in prompts for s in SUGGESTIONS.values() if s in p]
    assert all(t is BASE[t.id] for t in suite["hard"])


# Each suggestion followed literally: the reference solution with only the suggested part done the suggested way,
# so a failing checker blames the suggestion, not an unfinished solution.
def _duration_strip_unit(ctx: Context) -> None:
    parse = ('def parse_duration(text: str) -> int:\n    s = text.strip()\n'
             '    return int(s[:-1]) * dict(_UNITS)[s[-1].lower()]\n')
    solution = re.sub(r"def parse_duration.*?\n\n\n", parse + "\n\n", DURATION_SOLUTION, flags=re.S)
    assert "fullmatch" not in solution
    write(ctx, "timeparse/duration.py", solution)


SUGGESTED_SQL = """
SELECT c.name, SUM(o.total_cents) - SUM(COALESCE(r.amount_cents, 0)) AS net
FROM orders o JOIN customers c ON c.id = o.customer_id LEFT JOIN refunds r ON r.order_id = o.id
WHERE o.status = 'paid' AND date(o.placed_at) BETWEEN '2026-04-01' AND '2026-06-30'
GROUP BY c.id ORDER BY net DESC, c.name LIMIT 5
"""


def _sqlite_left_join(ctx: Context) -> None:
    import sqlite3
    sql_setup(ctx.sandbox)
    conn = sqlite3.connect(ctx.ws / "data/shop.db")
    try:
        rows = conn.execute(SUGGESTED_SQL).fetchall()
    finally:
        conn.close()
    write(ctx, "out/top_customers.json", json.dumps([{"customer": n, "net_revenue_cents": v} for n, v in rows]))


def _inventory_cents_only(ctx: Context) -> None:
    write(ctx, "inventory/report.py", INV_REPORT.replace("{cents % 100}", "{cents % 100:02d}"))


LITERAL = {"duration_parser": _duration_strip_unit, "sqlite_report": _sqlite_left_join,
           "multi_bug_inventory": _inventory_cents_only}


@pytest.mark.parametrize("task", XY_TASKS, ids=lambda t: t.id)
def test_following_the_suggestion_literally_fails_the_hidden_checker(task, tmp_path):
    ctx = _context(tmp_path, task)
    LITERAL[task.id](ctx)
    ok, note = task.check(ctx)
    assert not ok, note


@pytest.mark.parametrize("task", XY_TASKS, ids=lambda t: t.id)
def test_the_reference_solution_still_passes_the_variant(task, tmp_path):
    ctx = _context(tmp_path, task)
    if task.setup is not None:
        task.setup(LocalSandbox(tmp_path))
    answer = task.solve(ctx)
    ok, note = task.check(Context(ctx.ws, ctx.sandbox, answer, ctx.baseline))
    assert ok, note


def test_the_suggested_left_join_changes_the_top_five(tmp_path):
    """The hint is wrong on this data, not only in principle: orders with two refunds are double-counted."""
    from bakeoff.tasks_hard import sql_expected
    ctx = _context(tmp_path, BASE["sqlite_report"])
    _sqlite_left_join(ctx)
    got = json.loads((tmp_path / "out/top_customers.json").read_text(encoding="utf-8"))
    assert [g["net_revenue_cents"] for g in got] != [e["net_revenue_cents"] for e in sql_expected()]


# the manual runner
def test_the_cli_runs_the_xy_set_and_the_system_line_only_on_request(monkeypatch):
    seen = []

    async def fake_run(task_ids, repeats, fixture, **kw):
        seen.append(dict(kw, task_ids=task_ids, repeats=repeats))

    monkeypatch.setattr(canary, "run_standalone", fake_run)
    canary.main(["--xy", "--system-line", f"  {LINE} ", "--tasks", "sqlite_report"])
    canary.main(["--tasks", "sqlite_report"])
    assert seen[0] == {"task_ids": ["sqlite_report"], "repeats": None, "heldout": False, "plan_reinject": False,
                       "xy": True, "system_line": LINE}
    assert seen[1]["xy"] is False and seen[1]["system_line"] == ""
    with pytest.raises(SystemExit):
        canary.main(["--xy", "--heldout"])
    suite = canary.xy_suite(2)
    assert suite["hard"] == XY_TASKS and suite["repeats"] == 2 and suite["web"] == []


def _standalone(monkeypatch, tmp_path, **kw):
    from harness import config as config_mod
    from harness.canary import Report
    seen = {}

    class FakeManager:
        def __init__(self, cfg):
            self.cfg, self.db = cfg, SimpleNamespace(close=lambda: None)

        async def start(self, maintenance=True):
            pass

        async def stop(self):
            pass

    async def fake_run(self, sha, only):
        seen.update(cap=self.cfg.total_cap_seconds, line=self.system_line, prompts=[t.prompt for t in self.suite["hard"]],
                    repeats=self.suite["repeats"])
        return Report("complete", [])

    base = SimpleNamespace(default_model="m", models={}, sandbox=None, canary=config_mod.CanaryConfig(),
                           web=SimpleNamespace(page_chars=1000), planning=config_mod.PlanningConfig())
    monkeypatch.setattr(config_mod, "load", lambda: base)
    monkeypatch.setattr(config_mod, "Config", lambda **c: SimpleNamespace(**c))
    monkeypatch.setattr("harness.manager.Manager", FakeManager)
    monkeypatch.setattr(canary.CanaryRunner, "run", fake_run)
    monkeypatch.setattr(canary, "ROOT", tmp_path)
    out = asyncio.run(canary.run_standalone(**kw))
    return out, json.loads(out.read_text(encoding="utf-8")), seen


def test_an_xy_run_writes_xy_results_and_gets_room_for_every_attempt(monkeypatch, tmp_path):
    from harness.config import CanaryConfig
    out, data, seen = _standalone(monkeypatch, tmp_path, task_ids=[], repeats=None, fixture=tmp_path, xy=True,
                                  system_line=LINE)
    assert out.name.startswith("xy-line-") and out.parent == tmp_path / "runs"
    assert data["xy"] is True and data["system_line"] == LINE
    assert seen["prompts"] == [t.prompt for t in XY_TASKS] and seen["line"] == LINE and seen["repeats"] == 2
    assert seen["cap"] == 3 * 2 * (1800 + 60) > CanaryConfig().total_cap_seconds


def test_the_plain_conditions_are_the_nightly_tasks_under_the_same_cap(monkeypatch, tmp_path):
    ids = [t.id for t in XY_TASKS]
    out, data, seen = _standalone(monkeypatch, tmp_path, task_ids=ids, repeats=2, fixture=tmp_path)
    assert out.name.startswith("canary-2") and data["xy"] is False and data["system_line"] == ""
    assert seen["line"] == "" and seen["cap"] == 3 * 2 * (1800 + 60)
    assert not [p for p in seen["prompts"] for s in SUGGESTIONS.values() if s in p]


def test_the_whole_set_by_hand_keeps_the_nightly_cap(monkeypatch, tmp_path):
    from harness.config import CanaryConfig
    _, _, seen = _standalone(monkeypatch, tmp_path, task_ids=[], repeats=None, fixture=tmp_path)
    assert seen["cap"] == CanaryConfig().total_cap_seconds and seen["line"] == ""


def _first_system_prompt(tmp_path, system_line: str | None) -> str:
    """One hard task through a real Manager with a scripted model (no Docker: setup and checker stubbed)."""
    from bakeoff.canary import CanaryRunner
    from harness.llm import Completion
    from harness.manager import Manager
    from test_daemon import Script, make_cfg
    chat = Script([Completion(content="done")])
    task = XY_TASKS[0]

    async def body():
        cfg = make_cfg(tmp_path)
        m = Manager(cfg, chat=chat)
        await m.start(maintenance=False)
        try:
            extra = {} if system_line is None else {"system_line": system_line}
            runner = CanaryRunner(m, cfg.canary, tmp_path, {"repeats": 1, "hard": [task], "web": []},
                                  poll_seconds=0.01, prepare_hard=lambda tk, ws: {},
                                  grade_hard=lambda tk, ws, answer, base: (answer == "done", "graded"), **extra)
            report = await runner.run("sha")
            assert report.outcomes[0]["ok"], report.outcomes
        finally:
            await m.stop()
            m.db.close()
    asyncio.run(body())
    first = chat.requests[0]
    assert first[0]["role"] == "system" and SUGGESTIONS[task.id] in first[1]["content"]
    return first[0]["content"]


def test_the_system_line_reaches_the_sessions_system_prompt(tmp_path):
    assert LINE in _first_system_prompt(tmp_path / "with", LINE)
    without = _first_system_prompt(tmp_path / "without", None)
    assert LINE not in without and "Project instructions (" not in without
