"""Bake-off arm for hosted Claude Code and Codex: the same tasks under `tool_mode` builtin and split (#427).

    python -m bakeoff.hosted --backends claude,codex --modes builtin,split --suite hard --repeats 3

Each run is one session on a throwaway Manager (own data dir, own sandbox networks, port 0), so it never touches the
running harness. It does use the backend's real login, which the owner runs it with: it spends subscription
quota. Reports pass rate, turns, tokens and wall time per backend, mode and task.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import replace
from pathlib import Path

from .canary import DONE, grade_hard_task, prepare_hard_task
from .tasks import TASKS
from .tasks_hard import HARD_TASKS

ROOT = Path(__file__).resolve().parent.parent
SUITES = {"core": TASKS, "hard": HARD_TASKS}


def throwaway_config(run_dir: Path, backend_name: str, backend, mode: str):
    """A config with one hosted backend in `mode`; nothing is read from or written to the production data."""
    from harness.config import Config, ModelConfig, Project, SandboxConfig
    token = uuid.uuid4().hex[:10]
    return Config(host="127.0.0.1", port=0, data_dir=run_dir / "manager", repos_dir=run_dir / "repos",
                  default_model="unused", models={"unused": ModelConfig(name="unused", base_url="http://unused")},
                  sandbox=SandboxConfig(network=f"bakeoff-sandbox-{token}", egress_network=f"bakeoff-egress-{token}"),
                  projects={"scratch": Project(name="scratch")},
                  backends={backend_name: replace(backend, enabled=True, tool_mode=mode)})


async def run_hosted(task, backend_name: str, mode: str, run_dir: Path, backend, *, poll: float = 1.0,
                     manager_factory=None) -> dict:
    """One task on one hosted backend in one mode: the result row the report aggregates."""
    from harness.manager import Manager
    cfg = throwaway_config(run_dir, backend_name, backend, mode)
    m = (manager_factory or Manager)(cfg)
    await m.start()
    started = time.monotonic()
    try:
        s = m.create(task.prompt, project="scratch", backend=backend_name, title=f"bakeoff {task.id} {mode}")
        sid = s["id"]
        baseline = prepare_hard_task(task, Path(s["workspace"]))  # before the session's first await
        m.db.update_session(sid, run=dict(m.db.get_session(sid)["run"], max_turns=task.max_turns))
        stopped = False
        while m.db.get_session(sid)["status"] not in DONE:
            await asyncio.sleep(poll)
            if time.monotonic() - started > task.wall_limit:
                stopped = True
                await m.cancel(sid)
                break
        final = m.db.get_session(sid)
        ok, note = await asyncio.to_thread(grade_hard_task, task, Path(final["workspace"]), final["answer"], baseline)
        totals = final["totals"]
        tools = [e for e in m.db.events(sid) if e["type"] == "tool_call"]
        return {"backend": backend_name, "mode": mode, "task": task.id, "ok": bool(ok and final["status"] == "done"),
                "note": note + ("; stopped at the wall limit" if stopped else ""), "status": final["status"],
                "turns": totals.get("turns", 0),
                "tokens": totals.get("prompt_tokens", 0) + totals.get("completion_tokens", 0),
                "tool_calls": len(tools), "seconds": round(time.monotonic() - started, 1)}
    finally:
        await m.stop()
        m.db.close()


def summarize(rows: list[dict]) -> list[dict]:
    """Per backend and mode (and per task): pass rate, mean turns, mean tokens, mean wall seconds."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        for key in ((row["backend"], row["mode"], "*"), (row["backend"], row["mode"], row["task"])):
            groups.setdefault(key, []).append(row)
    return [{"backend": b, "mode": mode, "task": task, "runs": len(g), "pass_rate": round(sum(r["ok"] for r in g) / len(g), 3),
             "turns": round(statistics.fmean(r["turns"] for r in g), 1),
             "tokens": round(statistics.fmean(r["tokens"] for r in g)),
             "seconds": round(statistics.fmean(r["seconds"] for r in g), 1)}
            for (b, mode, task), g in sorted(groups.items())]


def render(summary: list[dict]) -> str:
    lines = ["backend  mode     task                    runs  pass   turns  tokens   seconds"]
    for r in summary:
        lines.append(f"{r['backend']:8} {r['mode']:8} {r['task']:22} {r['runs']:5} {r['pass_rate']:5.2f} {r['turns']:6} "
                     f"{r['tokens']:7} {r['seconds']:8}")
    return "\n".join(lines)


def main() -> None:
    from harness import config as config_mod
    parser = argparse.ArgumentParser(description="Hosted Claude Code and Codex bake-off arm: builtin vs split")
    parser.add_argument("--backends", default="claude,codex")
    parser.add_argument("--modes", default="builtin,split")
    parser.add_argument("--suite", choices=sorted(SUITES), default="hard")
    parser.add_argument("--tasks", default="", help="comma-separated task ids; default: the whole suite")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / f"hosted-{time.strftime('%Y%m%d-%H%M%S')}")
    args = parser.parse_args()
    wanted = {t for t in args.tasks.split(",") if t}
    tasks = [t for t in SUITES[args.suite] if not wanted or t.id in wanted]
    base = config_mod.load().backends  # read only: the model, image, proxy and login volume to run with
    rows: list[dict] = []
    for backend_name in args.backends.split(","):
        for task in tasks:
            for repeat in range(args.repeats):
                for mode in args.modes.split(","):  # interleaved, so a quota or load drift hits both arms alike
                    run_dir = args.out / backend_name / mode / f"{task.id}-{repeat}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    row = asyncio.run(run_hosted(task, backend_name, mode, run_dir, base[backend_name]))
                    rows.append(row)
                    print(json.dumps(row), flush=True)
    summary = summarize(rows)
    (args.out / "results.json").write_text(json.dumps({"rows": rows, "summary": summary}, indent=2), encoding="utf-8")
    print(render(summary))


if __name__ == "__main__":
    main()
