"""#674: held-out hard tasks for the harness-learning experiment (#673).

Each candidate's hidden checker must reject the starting files and accept the reference solution. The checkers
run pytest through a sandbox; CI has no Docker, so a local stand-in runs the same commands in the workspace.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bakeoff.sandbox import Sandbox
from bakeoff.tasks import PYTEST, Context, hash_tree, materialize
from bakeoff.tasks_hard import HARD_LIMITS, HARD_TASKS
from bakeoff.tasks_heldout import HELDOUT_TASKS

ROOT = Path(__file__).resolve().parent.parent
# This run's pytest plugins and options must not leak into the checker's pytest.
_SCRUB = ("PYTEST_ADDOPTS", "PYTEST_XDIST", "PYTEST_CURRENT_TEST", "COV_CORE", "COVERAGE_")


class LocalSandbox(Sandbox):
    """`Sandbox.exec` without Docker (never started): the workspace is the working directory, `python` is this
    interpreter."""

    def exec(self, command: str, timeout: int = 60) -> tuple[int, str]:
        command = command.replace("python -m pytest", f'"{sys.executable}" -m pytest', 1)
        env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB)}
        proc = subprocess.run(command, shell=True, cwd=self.workspace, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
        return proc.returncode, proc.stdout + proc.stderr


def _context(ws: Path, task) -> Context:
    materialize(ws, task.files())
    return Context(ws, LocalSandbox(ws), "", hash_tree(ws))


@pytest.mark.parametrize("task", HELDOUT_TASKS, ids=lambda t: t.id)
def test_the_starting_files_fail_the_hidden_checker(task, tmp_path):
    ok, note = task.check(_context(tmp_path, task))
    assert not ok, note


@pytest.mark.parametrize("task", HELDOUT_TASKS, ids=lambda t: t.id)
def test_the_reference_solution_passes(task, tmp_path):
    ctx = _context(tmp_path, task)
    answer = task.solve(ctx)
    ok, note = task.check(Context(ctx.ws, ctx.sandbox, answer, ctx.baseline))
    assert ok, note


@pytest.mark.parametrize("task", [t for t in HELDOUT_TASKS if any(rel.startswith("tests/test_") for rel in t.files())],
                         ids=lambda t: t.id)
def test_the_visible_tests_fail_at_the_start(task, tmp_path):
    """The agent sees a red suite, so there is visibly work to do (the hidden checks go further)."""
    ctx = _context(tmp_path, task)
    code, out = ctx.sandbox.exec(f"{PYTEST} tests", timeout=120)
    assert code != 0, out


def test_editing_the_visible_tests_fails_the_check(tmp_path):
    task = next(t for t in HELDOUT_TASKS if t.id == "ace_pot_ledger")
    ctx = _context(tmp_path, task)
    task.solve(ctx)
    (tmp_path / "tests" / "test_ledger.py").write_text("def test_nothing():\n    pass\n", encoding="utf-8")
    assert task.check(ctx) == (False, "tests were modified")


def test_held_out_tasks_are_new_and_never_in_the_canary():
    ids = [t.id for t in HELDOUT_TASKS]
    assert len(ids) == len(set(ids)) == 6
    assert not set(ids) & {t.id for t in HARD_TASKS}
    suite = yaml.safe_load((ROOT / "bakeoff" / "canary.yaml").read_text(encoding="utf-8"))
    assert not set(ids) & set(suite.get("hard", []) + suite.get("web", []))
    for task in HELDOUT_TASKS:
        assert (task.max_turns, task.wall_limit) == (HARD_LIMITS["max_turns"], HARD_LIMITS["wall_limit"])


def test_the_canary_cli_runs_the_held_out_suite_only_on_request(monkeypatch):
    from bakeoff import canary
    seen = {}

    async def fake_run(task_ids, repeats, fixture, heldout=False):
        seen.update(task_ids=task_ids, repeats=repeats, heldout=heldout)

    monkeypatch.setattr(canary, "run_standalone", fake_run)
    canary.main(["--heldout", "--repeats", "3", "--tasks", "cali_draw"])
    assert seen == {"task_ids": ["cali_draw"], "repeats": 3, "heldout": True}
    suite = canary.heldout_suite(3)
    assert [t.id for t in suite["hard"]] == [t.id for t in HELDOUT_TASKS]
    assert suite["repeats"] == 3 and suite["web"] == []
    assert not {t.id for t in canary.load_suite()["hard"]} & {t.id for t in HELDOUT_TASKS}
