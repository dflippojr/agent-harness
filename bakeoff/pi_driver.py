"""`python -m bakeoff.canary --driver pi` (#942): the canary's hard tasks driven by the Pi coding agent, by hand only.

The same task files, prompt text, llama-server endpoint and `default_model` as the native driver, and the same hidden
checkers grade the result, so #808 can compare the harnesses. The nightly canary never comes here: harness/manager.py
builds `CanaryRunner` (the native driver) and nothing else.

Pin: `@earendil-works/pi-coding-agent` 1.5.0, from earendil-works/pi tag v1.5.0 (commit f9a26365, MIT), installed in
the `agent-harness-pi` image (reference/pi/Dockerfile). Docs consulted at the pin: packages/coding-agent/docs/
models.md ("Configure a compatible endpoint"), models-configuration.md, llama-cpp.md, cli.md and json.md. Pi needs only
an OpenAI-compatible endpoint: llama-server is a custom provider with `api: openai-completions` in Pi's models.json.
Pi's built-in llama.cpp extension manages llama.cpp's router mode (`/llama`, `/models`); the harness's llama-server
runs one model, so the driver switches extensions off (`--no-extensions`).

Isolation: Pi runs in its own container, built on the bake-off sandbox image (the same Linux tools the checkers use),
with the task workspace as its only host mount and no network at all (`--network none`). Its one way out is a Unix
socket on a Docker volume made for the run: a socat forwarder on the host network listens on that socket and connects
to llama-server's host and port, nothing else; inside Pi's container the entrypoint relays 127.0.0.1:<port> to the
socket. A forwarder on the host network reaches a llama-server bound to the host's loopback (as on the tower:
install/run-server.sh), which a bridge container going through host-gateway cannot.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .run_measures import file_access, tool_counts

PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_VERSION = "1.5.0"  # keep in step with reference/pi/Dockerfile's PI_VERSION
PI_REPO = "earendil-works/pi"
PI_COMMIT = "f9a26365568127461c47a07a37dcb42c140a3ac5"  # tag v1.5.0
PI_DOCS = (f"https://github.com/{PI_REPO}/blob/v{PI_VERSION}/packages/coding-agent/docs/models.md"
           "#configure-a-compatible-endpoint")
IMAGE = "agent-harness-pi"
SANDBOX_IMAGE = "agent-harness-sandbox:py312"
FORWARDER_IMAGE = "alpine/socat:1.8.0.3"  # the forwarder ops/cli-llm and bakeoff/reference.py use
SOCKET_DIR = "/run/llm"  # where the run's volume is mounted in the forwarder and in Pi's container
SOCKET = f"{SOCKET_DIR}/llm.sock"
PROVIDER = "canary"  # the models.json provider id the driver selects
AGENT_DIR = "/tmp/pi-agent"  # Pi's config dir inside the container (PI_CODING_AGENT_DIR); models.json goes here
HOME = "/tmp/pi-home"  # Pi runs as the host user, who has no home in the image
ROOT = Path(__file__).resolve().parent.parent


# the Pi command
def models_json(base_url: str, model) -> dict:
    """Pi's models.json: one provider, llama-server's OpenAI-compatible API, the model the native driver uses.

    `model` is the harness's ModelConfig (name, context_tokens, max_tokens, sampling). Its sampling goes into every
    request (`samplingParams`), as harness.llm merges it into the native driver's."""
    entry: dict = {"id": model.name, "contextWindow": model.context_tokens, "maxTokens": model.max_tokens}
    if model.sampling:
        entry["samplingParams"] = dict(model.sampling)
    return {"providers": {PROVIDER: {"baseUrl": f"{base_url.rstrip('/')}/v1", "api": "openai-completions",
                                     "apiKey": "local", "models": [entry]}}}


def pi_argv(model_name: str, prompt: str, pi_bin: str = "pi") -> list[str]:
    """One non-interactive Pi run that prints its JSONL event stream (docs/json.md) and keeps no session file."""
    return [pi_bin, "--mode", "json", "--no-session", "--offline", "--no-extensions", "--no-skills",
            "--no-prompt-templates", "--no-context-files", "--provider", PROVIDER, "--model", model_name, "--", prompt]


def pi_env(base_url: str, model) -> dict[str, str]:
    """What the image's entrypoint needs: it writes PI_MODELS_JSON to $PI_CODING_AGENT_DIR/models.json, then runs Pi.
    Passing it in the environment keeps the workspace the container's only mount."""
    return {"PI_MODELS_JSON": json.dumps(models_json(base_url, model)), "PI_CODING_AGENT_DIR": AGENT_DIR,
            "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}


def docker_argv(name: str, ws: Path, route: "Route", env: dict[str, str], argv: list[str],
                image: str = IMAGE) -> list[str]:
    """`docker run` for one attempt: no network; the workspace is the only host mount, plus the run's socket volume.
    PI_LLM_PORT tells the entrypoint which loopback port to relay to the socket.

    Pi runs as the host user that prepared the workspace (with no capabilities, container root could not write its
    0644 files), so what it writes stays the host user's and the run's temp dir can be removed."""
    env_args = [a for k, v in {**env, "PI_LLM_PORT": str(route.port), "HOME": HOME}.items() for a in ("-e", f"{k}={v}")]
    user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    return ["docker", "run", "--rm", "--name", name, *user, "--network", "none", "--memory", "4g", "--cpus", "2",
            "--pids-limit", "1024", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--init",
            "--mount", f"type=bind,source={ws.resolve()},target=/workspace",
            "--mount", f"type=volume,source={route.volume},target={SOCKET_DIR}", "-w", "/workspace", *env_args,
            image, *argv]


# the route to llama-server
def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=check)


class Route:
    """The run's one way to llama-server: a socket volume and a host-network forwarder from it to llama-server's host
    and port. `base_url` is the endpoint as Pi sees it (its container's loopback, relayed to the socket)."""

    def __init__(self, model_base_url: str, docker: Callable[..., subprocess.CompletedProcess] | None = None):
        url = urlsplit(model_base_url)
        if url.scheme != "http" or not url.hostname:
            raise ValueError(f"the Pi driver needs an http:// llama-server base_url, not {model_base_url!r}")
        self.port = url.port or 80
        host = "127.0.0.1" if url.hostname == "localhost" else url.hostname
        self.target = f"TCP6:[{host}]:{self.port}" if ":" in host else f"TCP:{host}:{self.port}"
        token = uuid.uuid4().hex[:10]
        self.volume, self.forwarder = f"canary-pi-{token}", f"canary-pi-{token}-llm"
        self.base_url = f"http://127.0.0.1:{self.port}{url.path.rstrip('/')}"  # a reverse proxy's path prefix stays
        self.docker = docker or _docker

    def __enter__(self) -> "Route":
        try:
            self.docker("volume", "create", self.volume)
            self.docker("run", "-d", "--rm", "--name", self.forwarder, "--network", "host", "--read-only",
                        "--memory", "32m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                        "--mount", f"type=volume,source={self.volume},target={SOCKET_DIR}", FORWARDER_IMAGE,
                        f"UNIX-LISTEN:{SOCKET},fork,unlink-early,mode=666", self.target)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.docker("rm", "-f", self.forwarder, check=False)
        self.docker("volume", "rm", "-f", self.volume, check=False)


# launching
@dataclass
class Launched:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    seconds: float


Launch = Callable[[list[str], dict[str, str], Path, float, str], Launched]


def launch_local(argv: list[str], env: dict[str, str], ws: Path, timeout: float, name: str) -> Launched:
    """Run the Pi argv as a host process in the workspace (a fake Pi in the tests; the real one by hand always goes
    through `launch_docker`)."""
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=ws, env={**os.environ, **env}, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return Launched(_text(e.stdout), _text(e.stderr), None, True, round(time.monotonic() - started, 1))
    return Launched(proc.stdout, proc.stderr, proc.returncode, False, round(time.monotonic() - started, 1))


def launch_docker(route: Route, docker: Callable[..., subprocess.CompletedProcess] | None = None) -> Launch:
    docker = docker or _docker

    def launch(argv: list[str], env: dict[str, str], ws: Path, timeout: float, name: str) -> Launched:
        started = time.monotonic()
        try:
            proc = subprocess.run(docker_argv(name, ws, route, env, argv), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            docker("rm", "-f", name, check=False)
            return Launched(_text(e.stdout), _text(e.stderr), None, True, round(time.monotonic() - started, 1))
        return Launched(proc.stdout, proc.stderr, proc.returncode, False, round(time.monotonic() - started, 1))
    return launch


def _text(out) -> str:
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")


# the transcript
def events(stdout: str) -> list[dict]:
    """Pi's JSONL stream: one object per LF-terminated record (docs/json.md); anything else on stdout is skipped."""
    out = []
    for line in stdout.split("\n"):
        line = line.rstrip("\r").strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def parse_transcript(stdout: str, existing: set[str]) -> dict:
    """Every measure #942 asks for, from one Pi run's JSON events.

    Tokens are what llama-server reported per call, as Pi records them in each assistant message's usage: Pi splits
    the server's prompt_tokens into `input` (uncached), `cacheRead` (prompt_tokens_details.cached_tokens) and
    `cacheWrite`, so prompt tokens are their sum, the same count the native driver records. Prompt and completion
    tokens include compaction summaries (`compaction_end.result.usage`), as the native totals do; cached-prompt tokens
    and `turns` count agent turns only, as the native record does."""
    evs = events(stdout)
    assistants = [e["message"] for e in evs if e.get("type") == "message_end"
                  and isinstance(e.get("message"), dict) and e["message"].get("role") == "assistant"]
    # Compaction summaries are model calls too (the native totals count theirs): their usage is on compaction_end.
    summaries = [e["result"].get("usage") for e in evs if e.get("type") == "compaction_end"
                 and isinstance(e.get("result"), dict)]
    turn_usage = [m.get("usage") or {} for m in assistants]
    usage = turn_usage + [u for u in summaries if isinstance(u, dict)]
    calls = [(e.get("toolName", ""), e.get("args") or {}) for e in evs if e.get("type") == "tool_execution_start"]
    if not calls:  # no execution events (an older stream): the calls the model made, from its messages
        calls = [(b.get("name", ""), b.get("arguments") or {}) for m in assistants for b in m.get("content") or []
                 if isinstance(b, dict) and b.get("type") == "toolCall"]
    last = assistants[-1] if assistants else {}
    retry_failed = [e for e in evs if e.get("type") == "auto_retry_end" and e.get("success") is False]
    error = (last.get("errorMessage") or last.get("stopReason")) if last.get("stopReason") in ("error", "aborted") \
        else (retry_failed[-1].get("finalError") or "retries exhausted") if retry_failed else ""
    answer = next((t for t in (_text_of(m) for m in reversed(assistants)) if t.strip()), "")
    return {"answer": answer.strip(), "turns": sum(e.get("type") == "turn_end" for e in evs),
            "prompt_tokens": sum(u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0) for u in usage),
            "completion_tokens": sum(u.get("output", 0) for u in usage),
            # agent turns only: the native loop records no cached prefix for its summaries (run_measures)
            "cached_prompt_tokens": sum(u.get("cacheRead", 0) for u in turn_usage),
            "tool_calls": tool_counts(calls), **file_access(calls, existing),
            "settled": any(e.get("type") == "agent_settled" for e in evs), "stop_reason": last.get("stopReason", ""),
            "error": str(error), "events": len(evs)}


# one attempt
def run_attempt(task, repeat: int, *, base_url: str, model, launch: Launch, prepare: Callable, grade: Callable,
                workdir: Path) -> dict:
    """Prepare the task's workspace, run Pi on its prompt there, grade it with the hidden checker.

    `status` is `done` when Pi settled without an error, `wall_limit` when it ran past the task's wall-clock limit,
    `max_turns` when it took more turns than the task allows the native driver (Pi has no turn cap of its own, so
    the attempt is graded as the native driver's would be when it stops there), else `failed` (including a last reply
    cut off at the token limit)."""
    ws = workdir / f"{task.id}-{repeat}"
    ws.mkdir(parents=True)
    baseline = prepare(task, ws)
    name = f"canary-pi-{task.id}-{repeat}-{uuid.uuid4().hex[:6]}".replace("_", "-")
    run = launch(pi_argv(model.name, task.prompt), pi_env(base_url, model), ws, task.wall_limit, name)
    measures = parse_transcript(run.stdout, set(baseline))
    answer = measures.pop("answer")
    settled, error = measures.pop("settled"), measures.pop("error")
    if run.timed_out:
        status = "wall_limit"
    elif measures["turns"] > task.max_turns:
        status = "max_turns"
    elif run.exit_code == 0 and settled and not error and measures["stop_reason"] != "length":
        status = "done"
    else:  # a last reply cut off at the token limit is no finished answer (the native loop would go on)
        status = "failed"
    ok, note = grade(task, ws, answer, baseline)
    if status == "wall_limit":
        note = f"{note}; stopped at the {task.wall_limit:.0f} s wall-clock limit"
    elif status == "max_turns":
        note = f"{note}; {measures['turns']} turns, over the task's {task.max_turns}"
    elif status == "failed":
        why = error or (f"exited {run.exit_code}" if run.exit_code else "never settled" if not settled
                        else "stopped at the token limit")
        note = f"{note}; pi {why}"
        if not measures["events"]:
            note += f"; no events (stderr: {run.stderr.strip()[-300:]})"
    return {"task": task.id, "repeat": repeat, "driver": "pi", "ok": bool(ok and status == "done"), "note": note,
            "status": status, "seconds": run.seconds, "exit_code": run.exit_code, **measures}


# workspace ownership
def give_back(ws: Path, docker: Callable[..., subprocess.CompletedProcess] | None = None) -> bool:
    """Hand the workspace back to the host user. A task's setup and its checker run in the root sandbox
    (bakeoff/sandbox.py), so they can leave root-owned files, such as merge_conflict's .git, that Pi, running as the
    host user, could not change and the run could not remove. True if anything had to change hands."""
    if not hasattr(os, "getuid"):
        return False
    uid, gid = os.getuid(), os.getgid()
    paths = [ws, *ws.rglob("*")]
    if all(p.lstat().st_uid == uid for p in paths):
        return False
    (docker or _docker)("run", "--rm", "--network", "none", "--mount", f"type=bind,source={ws.resolve()},target=/w",
                        SANDBOX_IMAGE, "chown", "-R", f"{uid}:{gid}", "/w")
    return True


def prepare_for_pi(prepare: Callable, task, ws: Path) -> dict[str, str]:
    baseline = prepare(task, ws)
    give_back(ws)
    return baseline


def grade_for_pi(grade: Callable, task, ws: Path, answer: str, baseline: dict[str, str]) -> tuple[bool, str]:
    try:
        return grade(task, ws, answer, baseline)
    finally:
        give_back(ws)


# by hand
def build_images(root: Path = ROOT) -> None:
    from .sandbox import build_image
    build_image(root / "sandbox")
    subprocess.run(["docker", "build", "-t", IMAGE, str(root / "reference" / "pi")], check=True)


def pin() -> dict:
    return {"package": PI_PACKAGE, "version": PI_VERSION, "repo": PI_REPO, "commit": PI_COMMIT, "docs_url": PI_DOCS,
            "image": IMAGE}


def run_suite(suite: dict, only: list[str] | None, base_url: str, model, launch: Launch,
              prepare: Callable, grade: Callable, workdir: Path) -> list[dict]:
    tasks = [t for t in suite["hard"] if not only or t.id in only]
    return [run_attempt(t, r, base_url=base_url, model=model, launch=launch, prepare=prepare, grade=grade,
                        workdir=workdir)
            for t in tasks for r in range(suite["repeats"])]


def run_standalone(suite: dict, only: list[str] | None, name: str, build: bool = True) -> Path:
    """The Pi half of the comparison: every selected hard task, `suite["repeats"]` times, results in runs/pi-*.json."""
    from harness import config as config_mod
    from .canary import grade_hard_task, prepare_hard_task
    base = config_mod.load()
    model = base.models[base.default_model]
    if build:
        build_images()
    tmp = Path(tempfile.mkdtemp(prefix="canary-pi-"))
    try:
        with Route(model.base_url) as route:
            outcomes = run_suite(suite, only, route.base_url, model, launch_docker(route),
                                 functools.partial(prepare_for_pi, prepare_hard_task),
                                 functools.partial(grade_for_pi, grade_hard_task), tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = ROOT / "runs" / f"pi-{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"status": "complete", "driver": "pi", "pi": pin(), "model": model.name,
                               "outcomes": outcomes}, indent=1), encoding="utf-8")
    passed = sum(o["ok"] for o in outcomes)
    print(f"pi: {passed}/{len(outcomes)} passed · results in {out}")
    return out
