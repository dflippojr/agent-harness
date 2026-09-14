"""Re-grade a finished run with the current checkers, without re-running any model.

    python -m bakeoff.rescore runs/20260914-102032 [--tasks flaky_shared_state,merge_conflict]

Use after fixing a checker bug. Each saved workspace is checked again against a baseline rebuilt from a fresh copy
of the task fixture; result.json, summaries.json and summary.md are rewritten and changed verdicts are printed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .run import prepare, write_report
from .sandbox import Sandbox
from .tasks import TASKS, Context
from .tasks_hard import HARD_TASKS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--tasks", default="all")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    by_id = {t.id: t for t in TASKS + HARD_TASKS}
    wanted = None if args.tasks == "all" else set(args.tasks.split(","))
    summaries_path = args.run_dir / "summaries.json"
    summaries = json.loads(summaries_path.read_text(encoding="utf-8"))
    baselines: dict[str, dict[str, str]] = {}

    for summary in summaries:
        model_dir = args.run_dir / summary["model"].split("/")[-1]  # reference runs label models "<harness>/<model>"
        for record in summary["tasks"]:
            if wanted and record["task"] not in wanted:
                continue
            task = by_id[record["task"]]
            if task.id not in baselines:
                tmp = Path(tempfile.mkdtemp(prefix="rescore-"))
                sandbox, baselines[task.id] = prepare(task, tmp / "ws")
                sandbox.stop()
                shutil.rmtree(tmp, ignore_errors=True)
            run_dir = model_dir / f"{task.id}-{record['repeat']}"
            ws = run_dir / "workspace"
            sandbox = Sandbox(ws)
            sandbox.start()
            try:
                passed, note = task.check(Context(ws, sandbox, record.get("answer", ""), baselines[task.id]))
            except Exception as e:
                passed, note = False, f"checker error: {e}"
            finally:
                sandbox.stop()
            if passed != record["passed"]:
                print(f"{summary['model']} {task.id}#{record['repeat']}: {record['passed']} -> {passed} ({note})")
            record.update(passed=passed, note=note)
            (run_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    summaries_path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print(f"report: {write_report(summaries, args.run_dir)}")


if __name__ == "__main__":
    main()
