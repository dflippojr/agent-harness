"""Real-environment lane (#731): run the Docker sandbox, the local model and the hosted CLIs against one commit.

    python scripts/real_env_lane.py run [--sha SHA] [--suite sandbox --suite gpu --suite hosted-cli] [--no-report]
    python scripts/real_env_lane.py serve ...      (internal: the disposable daemon a suite drives)

CI never runs these paths (the hosted runner has no sandbox image, no GPU and no CLI login), and self-hosted Actions
runners are off for this public repository. So the lane runs from a scheduled task on the deploy host instead
(ops/real-env/, docs/real-env-lane.md), and reports where the owner and the box's sweep can see it:

- a commit status ``real-env/<suite>`` on the tested SHA. A skipped suite posts ``success`` with a description that
  starts ``skipped:``, never a plain pass;
- on a failure, a ``bug`` issue with the SHA, the suite and a redacted log excerpt, or a comment on the open issue
  for that suite (de-duplicated by a hidden marker in the body);
- ``latest.json`` (and ``runs/<id>/summary.json``) under the lane's base directory, outside the live data dir.

Every run works on a copy of the commit's tree (``git archive``), a fresh data dir and a free loopback port. It never
reads or writes the live instance's data, sessions or containers: the disposable daemon runs with maintenance off (it
would otherwise remove session containers it doesn't know), without the local-model supervision module (it would
otherwise stop or start llama-server) and with its own Docker network names. Reads of the live daemon are limited to
its ``/metrics`` (the GPU guard's state and its running sessions, so the lane yields to real work) and its config
files (the hosted backends and the local model's address). The suites:

- **sandbox**: the ``docker``-marked tests, then one session in the real sandbox image built from the commit:
  ``git_clone`` of a local fixture repo, ``run_shell``, ``verify``, and reads of a host file outside ``/workspace``
  (#370 class) that must never return its contents.
- **gpu**: skipped as ``gpu-held`` unless the live GPU guard is clear and the model is loaded; then the ``gpu``-marked
  tests and one short session on the real llama-server. The lane never loads, parks or restarts the model.
- **hosted-cli**: at most once a week (a ledger enforces it): one minimal print-mode session per configured, logged-in
  backend; with ``--pair``, two concurrent sessions sharing one credential (#390 class) and a login check after.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Protocol

ROOT = Path(__file__).resolve().parent.parent
REPO = "dflippojr/agent-harness"
SUITES = ("sandbox", "gpu", "hosted-cli")
NIGHTLY_SUITES = ("sandbox", "gpu")
PASSED, FAILED, SKIPPED, ERROR = "passed", "failed", "skipped", "error"
STATUS_STATE = {PASSED: "success", SKIPPED: "success", FAILED: "failure", ERROR: "error"}
STATUS_DESCRIPTION_MAX = 140   # GitHub truncates nothing; it refuses a longer description
ISSUE_LABEL = "bug"
EXCERPT_LINES = 60
HOSTED_INTERVAL = 6.5 * 86400  # the weekly cap: a hosted run within this long of the last one is skipped
CLIENT = {"X-Agent-Harness-Client": "cli/2"}
OK_WORD = "real-env-ok"
FIXTURE_REPO = "lane-fixture"
PROJECT = "lane"

DEFAULT_BASE = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "agent-harness-real-env"
LIVE_CONFIG = Path.home() / ".local" / "share" / "agent-harness" / "config"
LIVE_METRICS = "http://127.0.0.1:8100/metrics"


# --- results -------------------------------------------------------------------------------------------------------

@dataclass
class SuiteResult:
    suite: str
    state: str                       # passed | failed | skipped | error
    summary: str = ""                # one line: what ran, or why it didn't
    log: str = ""                    # the evidence; redacted before it leaves the box
    details: dict = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0


def status_for(result: SuiteResult) -> tuple[str, str]:
    """The commit status (state, description) for one suite. GitHub has no skipped state: a skip is `success` whose
    description starts with `skipped:`, so it never reads as a plain pass."""
    description = f"{result.state}: {result.summary}".strip().rstrip(":")
    if len(description) > STATUS_DESCRIPTION_MAX:
        description = description[:STATUS_DESCRIPTION_MAX - 3] + "..."
    return STATUS_STATE[result.state], description


# (pattern, keeps its first group): a labelled value keeps the label and loses the value; a bare token goes whole.
_SECRETS = (
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), False),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"), False),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), False),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), False),  # JWTs, OAuth tokens
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), True),
    (re.compile(r"(?i)((?:token|secret|password|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"),
     True),
)


def redact(text: str) -> str:
    """Mask anything shaped like a credential before a log excerpt goes into an issue or the summary."""
    for pattern, keeps_label in _SECRETS:
        text = pattern.sub((lambda m: m.group(1) + "[redacted]") if keeps_label else "[redacted]", text)
    return text


def excerpt(log: str, lines: int = EXCERPT_LINES) -> str:
    kept = log.strip().splitlines()[-lines:]
    return redact("\n".join(kept))


# --- reporting -----------------------------------------------------------------------------------------------------

class GitHubError(RuntimeError):
    pass


class GitHub(Protocol):
    def post_status(self, sha: str, context: str, state: str, description: str) -> None: ...
    def open_issues(self, label: str) -> list[dict]: ...
    def create_issue(self, title: str, body: str, labels: list[str]) -> int: ...
    def comment(self, number: int, body: str) -> None: ...


class GhCli:
    """GitHub through the `gh` CLI, as whoever the scheduled task's `gh` is logged in as (statuses and issues write)."""

    def __init__(self, repo: str = REPO, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.repo = repo
        self._run = run

    def _gh(self, args: list[str], stdin: str | None = None) -> str:
        proc = self._run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise GitHubError(redact((proc.stderr or proc.stdout or "").strip())[:300] or f"gh exited {proc.returncode}")
        return proc.stdout

    def post_status(self, sha: str, context: str, state: str, description: str) -> None:
        self._gh(["api", f"repos/{self.repo}/statuses/{sha}", "-f", f"state={state}", "-f", f"context={context}",
                  "-f", f"description={description}"])

    def open_issues(self, label: str) -> list[dict]:
        out = self._gh(["issue", "list", "-R", self.repo, "--state", "open", "--label", label, "--limit", "200",
                        "--json", "number,title,body"])
        return json.loads(out or "[]")

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        args = ["issue", "create", "-R", self.repo, "--title", title, "--body-file", "-"]
        for label in labels:
            args += ["--label", label]
        url = self._gh(args, stdin=body).strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def comment(self, number: int, body: str) -> None:
        self._gh(["issue", "comment", str(number), "-R", self.repo, "--body-file", "-"], stdin=body)


def issue_marker(suite: str) -> str:
    return f"<!-- real-env-lane suite={suite} -->"


def failure_text(result: SuiteResult, sha: str, run_id: str) -> str:
    return "\n".join([
        f"The real-environment lane's `{result.suite}` suite **{result.state}** on {sha} (run `{run_id}`).",
        "",
        f"- Suite: `real-env/{result.suite}`",
        f"- Commit: https://github.com/{REPO}/commit/{sha}",
        f"- Summary: {redact(result.summary)}",
        "",
        "<details><summary>Log excerpt (redacted)</summary>",
        "",
        "```text",
        excerpt(result.log) or "(no output)",
        "```",
        "",
        "</details>",
        "",
        "Reproduce on the deploy host: `python scripts/real_env_lane.py run --sha "
        f"{sha} --suite {result.suite} --no-report` (docs/real-env-lane.md).",
    ])


def file_failure(gh: GitHub, result: SuiteResult, sha: str, run_id: str) -> dict:
    """One open issue per suite: comment on it when it exists, otherwise open it."""
    marker = issue_marker(result.suite)
    text = failure_text(result, sha, run_id)
    for issue in gh.open_issues(ISSUE_LABEL):
        if marker in (issue.get("body") or ""):
            gh.comment(issue["number"], text)
            return {"action": "commented", "number": issue["number"]}
    number = gh.create_issue(f"real-env/{result.suite} {result.state} on {sha[:8]}", f"{marker}\n{text}",
                             [ISSUE_LABEL])
    return {"action": "created", "number": number}


def report(results: list[SuiteResult], sha: str, run_id: str, gh: GitHub | None) -> dict:
    """Post each suite's status and file its failure. A GitHub error is recorded, never raised: the summary still
    gets written, and the next run tries again."""
    out: dict[str, dict] = {}
    for result in results:
        entry: dict = {}
        out[result.suite] = entry
        if gh is None:
            entry["posted"] = "no (--no-report)"
            continue
        state, description = status_for(result)
        try:
            gh.post_status(sha, f"real-env/{result.suite}", state, description)
            entry["posted"] = f"posted {state}"
        except (GitHubError, OSError, subprocess.SubprocessError) as e:
            entry["posted"] = f"error: {e}"
        if result.state in (FAILED, ERROR):
            try:
                entry["issue"] = file_failure(gh, result, sha, run_id)
            except (GitHubError, OSError, subprocess.SubprocessError, ValueError) as e:
                entry["issue"] = {"action": "error", "error": str(e)}
    return out


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def summary_doc(sha: str, run_id: str, started_at: float, finished_at: float, results: list[SuiteResult],
                reports: dict) -> dict:
    def iso(t: float) -> str:
        return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": 1, "sha": sha, "run_id": run_id, "started_at": iso(started_at), "finished_at": iso(finished_at),
        "ok": all(r.state in (PASSED, SKIPPED) for r in results),
        "suites": {r.suite: {"state": r.state, "summary": redact(r.summary),
                             "status": status_for(r)[0], "description": status_for(r)[1],
                             "seconds": round(r.finished_at - r.started_at, 1), "details": r.details,
                             "excerpt": excerpt(r.log, 20) if r.state in (FAILED, ERROR) else "",
                             **reports.get(r.suite, {})}
                   for r in results},
    }


def run_lane(sha: str, suites: list[str], runners: dict[str, Callable[[], SuiteResult]], gh: GitHub | None,
             base: Path, run_id: str, clock: Callable[[], float] = time.time) -> dict:
    """Run each suite, report, and write the summary. A suite that raises is an `error`, not a crash."""
    started = clock()
    results = []
    for suite in suites:
        begin = clock()
        try:
            result = runners[suite]()
        except Exception as e:  # noqa: BLE001 - the lane reports a broken suite instead of dying with it
            result = SuiteResult(suite, ERROR, f"the lane could not run the suite: {type(e).__name__}: {e}",
                                 log=f"{type(e).__name__}: {e}")
        result.started_at = result.started_at or begin
        result.finished_at = result.finished_at or clock()
        results.append(result)
    reports = report(results, sha, run_id, gh)
    doc = summary_doc(sha, run_id, started, clock(), results, reports)
    _write_json(base / "runs" / run_id / "summary.json", doc)
    _write_json(base / "latest.json", doc)
    return doc


# --- live instance signals (read-only) -----------------------------------------------------------------------------

def parse_metrics(text: str) -> dict[str, float]:
    """`name{labels}` -> value for each sample line of a Prometheus text exposition."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.rpartition(" ")
        try:
            out[key] = float(value)
        except ValueError:
            continue
    return out


def fetch_metrics(url: str = LIVE_METRICS) -> dict[str, float] | None:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - a fixed loopback URL
            return parse_metrics(resp.read().decode("utf-8", "replace"))
    except OSError:
        return None


def live_busy(metrics: dict[str, float] | None) -> int:
    """Sessions the live daemon is running or about to run. Unknown (no answer) counts as idle: the daemon is down."""
    if not metrics:
        return 0
    return int(sum(metrics.get(f'harness_sessions{{status="{s}"}}', 0) for s in ("running", "queued")))


def gpu_hold_reason(metrics: dict[str, float] | None) -> str:
    """Why the GPU suite must not run now, or "" when the guard is clear and the model is loaded."""
    if metrics is None:
        return "gpu-held: the live daemon's /metrics did not answer, so the GPU guard's state is unknown"
    if "harness_gpu_guard_state{state=\"clear\"}" not in metrics:
        return "gpu-held: the live daemon reports no GPU guard"
    if metrics.get("harness_gpu_guard_state{state=\"clear\"}") != 1:
        state = next((k.split('"')[1] for k, v in metrics.items() if k.startswith("harness_gpu_guard_state") and v),
                     "unknown")
        return f"gpu-held: the GPU guard is {state}"
    if metrics.get("harness_gpu_manual_hold_age_seconds", 0) > 0:
        return "gpu-held: a manual GPU hold is on"
    if metrics.get("harness_model_parked", 0) == 1:
        return "gpu-held: the local model is parked (the lane never loads it)"
    if metrics.get("harness_resource_memory_low", 0) == 1:
        return "gpu-held: the resource guard reports low memory"
    return ""


def wait_for_idle(metrics_url: str, max_wait: float, poll: float = 60,
                  fetch: Callable[[str], dict | None] = fetch_metrics, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> int:
    """Yield to real sessions: wait while the live daemon is busy, up to `max_wait`. Returns the busy count at the
    end (0: go ahead)."""
    deadline = clock() + max_wait
    while True:
        busy = live_busy(fetch(metrics_url))
        if not busy or clock() >= deadline:
            return busy
        sleep(min(poll, max(0.0, deadline - clock())))


# --- pytest --------------------------------------------------------------------------------------------------------

@dataclass
class PytestRun:
    code: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.code in (0, 5) and not self.failed and not self.errors   # 5: nothing selected

    def line(self, label: str) -> str:
        return f"{label}: {self.passed} passed, {self.failed} failed, {self.errors} errors, {self.skipped} skipped"


def junit_counts(path: Path) -> tuple[int, int, int, int]:
    """(passed, failed, errors, skipped) from a pytest JUnit XML report; zeros when there is none."""
    if not path.is_file():
        return 0, 0, 0, 0
    root = ET.parse(path).getroot()  # noqa: S314 - our own pytest's report
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = failed = errors = skipped = 0
    for s in suites:
        total += int(s.get("tests", 0))
        failed += int(s.get("failures", 0))
        errors += int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
    return total - failed - errors - skipped, failed, errors, skipped


# --- the real environment ------------------------------------------------------------------------------------------

def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(args, **kwargs)  # noqa: S603 - fixed argument lists


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Lane:
    """Everything a real suite needs: the commit's tree, its images and the run's scratch space."""
    repo: Path
    sha: str
    run_dir: Path
    base: Path
    python: str = sys.executable
    metrics_url: str = LIVE_METRICS
    live_config: Path = LIVE_CONFIG
    yield_seconds: float = 1800
    workers: int = 2
    broken_fixture: bool = False
    pair: bool = False
    force_hosted: bool = False
    images: dict = field(default_factory=dict)   # kind -> image given on the command line (a PR SHA has none in CI)
    tree: Path = field(init=False)
    sandbox_image: str = ""
    cli_image: str = ""
    built_images: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tree = self.run_dir / "tree"

    @property
    def tag(self) -> str:
        return self.run_dir.name.lower()[-24:].strip("-_.")

    def extract(self) -> None:
        """The commit's files, without a worktree: `git archive` into run_dir/tree."""
        self.tree.mkdir(parents=True, exist_ok=True)
        archive = _run(["git", "-C", str(self.repo), "archive", "--format=tar", self.sha], capture_output=True,
                       text=False)
        if archive.returncode != 0:
            raise RuntimeError(f"git archive {self.sha} failed: {archive.stderr.decode('utf-8', 'replace')[:300]}")
        unpack = subprocess.run(["tar", "-x", "-C", str(self.tree)], input=archive.stdout, capture_output=True)
        if unpack.returncode != 0:
            raise RuntimeError(f"tar failed: {unpack.stderr.decode('utf-8', 'replace')[:300]}")

    def image(self, kind: str, dockerfile: str, build: bool) -> str:
        """The commit's image: CI's `ghcr.io/dflippojr/agent-harness-<kind>:sha-<sha>` if it is here or can be pulled,
        else (when `build`) one built from the tree under a lane tag, removed after the run."""
        if self.images.get(kind):
            return self.images[kind]
        published = f"ghcr.io/dflippojr/agent-harness-{kind}:sha-{self.sha}"
        if _run(["docker", "image", "inspect", published]).returncode == 0:
            return published
        if _run(["docker", "pull", "-q", published], timeout=900).returncode == 0:
            return published
        if not build:
            return ""
        tag = f"real-env-lane-{kind}:{self.sha[:12]}"
        proc = _run(["docker", "build", "-q", "-t", tag, "-f", str(self.tree / "sandbox" / dockerfile),
                     str(self.tree / "sandbox")], timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(f"building the {kind} image failed: {(proc.stderr or proc.stdout)[-500:]}")
        self.built_images.append(tag)
        return tag

    def env(self, **extra: str) -> dict[str, str]:
        tmp = self.base / "tmp"   # outside the run dir, so the pinned gitleaks the tests fetch is kept between runs
        tmp.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "TMPDIR": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("HARNESS_CONFIG_DIR", None)
        env.pop("HARNESS_DATA_DIR", None)
        env.update(extra)
        return env

    def pytest(self, marker: str, env: dict[str, str], timeout: float = 3600) -> PytestRun:
        report = self.run_dir / f"pytest-{marker}.xml"
        args = [self.python, "-m", "pytest", "tests", "-m", marker, "-q", "-rs", "-p", "no:cacheprovider",
                f"--basetemp={self.run_dir / f'pytest-{marker}'}", f"--junitxml={report}"]
        if self.workers > 1:
            args += ["-n", str(min(self.workers, 4)), "--dist", "loadfile"]
        try:
            proc = _run(args, cwd=self.tree, env=env, timeout=timeout)
            code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as e:
            code, output = -1, f"pytest -m {marker} timed out after {timeout:.0f}s\n{e.stdout or ''}"
        return PytestRun(code, *junit_counts(report), output=output)

    @contextlib.contextmanager
    def instance(self, mode: str) -> Iterator["Instance"]:
        """The disposable daemon (`serve`), from the commit's tree, until the block ends."""
        root = self.run_dir / f"instance-{mode}"
        root.mkdir(parents=True, exist_ok=True)
        port = free_port()
        args = [self.python, str(Path(__file__).resolve()), "serve", "--tree", str(self.tree), "--dir", str(root),
                "--mode", mode, "--port", str(port), "--tag", self.tag, "--live-config", str(self.live_config)]
        if mode == "sandbox":
            args += ["--image", self.sandbox_image]
        if self.broken_fixture:
            args.append("--broken-fixture")
        log = (root / "daemon.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(args, cwd=self.tree, env=self.env(), stdout=log, stderr=subprocess.STDOUT,  # noqa: S603
                                start_new_session=True)
        try:
            handle = root / "instance.json"
            deadline = time.monotonic() + 120
            while not handle.is_file():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"the disposable daemon did not start (exit {proc.poll()}):\n"
                                       + (root / "daemon.log").read_text(encoding="utf-8", errors="replace")[-2000:])
                time.sleep(0.5)
            info = json.loads(handle.read_text(encoding="utf-8"))
            yield Instance(info["server"], info["token"], root, info)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)  # its own session: the daemon and nothing else
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
            log.close()
            self._remove_docker_leftovers(root)

    def _remove_docker_leftovers(self, root: Path) -> None:
        """This instance's session containers (named by its own session ids) and its networks; nothing else."""
        sessions = root / "sessions.txt"
        for sid in sessions.read_text(encoding="utf-8").split() if sessions.is_file() else ():
            _run(["docker", "rm", "-f", f"harness-{sid}"], timeout=60)
        for net in ("sbx", "egress", "proxy"):
            _run(["docker", "network", "rm", f"real-env-{self.tag}-{net}"], timeout=60)

    def cleanup(self) -> None:
        for tag in self.built_images:
            _run(["docker", "image", "rm", tag], timeout=120)


@dataclass
class Instance:
    server: str
    token: str
    root: Path
    info: dict

    def client(self):
        import httpx
        return httpx.Client(base_url=self.server, timeout=60, trust_env=False,
                            headers={"Authorization": f"Bearer {self.token}", **CLIENT})

    def record(self, sid: str) -> None:
        with (self.root / "sessions.txt").open("a", encoding="utf-8") as f:
            f.write(sid + "\n")


@dataclass
class SessionRun:
    sid: str
    status: str
    stop_reason: str = ""
    transcript: str = ""
    events: list[dict] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)

    def tool_results(self, name: str | None = None) -> list[dict]:
        return [e for e in self.events if e.get("type") == "tool_result" and (name is None or e.get("name") == name)]


def parse_sse(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                item = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append({**(item.get("data") or {}), **{k: v for k, v in item.items() if k != "data"}}
                           if isinstance(item.get("data"), dict) else item)
    return out


def drive_session(inst: Instance, prompt: str, *, backend: str | None = None, timeout: float = 600,
                  client=None) -> SessionRun:
    """Start a session through the real API and wait for it to end. Every approval it asks for is denied: the lane
    never lets a session do more than its default policy allows."""
    own = client is None
    client = client or inst.client()
    try:
        body: dict = {"prompt": prompt, "project": PROJECT, "title": f"real-env lane: {prompt[:40]}"}
        if backend:
            body["backend"] = backend
        r = client.post("/api/v1/sessions", json=body)
        if r.status_code != 201:
            return SessionRun("", "refused", f"HTTP {r.status_code}: {r.text[:300]}")
        sid = r.json()["id"]
        inst.record(sid)
        denied: list[str] = []
        deadline = time.monotonic() + timeout
        status, reason = "", ""
        while time.monotonic() < deadline:
            s = client.get(f"/api/v1/sessions/{sid}").json()
            status, reason = s.get("status", ""), s.get("stop_reason") or ""
            if status == "waiting_approval":
                for a in client.get(f"/api/v1/sessions/{sid}/approvals").json():
                    denied.append(str(a.get("reason") or a.get("tool") or a.get("id")))
                    client.post(f"/api/v1/sessions/{sid}/approvals/{a['id']}",
                                json={"decision": "deny", "note": "real-env lane: never approves"})
            elif status in ("done", "failed", "cancelled"):
                break
            time.sleep(1)
        else:
            client.post(f"/api/v1/sessions/{sid}/cancel")
            status = f"timeout ({status})"
        events = parse_sse(client.get(f"/api/v1/sessions/{sid}/events", params={"follow": "false"}).text)
        transcript = client.get(f"/api/v1/sessions/{sid}/transcript").text
        return SessionRun(sid, status, reason, transcript, events, denied)
    finally:
        if own:
            client.close()


def describe(run: SessionRun) -> str:
    lines = [f"session {run.sid}: {run.status}" + (f" ({run.stop_reason})" if run.stop_reason else "")]
    for e in run.events:
        if e.get("type") in ("tool_call", "tool_result", "error"):
            text = json.dumps({k: e.get(k) for k in ("type", "name", "args", "output", "error", "message")
                               if e.get(k) is not None}, ensure_ascii=False)
            lines.append(text[:600])
    if run.denied:
        lines.append(f"denied approvals: {run.denied}")
    return "\n".join(lines)


# --- suites --------------------------------------------------------------------------------------------------------

def sandbox_checks(run: SessionRun, secret: str) -> list[str]:
    """What the scripted sandbox session must show. Empty: it passed."""
    problems = []
    if run.status != "done":
        problems.append(f"the session ended {run.status}: {run.stop_reason}")
    outputs = {}
    for e in run.tool_results():
        outputs.setdefault(e.get("name"), []).append(str(e.get("output") or e.get("error") or ""))
    if not any(o.startswith("cloned") for o in outputs.get("git_clone", [])):
        problems.append(f"git_clone of local:{FIXTURE_REPO} did not clone: {outputs.get('git_clone')}")
    if not any("fixture-ok" in o for o in outputs.get("run_shell", [])):
        problems.append(f"run_shell did not run the fixture in the sandbox: {outputs.get('run_shell')}")
    verify = outputs.get("verify", [])
    if not any(o.startswith("verify: all checks passed") for o in verify):
        problems.append(f"verify did not pass: {verify}")
    if secret in run.transcript or any(secret in json.dumps(e) for e in run.events):
        problems.append("#370 class: a read outside /workspace returned the host file's contents")
    return problems


def sandbox_suite(lane: Lane) -> SuiteResult:
    busy = wait_for_idle(lane.metrics_url, lane.yield_seconds)
    if busy:
        return SuiteResult("sandbox", SKIPPED, f"busy: the live daemon kept {busy} session(s) running")
    lane.sandbox_image = lane.image("sandbox", "Dockerfile", build=True)
    lane.cli_image = lane.image("cli", "cli.Dockerfile", build=False)
    env = lane.env(HARNESS_TEST_SANDBOX_IMAGE=lane.sandbox_image, HARNESS_LIVE_DOCKER="1",
                   **({"HARNESS_TEST_CLI_IMAGE": lane.cli_image} if lane.cli_image else {}))
    tests = lane.pytest("docker", env)
    with lane.instance("sandbox") as inst:
        run = drive_session(inst, "real-env lane: sandbox journey", timeout=900)
        secret = (inst.root / "outside" / "secret.txt").read_text(encoding="utf-8").strip()
    problems = ([] if tests.ok else [tests.line("docker-marked tests")]) + sandbox_checks(run, secret)
    details = {"image": lane.sandbox_image, "cli_image": lane.cli_image or "(none: CLI container tests skip)",
               "pytest": asdict(tests) | {"output": ""}, "session": run.sid, "denied_approvals": run.denied}
    log = tests.output[-6000:] + "\n" + describe(run)
    if problems:
        return SuiteResult("sandbox", FAILED, "; ".join(problems)[:500], log, details)
    note = "" if tests.passed else " (no docker-marked test ran)"
    return SuiteResult("sandbox", PASSED, f"{tests.line('docker tests')}; sandbox session passed{note}", log, details)


def gpu_suite(lane: Lane) -> SuiteResult:
    busy = wait_for_idle(lane.metrics_url, lane.yield_seconds)
    if busy:
        return SuiteResult("gpu", SKIPPED, f"busy: the live daemon kept {busy} session(s) running")
    reason = gpu_hold_reason(fetch_metrics(lane.metrics_url))
    if reason:
        return SuiteResult("gpu", SKIPPED, reason)
    model = live_model(lane.live_config)
    env = lane.env(HARNESS_REAL_MODEL_URL=model["base_url"], HARNESS_REAL_MODEL_NAME=model["name"])
    tests = lane.pytest("gpu", env, timeout=1800)
    with lane.instance("gpu") as inst:
        run = drive_session(inst, f"Use the write_file tool to create lane.txt containing exactly {OK_WORD}. "
                                  "Then reply with the single word done.", timeout=900)
    problems = [] if tests.ok else [tests.line("gpu-marked tests")]
    if run.status != "done":
        problems.append(f"the session ended {run.status}: {run.stop_reason}")
    elif not [e for e in run.tool_results("write_file") if e.get("ok")]:
        problems.append("the model never wrote lane.txt with write_file")
    log = tests.output[-6000:] + "\n" + describe(run)
    details = {"model": model["name"], "pytest": asdict(tests) | {"output": ""}, "session": run.sid}
    if problems:
        return SuiteResult("gpu", FAILED, "; ".join(problems)[:500], log, details)
    return SuiteResult("gpu", PASSED, f"{tests.line('gpu tests')}; session on {model['name']} passed", log, details)


def read_ledger(base: Path) -> dict:
    try:
        return json.loads((base / "hosted-ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def hosted_suite(lane: Lane, clock: Callable[[], float] = time.time) -> SuiteResult:
    last = read_ledger(lane.base).get("last_run_at", 0)
    if not lane.force_hosted and clock() - last < HOSTED_INTERVAL:
        when = datetime.fromtimestamp(last, timezone.utc).isoformat(timespec="minutes")
        return SuiteResult("hosted-cli", SKIPPED, f"weekly cap: the last hosted run was at {when}")
    busy = wait_for_idle(lane.metrics_url, lane.yield_seconds)
    if busy:
        return SuiteResult("hosted-cli", SKIPPED, f"busy: the live daemon kept {busy} session(s) running")
    runs: dict[str, SessionRun] = {}
    problems, notes, log = [], [], []
    with lane.instance("hosted") as inst, inst.client() as client:
        listed = client.get("/api/v1/backends").json()
        hosted = [b for b in listed if b.get("name") != "local" and b.get("available")]
        ready = [b["name"] for b in hosted if b.get("logged_in")]
        notes += [f"{b['name']}: not logged in, not run" for b in hosted if not b.get("logged_in")]
        if not ready:
            return SuiteResult("hosted-cli", SKIPPED, "no configured hosted backend is logged in",
                               "\n".join(notes), {"backends": [b.get("name") for b in listed]})
        _write_json(lane.base / "hosted-ledger.json", {"last_run_at": clock(), "sha": lane.sha})
        prompt = f"Reply with exactly {OK_WORD} and nothing else. Do not use any tools."
        pair = "claude" if lane.pair and "claude" in ready else ""
        for name in ready:
            if name == pair:
                continue   # its weekly session is the pair below
            runs[name] = drive_session(inst, prompt, backend=name, timeout=300, client=client)
        if pair:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                both = list(pool.map(lambda _: drive_session(inst, prompt, backend=pair, timeout=300), range(2)))
            runs[f"{pair} (pair 1)"], runs[f"{pair} (pair 2)"] = both
            after = {b["name"]: b for b in client.get("/api/v1/backends").json()}
            if not after.get(pair, {}).get("logged_in"):
                problems.append(f"#390 class: {pair} is logged out after two concurrent sessions")
    for name, run in runs.items():
        log.append(f"[{name}]\n{describe(run)}")
        if run.status != "done" or OK_WORD not in run.transcript:
            problems.append(f"{name}: {run.status} {run.stop_reason}".strip())
    details = {"sessions": {n: r.status for n, r in runs.items()}, "notes": notes, "pair": pair or "off"}
    if problems:
        return SuiteResult("hosted-cli", FAILED, "; ".join(problems)[:500], "\n".join(log + notes), details)
    return SuiteResult("hosted-cli", PASSED, f"{len(runs)} print-mode session(s) passed: {', '.join(runs)}",
                       "\n".join(log + notes), details)


# --- the disposable daemon (`serve`) -------------------------------------------------------------------------------

def live_raw(live_config: Path) -> dict:
    """The live config as the daemon merges it (read-only), through the tested tree's own loader."""
    from harness import config as config_mod
    return config_mod._read_raw_config(Path(live_config))  # noqa: SLF001 - the loader's merge, not a copy of it


def live_model(live_config: Path) -> dict:
    import yaml
    files = [Path(live_config) / "harness.yaml", Path(live_config) / "harness.local.yaml"]
    raw: dict = {}
    for f in files:
        if f.is_file():
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            models = {**(raw.get("models") or {}), **(data.get("models") or {})}
            raw.update(data)
            raw["models"] = models
    name = raw.get("default_model") or ""
    spec = (raw.get("models") or {}).get(name) or {}
    if not spec.get("base_url"):
        raise RuntimeError(f"the live config at {live_config} names no local model with a base_url")
    return {"name": name, **spec}


def write_fixture_repo(repos: Path, broken: bool, outside: Path) -> None:
    """A tiny git repo the session clones with `git_clone local:lane-fixture`. It carries a symlink to the host file
    outside /workspace, which must not let a read reach it (#370 class)."""
    repo = repos / FIXTURE_REPO
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("print('fixture-ok')\n", encoding="utf-8")
    expected = "'never'" if broken else "'fixture-ok'"
    (repo / "test_app.py").write_text(
        "import subprocess, sys, unittest\n\n\nclass App(unittest.TestCase):\n    def test_prints(self):\n"
        "        out = subprocess.run([sys.executable, 'app.py'], capture_output=True, text=True).stdout.strip()\n"
        f"        self.assertEqual(out, {expected})\n", encoding="utf-8")
    (repo / "escape").symlink_to(outside)
    git = ["git", "-C", str(repo), "-c", "user.name=real-env lane", "-c", "user.email=lane@localhost"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "fixture"]):
        subprocess.run([*git, *args], check=True, capture_output=True)  # noqa: S603


def lane_config(root: Path, port: int, mode: str, tag: str, image: str, live_config: Path) -> dict:
    data = root / "data"
    config: dict = {
        "listen": {"host": "127.0.0.1", "port": port},
        "data_dir": data.as_posix(),
        "repos_dir": (root / "repos").as_posix(),
        "modules": {"homelab": False, "memory_library": False, "images": False, "image_edit": False,
                    "gpu_guard": False, "endpoint": False, "web": False, "notifications": False,
                    "backup": False, "remote_control": False, "hub": False, "jobs": False, "runners": False,
                    "mcp_client": False},
        "gpu_guard": {"enabled": False, "pause_flag": (root / "llama-server.paused").as_posix()},
        "canary": {"enabled": False},
        "sandbox": {"image": image or "agent-harness-sandbox:py312", "network": f"real-env-{tag}-sbx",
                    "egress_network": f"real-env-{tag}-egress", "proxy_network": f"real-env-{tag}-proxy",
                    # No proxy of its own: a network command fails closed instead of using the live one.
                    "proxy": f"http://real-env-{tag}-no-proxy:8888", "idle_stop_seconds": 0},
        "budgets": {"approval_timeout_seconds": 3600},
    }
    if mode == "sandbox":
        config["default_model"] = "lane-scripted"
        config["models"] = {"lane-scripted": {"base_url": "http://127.0.0.1:9", "context_tokens": 32768}}
    else:
        model = live_model(live_config)
        config["default_model"] = model["name"]
        config["models"] = {model["name"]: {k: v for k, v in model.items() if k != "name"}}
    if mode == "hosted":
        config["backends"] = live_raw(live_config).get("backends") or {}
    return config


class ScriptedSandboxModel:
    """The sandbox journey's model: turn N of the session answers with step N, whatever the messages say."""

    def __init__(self, secret_path: Path):
        outside = secret_path.as_posix()
        self.steps: list[tuple[str, dict]] = [
            ("git_clone", {"url": f"local:{FIXTURE_REPO}"}),
            ("run_shell", {"command": f"cd {FIXTURE_REPO} && python app.py && git log --oneline | wc -l"}),
            ("verify", {}),
            ("read_file", {"path": "../outside/secret.txt"}),
            ("read_file", {"path": outside}),
            ("read_file", {"path": f"{FIXTURE_REPO}/escape"}),
            ("run_shell", {"command": f"cat {outside} {FIXTURE_REPO}/escape ../outside/secret.txt 2>&1; "
                                      "echo shell-done"}),
        ]

    async def __call__(self, model, messages, tools=None, on_delta=None, max_tokens=None, extra=None, timeout=0,
                       on_progress=None, **_kwargs):
        from harness.llm import Completion
        if tools is None:  # a compaction summary or a title: never the journey
            return Completion(content="real-env lane journey", prompt_tokens=10, completion_tokens=5)
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        if turn >= len(self.steps):
            return Completion(content="done", finish_reason="stop", prompt_tokens=100, completion_tokens=1)
        name, args = self.steps[turn]
        call = {"id": f"lane-{turn}-{name}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        return Completion(tool_calls=[call], finish_reason="tool_calls", prompt_tokens=100, completion_tokens=10)


def serve(args) -> int:
    """The disposable daemon: the tested tree's harness on a loopback port, maintenance off, no GPU modules."""
    sys.path.insert(0, str(Path(args.tree).resolve()))
    import asyncio

    import uvicorn
    import yaml

    from harness import config as config_mod, fake_model
    from harness.api import create_app
    from harness.manager import Manager
    from harness.modules import namespace_packages

    root = Path(args.dir).resolve()
    (root / "repos").mkdir(parents=True, exist_ok=True)
    outside = root / "outside"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text(f"real-env-secret-{uuid.uuid4().hex}\n", encoding="utf-8")
    chat = None
    if args.mode == "sandbox":
        write_fixture_repo(root / "repos", args.broken_fixture, outside)
        chat = ScriptedSandboxModel(secret)
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    cfg_raw = lane_config(root, args.port, args.mode, args.tag, args.image, Path(args.live_config))
    (config_dir / "harness.yaml").write_text(yaml.safe_dump(cfg_raw, sort_keys=False), encoding="utf-8")
    checks = [{"name": "fixture", "command": f"cd {FIXTURE_REPO} && python -m unittest -v", "timeout": 120}]
    projects = {"projects": {PROJECT: {"description": "real-env lane (#731): synthetic workspace",
                                       **({"verify": checks} if args.mode == "sandbox" else {})}}}
    (config_dir / "projects.yaml").write_text(yaml.safe_dump(projects, sort_keys=False), encoding="utf-8")
    cfg = config_mod.load(config_dir, root / "data")
    # As fake model mode does: no local-model supervision, so nothing here can stop or start the live llama-server.
    for name in fake_model.GPU_SWITCHES:
        setattr(cfg.modules, name, False)
        setattr(cfg.installed, name, False)
    cfg.gpu_guard.enabled = False
    packages = cfg.module_packages if cfg.module_packages is not None else namespace_packages()
    cfg.module_packages = tuple(p for p in packages if p not in fake_model.GPU_PACKAGES)
    manager = Manager(cfg, chat=chat) if chat is not None else Manager(cfg)
    real_start = manager.start

    async def start_without_maintenance(maintenance: bool = True) -> None:
        # Maintenance removes session containers its own database doesn't know: on a shared Docker engine those are
        # the live instance's.
        await real_start(maintenance=False)
    manager.start = start_without_maintenance  # type: ignore[method-assign]
    _key, token = manager.db.main.create_api_key("real-env-lane", "admin", "owner")
    server = uvicorn.Server(uvicorn.Config(create_app(manager), host="127.0.0.1", port=args.port, log_level="warning",
                                           proxy_headers=False, timeout_graceful_shutdown=5))

    async def main() -> None:
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                await task
                return
            await asyncio.sleep(0.1)
        handle = root / "instance.json"
        tmp = root / ".instance.json.tmp"
        tmp.write_text(json.dumps({"server": f"http://127.0.0.1:{args.port}", "token": token, "pid": os.getpid()}),
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, handle)
        await task
    try:
        asyncio.run(main())
    finally:
        manager.db.close()
    return 0


# --- command line --------------------------------------------------------------------------------------------------

def resolve_sha(repo: Path, sha: str) -> str:
    if not sha:
        _run(["git", "-C", str(repo), "fetch", "-q", "origin", "main"], timeout=300)
        sha = "origin/main"
    proc = _run(["git", "-C", str(repo), "rev-parse", "--verify", f"{sha}^{{commit}}"])
    if proc.returncode != 0:
        raise SystemExit(f"unknown commit {sha!r}: {proc.stderr.strip()}")
    return proc.stdout.strip()


KEEP_RUNS = 14


def prune_runs(base: Path, keep: int = KEEP_RUNS) -> list[str]:
    """Drop all but the newest `keep` run directories (their ids sort by time). latest.json is never touched."""
    runs = sorted(p for p in (base / "runs").glob("*") if p.is_dir())
    gone = runs[:-keep] if keep > 0 else runs
    for path in gone:
        shutil.rmtree(path, ignore_errors=True)
    return [p.name for p in gone]


def run(args) -> int:
    repo = Path(args.repo).resolve()
    sha = resolve_sha(repo, args.sha)
    base = Path(args.base).expanduser().resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{sha[:8]}"
    run_dir = base / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    lane = Lane(repo, sha, run_dir, base, python=args.python, metrics_url=args.metrics_url,
                live_config=Path(args.live_config).expanduser(), yield_seconds=args.yield_seconds,
                workers=args.workers, broken_fixture=args.broken_fixture, pair=args.pair,
                force_hosted=args.force_hosted, images={"sandbox": args.sandbox_image, "cli": args.cli_image})
    suites = args.suite or list(NIGHTLY_SUITES)
    runners = {"sandbox": lambda: sandbox_suite(lane), "gpu": lambda: gpu_suite(lane),
               "hosted-cli": lambda: hosted_suite(lane)}
    try:
        lane.extract()
        doc = run_lane(sha, suites, runners, None if args.no_report else GhCli(args.github_repo), base, run_id)
    finally:
        lane.cleanup()
        if not args.keep:
            shutil.rmtree(lane.tree, ignore_errors=True)
            for leftover in run_dir.glob("pytest-*"):
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
    prune_runs(base, args.keep_runs)
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0 if doc["ok"] else 1


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the lane against one commit and report")
    r.add_argument("--sha", default="", help="commit to test (default: origin/main after a fetch)")
    r.add_argument("--suite", action="append", choices=SUITES,
                   help=f"repeatable; default: {' '.join(NIGHTLY_SUITES)} (hosted-cli is the weekly run)")
    r.add_argument("--repo", default=str(ROOT), help="the git checkout to take the commit from")
    r.add_argument("--base", default=str(DEFAULT_BASE), help="where runs, latest.json and the ledger go")
    r.add_argument("--python", default=sys.executable, help="Python with requirements.txt installed")
    r.add_argument("--metrics-url", default=LIVE_METRICS, help="the live daemon's /metrics (read-only)")
    r.add_argument("--live-config", default=str(LIVE_CONFIG), help="the live config dir (read-only)")
    r.add_argument("--yield-seconds", type=float, default=1800, help="how long to wait for live sessions to end")
    r.add_argument("--workers", type=int, default=2, help="pytest workers (at most 4)")
    r.add_argument("--github-repo", default=REPO)
    r.add_argument("--no-report", action="store_true", help="no statuses or issues; still writes the summary")
    r.add_argument("--broken-fixture", action="store_true", help="self-test: the fixture's verify check fails")
    r.add_argument("--pair", action="store_true", help="hosted-cli: two concurrent Claude sessions (#390 class)")
    r.add_argument("--force-hosted", action="store_true", help="hosted-cli: ignore the weekly cap")
    r.add_argument("--sandbox-image", default="", help="use this sandbox image instead of the commit's own")
    r.add_argument("--cli-image", default="", help="use this CLI image instead of the commit's own")
    r.add_argument("--keep", action="store_true", help="keep the extracted tree and pytest temp dirs")
    r.add_argument("--keep-runs", type=int, default=KEEP_RUNS, help="run directories to keep (logs, instance data)")
    s = sub.add_parser("serve", help=argparse.SUPPRESS)
    s.add_argument("--tree", required=True)
    s.add_argument("--dir", required=True)
    s.add_argument("--mode", choices=("sandbox", "gpu", "hosted"), required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--tag", required=True)
    s.add_argument("--image", default="")
    s.add_argument("--live-config", default=str(LIVE_CONFIG))
    s.add_argument("--broken-fixture", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "serve":
        return serve(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
