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
import asyncio
from contextlib import nullcontext
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from types import SimpleNamespace
from .results import classify, error_metrics, outcome

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
    # Some CLIs emit a pretty-printed final envelope instead of JSONL.
    try:
        document = json.loads(stdout)
        if isinstance(document, dict):
            return [document]
    except ValueError:
        pass
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                pass
    return events


def run_container(harness: str, task: Task, run_dir: Path, env: dict[str, str], argv: list[str]) -> tuple[list[dict], dict]:
    """Run one task in the harness container. Returns parsed JSONL events and the process facts."""
    ws = (run_dir / "workspace").resolve()
    name = f"{harness}-{run_dir.parent.name}-{run_dir.name}".replace(".", "-")[:60]
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    cmd = ["docker", "run", "--rm", "--name", name, "--network", NETWORK, "--memory", "4g", "--cpus", "2",
           "--init", "--mount", f"type=bind,source={ws},target={'/fixture' if harness == 'openclaw' else '/workspace'}", "-w", "/workspace", *env_args,
           f"agent-harness-{harness}", *argv]
    started = time.monotonic()
    infrastructure_error = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=task.wall_limit)
        stdout, stderr, code, stop = proc.stdout, proc.stderr, proc.returncode, f"exit {proc.returncode}"
    except subprocess.TimeoutExpired as e:
        docker("rm", "-f", name, check=False)
        stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr, code, stop = "", None, "wall_limit"
    except OSError as error:
        stdout, stderr, code, stop = "", str(error), None, "infrastructure_error"
        infrastructure_error = str(error)
    (run_dir / f"{harness}.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / f"{harness}.stderr.log").write_text(stderr, encoding="utf-8")
    return parse_events(stdout), {"finished": code == 0, "exit_code": code, "stop_reason": stop,
                                  "infrastructure_error": infrastructure_error,
                                  "wall_seconds": round(time.monotonic() - started, 1)}


# --- OpenHands CLI ----------------------------------------------------------

def terminal_errors(events: list[dict], is_error, is_success) -> list[dict]:
    """Errors before a subsequent successful terminal event were recovered."""
    errors = []
    for event in events:
        if is_error(event):
            errors.append(event)
        elif is_success(event):
            errors.clear()
    return errors


def openhands_finished(event: dict) -> bool:
    action = event.get("action") or {}
    return (isinstance(action, dict) and action.get("kind") == "FinishAction") or event.get("tool_name") == "finish"

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


def openhands_result(events: list[dict], facts: dict) -> dict:
    actions = [e["action"] for e in events if e.get("source") == "agent" and isinstance(e.get("action"), dict)]
    native_actions = [e for e in events if e.get("kind") == "ActionEvent" and not isinstance(e.get("action"), dict)]
    errors = terminal_errors(events, lambda e: e.get("error") or e.get("type") == "error", openhands_finished)
    return {"answer": openhands_answer(events), **facts,
            "adapter_error": "missing agent events" if not events and facts.get("stop_reason") != "wall_limit" else None,
            "turns": sum(e.get("source") == "agent" for e in events),
            "tool_calls": sum(a.get("kind") != "FinishAction" for a in actions) + sum(e.get("tool_name") != "finish" for e in native_actions),
            "tool_errors": sum(e.get("source") == "environment" and bool((e.get("observation") or {}).get("is_error")) for e in events),
            "finished": facts["finished"] and not errors, **error_metrics(errors, facts.get("infrastructure_error"))}


def run_openhands(task: Task, run_dir: Path, model: str, port: int) -> dict:
    # -t, not -f: -f frames the prompt as "file context" from another path, which sent the agent looking for the
    # project in the wrong place. The baseline also gets the prompt as a plain user message.
    events, facts = run_container(
        "openhands", task, run_dir,
        {"LLM_MODEL": f"openai/{model}", "LLM_BASE_URL": f"http://{PROXY}:{port}/v1", "LLM_API_KEY": "local"},
        ["openhands", "--headless", "--json", "--override-with-envs", "-t", task.prompt],
    )
    return openhands_result(events, facts)


# --- OpenCode ---------------------------------------------------------------

def opencode_answer(events: list[dict]) -> str:
    """Text parts of the last assistant message that produced any text."""
    by_message: dict[str, list[str]] = {}
    for e in events:
        part = e.get("part") or {}
        if e.get("type") == "text" and part.get("text"):
            by_message.setdefault(part.get("messageID", ""), []).append(part["text"])
    return "\n".join(list(by_message.values())[-1]).strip() if by_message else ""


def opencode_result(events: list[dict], facts: dict) -> dict:
    tools = [e["part"] for e in events if e.get("type") == "tool_use" and isinstance(e.get("part"), dict)]
    steps = [e.get("part") or {} for e in events if e.get("type") == "step_finish"]
    errors = terminal_errors(events, lambda e: e.get("type") == "error",
                             lambda e: e.get("type") == "text" and (e.get("part") or {}).get("text"))
    return {"answer": opencode_answer(events), **facts, "finished": facts["finished"] and not errors,
            "adapter_error": "missing agent events" if not events and facts.get("stop_reason") != "wall_limit" else None,
            "turns": len(steps), "tool_calls": len(tools),
            "tool_errors": sum((t.get("state") or {}).get("status") == "error" for t in tools),
            "prompt_tokens": sum((s.get("tokens") or {}).get("input", 0) for s in steps) if steps else None,
            "completion_tokens": sum((s.get("tokens") or {}).get("output", 0) for s in steps) if steps else None,
            **error_metrics(errors, facts.get("infrastructure_error"))}


def run_opencode(task: Task, run_dir: Path, model: str, port: int) -> dict:
    # --auto approves everything not denied in reference/opencode/opencode.json (web tools and `question` are denied).
    events, facts = run_container(
        "opencode", task, run_dir, {"LLM_BASE_URL": f"http://{PROXY}:{port}/v1"},
        ["opencode", "run", "--format", "json", "--auto", "-m", f"llama/{model}", "--", task.prompt],
    )
    return opencode_result(events, facts)


def hermes_result(events: list[dict], facts: dict) -> dict:
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    tokens = final.get("tokens") or {}
    errors = terminal_errors(events, lambda e: e.get("error") or e.get("type") == "error",
                             lambda e: e.get("type") == "result" and not e.get("exit_code"))
    metrics = next((e for e in events if e.get("type") == "bakeoff_metrics"), {})
    return {**facts, "answer": final.get("text", ""),
            "finished": facts["finished"] and bool(final) and not final.get("exit_code") and not errors,
            "adapter_error": None if final or facts.get("stop_reason") == "wall_limit" else "missing result envelope",
            "turns": metrics.get("turns"), "tool_calls": sum(e.get("type") == "tool_use" for e in events),
            "tool_errors": sum(e.get("type") == "tool_result" and bool(e.get("is_error")) for e in events),
            "prompt_tokens": metrics.get("prompt_tokens", tokens.get("input")),
            "completion_tokens": metrics.get("completion_tokens", tokens.get("output")),
            "compactions": metrics.get("compactions"), "compaction_failures": metrics.get("compaction_failures"),
            **error_metrics(errors, facts.get("infrastructure_error"))}


def openclaw_result(events: list[dict], facts: dict) -> dict:
    final = next((e for e in events if "payloads" in e), {})
    meta = final.get("meta") or {}
    agent = meta.get("agentMeta") or {}
    messages = [e["message"] for e in events if e.get("type") == "message" and isinstance(e.get("message"), dict)]
    assistants = [m for m in messages if m.get("role") == "assistant"]
    tools = [c for m in assistants for c in m.get("content", []) if c.get("type") == "toolCall"]
    usage = agent.get("usage") or {}
    tool_summary = meta.get("toolSummary") or {}
    errors = assistants[-1:] if assistants and assistants[-1].get("stopReason") == "error" else []
    if meta.get("error"):
        errors.append(meta["error"])
    attempts = (meta.get("executionTrace") or {}).get("attempts")
    return {**facts, "answer": "\n".join(p.get("text", "") for p in final.get("payloads", [])),
            "finished": facts["finished"] and bool(final) and not errors and not meta.get("error"),
            "adapter_error": None if final or facts.get("stop_reason") == "wall_limit" else "missing result envelope",
            "turns": agent.get("assistantTurns", len(assistants) if messages else None),
            "tool_calls": tool_summary.get("calls", len(tools) if messages else None),
            "tool_errors": tool_summary.get("failures", sum(m.get("role") == "toolResult" and bool(m.get("isError")) for m in messages) if messages else None),
            "prompt_tokens": usage.get("input"), "completion_tokens": usage.get("output"),
            "context_tokens": (agent.get("lastCallUsage") or {}).get("total"),
            "retries": max(0, len(attempts) - 1) if attempts is not None else None,
            "compactions": sum(e.get("type") == "compaction" for e in events) if messages else None,
            "system_prompt_chars": (meta.get("systemPromptReport") or {}).get("systemPrompt", {}).get("chars"),
            **error_metrics(errors, facts.get("infrastructure_error"))}


def run_hermes(task: Task, run_dir: Path, model: str, port: int) -> dict:
    events, facts = run_container("hermes", task, run_dir,
                                 {"LLM_BASE_URL": f"http://{PROXY}:{port}/v1", "LLM_MODEL": model},
                                 ["python", "/opt/bakeoff/entrypoint.py", "--query", task.prompt,
                                  "--max-turns", str(task.max_turns)])
    return hermes_result(events, facts)


def run_openclaw(task: Task, run_dir: Path, model: str, port: int) -> dict:
    events, facts = run_container("openclaw", task, run_dir,
                                 {"LLM_BASE_URL": f"http://{PROXY}:{port}/v1", "LLM_MODEL": model, "OPENCLAW_DEBUG": "1"},
                                 ["python", "/opt/bakeoff/entrypoint.py", task.prompt])
    return openclaw_result(events, facts)


def run_agent_harness(task: Task, run_dir: Path, model: str, port: int) -> dict:
    from .current_harness import run_current
    return asyncio.run(run_current(task, run_dir, model, f"http://127.0.0.1:{port}"))


HARNESSES: dict[str, Callable[[Task, Path, str, int], dict]] = {
    "agent-harness": run_agent_harness, "openhands": run_openhands, "opencode": run_opencode,
    "openclaw": run_openclaw, "hermes": run_hermes,
}


def validate_context(harness: str, ctx_size: int) -> None:
    if harness == "hermes" and ctx_size < 65536:
        raise ValueError("Hermes requires at least 65536 context tokens; this cell is excluded (no model started)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    parser.add_argument("--models", default="qwen3.6-35b-a3b")
    parser.add_argument("--suite", choices=["core", "hard", "all"], default="hard")
    parser.add_argument("--tasks", default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--fake-script", type=Path, help="scripted fake endpoint; no model load or GPU sampling")
    parser.add_argument("--resume", type=Path, help="existing run dir: keep finished results and run the rest")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    harness, run_task = args.harness, HARNESSES[args.harness]
    config = load_config()
    if not args.fake_script:
        validate_context(harness, config["ctx_size"])
    suite = {"core": TASKS, "hard": HARD_TASKS, "all": TASKS + HARD_TASKS}[args.suite]
    tasks = suite if args.tasks == "all" else [t for t in suite if t.id in args.tasks.split(",")]
    if not args.no_build:
        build_image(ROOT / "sandbox")
        if harness != "agent-harness":
            subprocess.run(["docker", "build", "-t", f"agent-harness-{harness}", str(ROOT / "reference" / harness)],
                           check=True)

    out_dir = args.resume or RUNS / f"{harness}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    fake = None
    if args.fake_script:
        from .fake_network import FakeEndpoint
        fake = FakeEndpoint(args.fake_script, out_dir / "fake-endpoint", reference_module=sys.modules[__name__])
        fake.__enter__()
        args.models = "fake"
    else:
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
            server_context = nullcontext(SimpleNamespace(load_seconds=0)) if fake else LlamaServer(model, server_log, config)
            gpu_context = nullcontext(SimpleNamespace(peak_mib=None)) if fake else GpuSampler()
            memory_context = nullcontext(SimpleNamespace(peak_commit_mib=None, min_avail_mib=None)) if fake else MemorySampler(out_dir / "memory.csv")
            with server_context as server, gpu_context as gpu, memory_context as mem:
                summary["load_seconds"] = round(server.load_seconds or 0, 1)
                print(f"[{label}] loaded in {summary['load_seconds']}s; {len(todo)} runs to go")
                for task, r in todo:
                    run_dir = out_dir / model / f"{task.id}-{r}"
                    sandbox, baseline = prepare(task, run_dir / "workspace")
                    try:
                        port = (fake.port if harness == "agent-harness" else fake.container_port) if fake else config["port"]
                        result = run_task(task, run_dir, model, port)
                        try:
                            passed, note = task.check(Context(run_dir / "workspace", sandbox, result["answer"], baseline))
                        except Exception as e:
                            passed, note = False, f"checker error: {e}"
                    finally:
                        sandbox.stop()
                    record = {"task": task.id, "category": task.category, "repeat": r, "passed": passed,
                              "note": note, "invalid_tool_calls": 0, "median_gen_tps": None,
                              "median_prompt_tps": None, **result}
                    record.update(failure_class=classify(record), outcome=outcome(record))
                    (run_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
                    summary["tasks"].append(record)
                    print(f"[{label}] {'PASS' if passed else 'fail'} {task.id}#{r} turns={result['turns']} "
                          f"{result['wall_seconds']:.0f}s stop={result['stop_reason']} ({note}) "
                          f"min_avail={mem.min_avail_mib}MiB peak_commit={mem.peak_commit_mib}MiB")
                summary["peak_vram_mib"] = gpu.peak_mib
                summary["peak_ram_mib"] = mem.peak_commit_mib
            summaries.append(summary)
            (out_dir / "summaries.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
            write_report(summaries, out_dir)
    finally:
        if fake:
            fake.__exit__(None, None, None)
        else:
            stop_proxy()
    print(f"report: {write_report(summaries, out_dir)}")


if __name__ == "__main__":
    main()
