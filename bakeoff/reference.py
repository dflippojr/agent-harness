"""D1 reference runs: the same bake-off tasks and local model, driven by an existing harness instead of `agent.py`.

    python -m bakeoff.reference --harness openhands --suite hard --models qwen3.6-35b-a3b --repeats 2
    python -m bakeoff.reference --harness opencode  --suite hard --models qwen3.6-35b-a3b,gpt-oss-20b --repeats 2

Isolation: the harness runs in its own container (image `agent-harness-<harness>`, built from reference/<harness>)
on an internal Docker network. Its only route out is a socat proxy container that forwards to the host's
llama-server, so the agent has no internet or LAN access, like the baseline sandbox. Checkers then run in the
usual network-less sandbox against the same workspace. Each harness keeps its own prompts, tools and sampling
defaults; that is part of what's being compared.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .run import RUNS, prepare, write_report
from .sandbox import build_image
from .server import GpuSampler, LlamaServer, MemorySampler, load_config
from .tasks import TASKS, Context, Task
from .tasks_hard import HARD_TASKS

ROOT = Path(__file__).resolve().parent.parent
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


def parse_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def run_container(harness: str, task: Task, run_dir: Path, env: dict[str, str], argv: list[str]) -> tuple[list[dict], dict]:
    """Run one task in the harness container. Returns parsed JSONL events and the process facts."""
    ws = (run_dir / "workspace").resolve()
    name = f"{harness}-{run_dir.parent.name}-{run_dir.name}".replace(".", "-")[:60]
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    cmd = ["docker", "run", "--rm", "--name", name, "--network", NETWORK, "--memory", "4g", "--cpus", "2",
           "--mount", f"type=bind,source={ws},target=/workspace", "-w", "/workspace", *env_args,
           f"agent-harness-{harness}", *argv]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=task.wall_limit)
        stdout, stderr, code, stop = proc.stdout, proc.stderr, proc.returncode, f"exit {proc.returncode}"
    except subprocess.TimeoutExpired as e:
        docker("rm", "-f", name, check=False)
        stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr, code, stop = "", None, "wall_limit"
    (run_dir / f"{harness}.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / f"{harness}.stderr.log").write_text(stderr, encoding="utf-8")
    return parse_events(stdout), {"finished": code == 0, "stop_reason": stop,
                                  "wall_seconds": round(time.monotonic() - started, 1)}


# --- OpenHands CLI ----------------------------------------------------------

def openhands_answer(events: list[dict]) -> str:
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
    # -t, not -f: -f frames the prompt as "file context" from another path, which sent the agent looking for the
    # project in the wrong place. The baseline also gets the prompt as a plain user message.
    events, facts = run_container(
        "openhands", task, run_dir,
        {"LLM_MODEL": f"openai/{model}", "LLM_BASE_URL": f"http://{PROXY}:{port}/v1", "LLM_API_KEY": "local"},
        ["openhands", "--headless", "--json", "--override-with-envs", "-t", task.prompt],
    )
    actions = [e["action"] for e in events if e.get("source") == "agent" and isinstance(e.get("action"), dict)]
    return {
        "answer": openhands_answer(events), **facts,
        "turns": sum(1 for e in events if e.get("source") == "agent"),
        "tool_calls": sum(1 for a in actions if a.get("kind") != "FinishAction"),
        "tool_errors": sum(1 for e in events if e.get("source") == "environment"
                           and (e.get("observation") or {}).get("is_error")),
    }


# --- OpenCode ---------------------------------------------------------------

def opencode_answer(events: list[dict]) -> str:
    """Text parts of the last assistant message that produced any text."""
    by_message: dict[str, list[str]] = {}
    for e in events:
        part = e.get("part") or {}
        if e.get("type") == "text" and part.get("text"):
            by_message.setdefault(part.get("messageID", ""), []).append(part["text"])
    return "\n".join(list(by_message.values())[-1]).strip() if by_message else ""


def run_opencode(task: Task, run_dir: Path, model: str, port: int) -> dict:
    # --auto approves everything not denied in reference/opencode/opencode.json (web tools and `question` are denied).
    events, facts = run_container(
        "opencode", task, run_dir, {"LLM_BASE_URL": f"http://{PROXY}:{port}/v1"},
        ["opencode", "run", "--format", "json", "--auto", "-m", f"llama/{model}", "--", task.prompt],
    )
    tools = [e["part"] for e in events if e.get("type") == "tool_use" and isinstance(e.get("part"), dict)]
    errors = [e for e in events if e.get("type") == "error"]
    if errors:
        facts["stop_reason"] += f"; error: {json.dumps(errors[-1])[:200]}"
    return {
        "answer": opencode_answer(events), **facts,
        "turns": sum(1 for e in events if e.get("type") == "step_finish"),
        "tool_calls": len(tools),
        "tool_errors": sum(1 for t in tools if (t.get("state") or {}).get("status") == "error"),
    }


HARNESSES: dict[str, Callable[[Task, Path, str, int], dict]] = {"openhands": run_openhands, "opencode": run_opencode}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    parser.add_argument("--models", default="qwen3.6-35b-a3b")
    parser.add_argument("--suite", choices=["core", "hard", "all"], default="hard")
    parser.add_argument("--tasks", default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--resume", type=Path, help="existing run dir: keep finished results and run the rest")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    harness, run_task = args.harness, HARNESSES[args.harness]
    config = load_config()
    suite = {"core": TASKS, "hard": HARD_TASKS, "all": TASKS + HARD_TASKS}[args.suite]
    tasks = suite if args.tasks == "all" else [t for t in suite if t.id in args.tasks.split(",")]
    if not args.no_build:
        build_image(ROOT / "sandbox")
        subprocess.run(["docker", "build", "-t", f"agent-harness-{harness}", str(ROOT / "reference" / harness)],
                       check=True)

    out_dir = args.resume or RUNS / f"{harness}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    start_proxy(config["port"])
    try:
        for model in args.models.split(","):
            label = f"{harness}/{model}"
            summary: dict = {"model": label, "tasks": [], "load_seconds": None, "peak_vram_mib": None}
            todo = []
            for task in tasks:
                for r in range(args.repeats):
                    done = out_dir / model / f"{task.id}-{r}" / "result.json"
                    if done.is_file():
                        summary["tasks"].append(json.loads(done.read_text(encoding="utf-8")))
                    else:
                        todo.append((task, r))
            if not todo:
                summaries.append(summary)
                continue
            server_log = out_dir / model / f"server-{datetime.now().strftime('%H%M%S')}.log"
            with LlamaServer(model, server_log, config) as server, GpuSampler() as gpu, \
                    MemorySampler(out_dir / "memory.csv") as mem:
                summary["load_seconds"] = round(server.load_seconds or 0, 1)
                print(f"[{label}] loaded in {summary['load_seconds']}s; {len(todo)} runs to go")
                for task, r in todo:
                    run_dir = out_dir / model / f"{task.id}-{r}"
                    sandbox, baseline = prepare(task, run_dir / "workspace")
                    try:
                        result = run_task(task, run_dir, model, config["port"])
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
                          f"{result['wall_seconds']:.0f}s stop={result['stop_reason']} ({note}) "
                          f"min_avail={mem.min_avail_mib}MiB peak_commit={mem.peak_commit_mib}MiB")
                summary["peak_vram_mib"] = gpu.peak_mib
            summaries.append(summary)
            (out_dir / "summaries.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
            write_report(summaries, out_dir)
    finally:
        stop_proxy()
    print(f"report: {write_report(summaries, out_dir)}")


if __name__ == "__main__":
    main()
