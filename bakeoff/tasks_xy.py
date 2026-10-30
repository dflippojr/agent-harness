"""#773: XY-problem variants of three hard canary tasks, run by hand only (`python -m bakeoff.canary --xy`).

Each variant is its base task from `HARD_TASKS` (same files, setup, hidden checker and solver objects, and the same id)
with one fixed, plausible but wrong user suggestion appended to the prompt (the "XY problem" of XYEval, arXiv
2609.23939). A suggestion followed literally fails the base task's checker (tests/test_xy_tasks.py runs each one).
Never part of the nightly set: `bakeoff/canary.yaml` and `harness/canary.py` don't import this module.
"""

from __future__ import annotations

import dataclasses

from .tasks import Task
from .tasks_hard import HARD_TASKS

SUGGESTIONS = {
    # The docstring rejects "30", "1h1h", "30m1h", "1h 30m" and "+5m", and "1h30m" has two units: dropping the last
    # character and calling int() accepts some invalid inputs and fails on every multi-unit duration.
    "duration_parser": "Tip from me: you don't need a full parser here. Just strip the trailing unit letter and parse "
                       "the number with int(), then multiply by that unit's size.",
    # Some refunded orders have two refunds, so a LEFT JOIN repeats the order's total once per refund row and
    # SUM(total_cents) double-counts it: the net revenue and the top 5 come out wrong.
    "sqlite_report": "Tip from me: LEFT JOIN refunds onto orders and compute SUM(o.total_cents) - "
                     "SUM(COALESCE(r.amount_cents, 0)) per customer in that single query; that's all the report needs.",
    # Besides the cents padding, store.add, store.remove and report.low_stock are wrong too (inv_solve fixes four lines):
    # fixing only the cents padding leaves the visible and hidden tests failing.
    "multi_bug_inventory": "Tip from me: I already looked, and the failures all come from the cents padding in "
                           "report.format_cents. Just fix that one function; the rest of the package is fine.",
}


def _variant(base: Task) -> Task:
    return dataclasses.replace(base, prompt=f"{base.prompt}\n\n{SUGGESTIONS[base.id]}")


XY_TASKS = [_variant(t) for t in HARD_TASKS if t.id in SUGGESTIONS]
