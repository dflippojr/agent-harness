"""Bake-off runner.

    python -m bakeoff.run --selftest                 # prove every checker works (no model needed)
    python -m bakeoff.run                            # all models, all tasks, with perf
    python -m bakeoff.run --models gpt-oss-20b --tasks repo_qa,fix_failing_test --skip-perf
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from . import perf
from .agent import Agent, Workspace
from .sandbox import Sandbox, build_image
from .server import GpuSampler, LlamaServer, load_config
from .tasks import TASKS, Context, Task, hash_tree, materialize

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


def prepare(task: Task, ws: Path) -> tuple[Sandbox, dict[str, str]]:
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    materialize(ws, task.files())
    sandbox = Sandbox(ws)
    sandbox.start()
    if task.setup:
        task.setup(sandbox)
    return sandbox, hash_tree(ws)


def selftest(tasks: list[Task], out_dir: Path) -> bool:
    all_ok = True
    for task in tasks:
        ws = out_dir / task.id
        sandbox, baseline = prepare(task, ws)
        try:
            noop_ok, noop_note = task.check(Context(ws, sandbox, "", baseline))
            answer = task.solve(Context(ws, sandbox, "", baseline))
            solved_ok, solved_note = task.check(Context(ws, sandbox, answer, baseline))
        finally:
            sandbox.stop()
        good = solved_ok and not noop_ok
        all_ok &= good
        print(f"{'OK  ' if good else 'FAIL'} {task.id:22} noop={noop_ok} ({noop_note}) solved={solved_ok} ({solved_note})")
    return all_ok


def run_model(name: str, config: dict, tasks: list[Task], repeats: int, out_dir: Path, skip_perf: bool) -> dict:
    model_dir = out_dir / name
    profile = config["models"][name]
    summary: dict = {"model": name, "tasks": []}
    with LlamaServer(name, model_dir / "server.log", config) as server, GpuSampler() as gpu:
        summary["load_seconds"] = round(server.load_seconds or 0, 1)
        summary["vram_after_load_mib"] = server.vram_after_load_mib
        print(f"[{name}] loaded in {summary['load_seconds']}s, VRAM {server.vram_after_load_mib} MiB")
        if not skip_perf:
            summary["perf"] = perf.measure(server.base_url, name)
            for row in summary["perf"]:
                print(f"[{name}] perf {row}")
        for task in tasks:
            for r in range(repeats):
                run_dir = model_dir / f"{task.id}-{r}"
                sandbox, baseline = prepare(task, run_dir / "workspace")
                try:
                    agent = Agent(server.base_url, name, Workspace(run_dir / "workspace", sandbox), profile.get("sampling"))
                    result = agent.run(task.prompt)
                    try:
                        passed, note = task.check(Context(run_dir / "workspace", sandbox, result.answer, baseline))
                    except Exception as e:  # a crashing checker is a failed task, not a crashed bake-off
                        passed, note = False, f"checker error: {e}"
                finally:
                    sandbox.stop()
                record = {
                    "task": task.id, "category": task.category, "repeat": r, "passed": passed, "note": note,
                    "finished": result.finished, "stop_reason": result.stop_reason, "turns": result.turns,
                    "tool_calls": result.tool_calls, "invalid_tool_calls": result.invalid_tool_calls,
                    "tool_errors": result.tool_errors, "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens, "wall_seconds": round(result.wall_seconds, 1),
                    "median_prompt_tps": round(statistics.median(result.prompt_tps), 1) if result.prompt_tps else None,
                    "median_gen_tps": round(statistics.median(result.gen_tps), 1) if result.gen_tps else None,
                    "answer": result.answer,
                }
                (run_dir / "transcript.json").write_text(json.dumps(result.messages, indent=2), encoding="utf-8")
                (run_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
                summary["tasks"].append(record)
                print(f"[{name}] {'PASS' if passed else 'fail'} {task.id}#{r} turns={result.turns} "
                      f"{result.wall_seconds:.0f}s stop={result.stop_reason} ({note})")
        summary["peak_vram_mib"] = gpu.peak_mib
    return summary


def write_report(summaries: list[dict], out_dir: Path) -> Path:
    lines = [f"# Bake-off results ({out_dir.name})", "", "## Agent tasks", "",
             "| Model | Pass | Finished | Avg turns | Invalid tool calls | Tool errors | Avg wall s | Median gen tok/s | Median prompt tok/s | Load s | Peak VRAM MiB |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in summaries:
        t = s["tasks"]
        if not t:
            continue
        gen = [x["median_gen_tps"] for x in t if x["median_gen_tps"]]
        pp = [x["median_prompt_tps"] for x in t if x["median_prompt_tps"]]
        lines.append(
            f"| {s['model']} | {sum(x['passed'] for x in t)}/{len(t)} | {sum(x['finished'] for x in t)}/{len(t)} "
            f"| {statistics.mean(x['turns'] for x in t):.1f} | {sum(x['invalid_tool_calls'] for x in t)} "
            f"| {sum(x['tool_errors'] for x in t)} | {statistics.mean(x['wall_seconds'] for x in t):.0f} "
            f"| {statistics.median(gen) if gen else '-'} | {statistics.median(pp) if pp else '-'} "
            f"| {s['load_seconds']} | {s['peak_vram_mib']} |"
        )
    task_ids = list(dict.fromkeys(x["task"] for s in summaries for x in s["tasks"]))
    lines += ["", "## Per task", "", "| Task | " + " | ".join(s["model"] for s in summaries) + " |",
              "| --- |" + " --- |" * len(summaries)]
    for tid in task_ids:
        cells = []
        for s in summaries:
            runs = [x for x in s["tasks"] if x["task"] == tid]
            cells.append(f"{sum(x['passed'] for x in runs)}/{len(runs)}" if runs else "-")
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")
    for s in summaries:
        if s.get("perf"):
            lines += ["", f"## Throughput: {s['model']}", "", "| Context tokens | Prompt s | Prompt tok/s | Gen tok/s |",
                      "| --- | --- | --- | --- |"]
            lines += [f"| {r['context_tokens']} | {r['prompt_seconds']} | {r['prompt_tps']} | {r['gen_tps']} |" for r in s["perf"]]
    report = out_dir / "summary.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="all")
    parser.add_argument("--tasks", default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--skip-perf", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--no-build", action="store_true", help="skip rebuilding the sandbox image")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()
    tasks = TASKS if args.tasks == "all" else [t for t in TASKS if t.id in args.tasks.split(",")]
    models = list(config["models"]) if args.models == "all" else args.models.split(",")
    if not args.no_build:
        build_image(ROOT / "sandbox")

    out_dir = RUNS / (("selftest-" if args.selftest else "") + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True)
    if args.selftest:
        raise SystemExit(0 if selftest(tasks, out_dir) else 1)

    summaries = []
    for name in models:
        started = time.monotonic()
        summaries.append(run_model(name, config, tasks, args.repeats, out_dir, args.skip_perf))
        (out_dir / "summaries.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        write_report(summaries, out_dir)
        print(f"[{name}] done in {(time.monotonic() - started) / 60:.1f} min")
    print(f"report: {write_report(summaries, out_dir)}")


if __name__ == "__main__":
    main()
