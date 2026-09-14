"""D1 reference run: the same bake-off tasks and local model, driven by OpenHands CLI instead of `agent.py`.

    python -m bakeoff.openhands_ref --suite hard --models qwen3.6-35b-a3b --repeats 2

Isolation: OpenHands runs in its own container (image `agent-harness-openhands`, built from reference/openhands)
on an internal Docker network. Its only route out is a socat proxy container that forwards to the host's
llama-server, so the agent has no internet or LAN access, like the baseline sandbox. Checkers then run in the
usual network-less sandbox against the same workspace.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .run import RUNS, prepare, write_report
from .sandbox import build_image
from .server import GpuSampler, LlamaServer, load_config
from .tasks import TASKS, Context, Task
from .tasks_hard import HARD_TASKS

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "agent-harness-openhands"
NETWORK = "harness-llm"
PROXY = "harness-llm-proxy"
PROXY_IMAGE = "alpine/socat:1.8.0.3"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=check)


def start_proxy(port: int) -> None:
    if docker("network", "inspect", NETWORK, check=False).returncode != 0:
        docker("network", "create", "--internal", NETWORK)
    docker("rm", "-f", PROXY, check=False)
    docker("run", "-d", "--rm", "--name", PROXY, PROXY_IMAGE,
           f"TCP-LISTEN:{port},fork,reuseaddr", f"TCP:host.docker.internal:{port}")
    docker("network", "connect", NETWORK, PROXY)


def stop_proxy() -> None:
    docker("rm", "-f", PROXY, check=False)


def parse_events(stdout: str) -> tuple[list[dict], list[str]]:
    events, other = [], []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
                continue
            except ValueError:
                pass
        if line:
            other.append(line)
    return events, other


def agent_actions(events: list[dict]) -> list[dict]:
    return [e["action"] for e in events if e.get("source") == "agent" and isinstance(e.get("action"), dict)]


def final_answer(events: list[dict]) -> str:
    """The agent's last words: whichever comes last of a finish action's message or a plain assistant message."""
    for event in reversed(events):
        if event.get("source") != "agent":
            continue
        action = event.get("action")
        if isinstance(action, dict) and action.get("kind") == "FinishAction":
            return str(action.get("message") or "")
        content = (event.get("llm_message") or {}).get("content") or []
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
        if text:
            return text
    return ""


def run_openhands(task: Task, run_dir: Path, model: str, port: int) -> dict:
    ws = (run_dir / "workspace").resolve()
    name = f"openhands-{run_dir.parent.name}-{run_dir.name}".replace(".", "-")[:60]
    # -t, not -f: -f frames the prompt as "file context" from another path, which sent the agent looking for the
    # project in the wrong place. The baseline also gets the prompt as a plain user message.
    cmd = [
        "docker", "run", "--rm", "--name", name, "--network", NETWORK, "--memory", "4g", "--cpus", "2",
        "--mount", f"type=bind,source={ws},target=/workspace", "-w", "/workspace",
        "-e", f"LLM_MODEL=openai/{model}", "-e", f"LLM_BASE_URL=http://{PROXY}:{port}/v1", "-e", "LLM_API_KEY=local",
        IMAGE, "openhands", "--headless", "--json", "--override-with-envs", "-t", task.prompt,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=task.wall_limit)
        stdout, stderr, code, stop = proc.stdout, proc.stderr, proc.returncode, f"exit {proc.returncode}"
    except subprocess.TimeoutExpired as e:
        docker("rm", "-f", name, check=False)
        stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr, code, stop = "", None, "wall_limit"
    wall = time.monotonic() - started
    (run_dir / "openhands.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / "openhands.stderr.log").write_text(stderr, encoding="utf-8")
    events, _ = parse_events(stdout)
    actions = agent_actions(events)
    tool_errors = sum(1 for e in events if e.get("source") == "environment" and (e.get("observation") or {}).get("is_error"))
    return {"answer": final_answer(events), "finished": code == 0, "stop_reason": stop,
            "turns": sum(1 for e in events if e.get("source") == "agent"),
            "tool_calls": sum(1 for a in actions if a.get("kind") != "FinishAction"), "tool_errors": tool_errors,
            "wall_seconds": round(wall, 1)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="qwen3.6-35b-a3b")
    parser.add_argument("--suite", choices=["core", "hard", "all"], default="hard")
    parser.add_argument("--tasks", default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()
    suite = {"core": TASKS, "hard": HARD_TASKS, "all": TASKS + HARD_TASKS}[args.suite]
    tasks = suite if args.tasks == "all" else [t for t in suite if t.id in args.tasks.split(",")]
    if not args.no_build:
        build_image(ROOT / "sandbox")
        subprocess.run(["docker", "build", "-t", IMAGE, str(ROOT / "reference/openhands")], check=True)

    out_dir = RUNS / f"openhands-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True)
    summaries = []
    start_proxy(config["port"])
    try:
        for model in args.models.split(","):
            label = f"openhands/{model}"
            summary: dict = {"model": label, "tasks": []}
            with LlamaServer(model, out_dir / model / "server.log", config) as server, GpuSampler() as gpu:
                summary["load_seconds"] = round(server.load_seconds or 0, 1)
                print(f"[{label}] loaded in {summary['load_seconds']}s")
                for task in tasks:
                    for r in range(args.repeats):
                        run_dir = out_dir / model / f"{task.id}-{r}"
                        sandbox, baseline = prepare(task, run_dir / "workspace")
                        try:
                            result = run_openhands(task, run_dir, model, config["port"])
                            try:
                                passed, note = task.check(Context(run_dir / "workspace", sandbox, result["answer"], baseline))
                            except Exception as e:
                                passed, note = False, f"checker error: {e}"
                        finally:
                            sandbox.stop()
                        record = {"task": task.id, "category": task.category, "repeat": r, "passed": passed,
                                  "note": note, "invalid_tool_calls": 0, "median_gen_tps": None,
                                  "median_prompt_tps": None, **result}
                        (run_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
                        summary["tasks"].append(record)
                        print(f"[{label}] {'PASS' if passed else 'fail'} {task.id}#{r} turns={result['turns']} "
                              f"{result['wall_seconds']:.0f}s stop={result['stop_reason']} ({note})")
                summary["peak_vram_mib"] = gpu.peak_mib
            summaries.append(summary)
            (out_dir / "summaries.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
            write_report(summaries, out_dir)
    finally:
        stop_proxy()
    print(f"report: {write_report(summaries, out_dir)}")


if __name__ == "__main__":
    main()
