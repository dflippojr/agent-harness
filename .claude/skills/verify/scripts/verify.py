"""Prove a change in the running app: launch, doctor, drive, evidence, cleanup (.claude/skills/verify/SKILL.md).

    python .claude/skills/verify/scripts/verify.py run [--feature NAME ...] [--keep] [--evidence DIR]
    python .claude/skills/verify/scripts/verify.py cleanup --evidence DIR

`run` starts a disposable fake-model instance (docs/ui-testing.md, "Start") in a new temp directory as its own
background process, records it in DIR/launch.json before waiting for anything, runs the doctor, drives the smoke
journey (plus the named features) on desktop and Pixel 7 with the tools/ui-tests Playwright package, writes
DIR/summary.md, and always cleans up: the contract's Teardown, then every process this run started. `cleanup` repeats
that cleanup from a launch.json when a run was killed before its own cleanup ran. DIR defaults to
.verify/<UTC timestamp>-<suffix>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor  # noqa: E402 - the sibling script

REPO = doctor.REPO
UI_TESTS = REPO / "tools" / "ui-tests"
PLAYWRIGHT_CLI = UI_TESTS / "node_modules" / "@playwright" / "test" / "cli.js"
DRIVE_CONFIG = UI_TESTS / "verify.config.ts"
LAUNCHER = ("-m", "harness.fake_instance")
FEATURES = ("task",)  # driven on request, after the smoke journey that every run drives
START_SECONDS = 180  # docs/ui-testing.md, "Playwright and CI": the suite allows 180 s
STEP_SECONDS = 600
STEPS = ("launch", "doctor", "drive", "cleanup")  # a run passes only when every one of them ran and passed
# The start summary prints the instance's two tokens. They stay in memory for the doctor and never reach a file.
TOKEN_LINE = re.compile(r"^(\s*(?:Owner|Local) token:\s+)(\S+)")
# The same shapes tools/ui-tests/teardown.mjs redacts from traces: owner tokens and bearer headers.
TOKEN_SHAPES = (re.compile(r"\bho-[A-Za-z0-9_-]{8,}"), re.compile(r"Bearer [A-Za-z0-9_.-]+"))
REDACTED = "[REDACTED]"
SEEDED_FAILURE = "simulated fatal model error (fake model mode)"  # docs/ui-testing.md, the "Failed run" seed
SETUP = ("npm --prefix tools/ui-tests ci", "npm --prefix tools/ui-tests exec -- playwright install chromium")


def run_name() -> str:
    """A UTC timestamp plus a random suffix: two runs started in the same second never share a folder."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def created(pid: int) -> float | None:
    import psutil
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def alive(entry: dict):
    """The recorded process if it still runs with its recorded start time (so a reused pid is left alone)."""
    import psutil
    try:
        proc = psutil.Process(int(entry["pid"]))
        if entry.get("created") is None or abs(proc.create_time() - float(entry["created"])) > 0.01:
            return None
        return None if proc.status() == psutil.STATUS_ZOMBIE else proc
    except (psutil.Error, KeyError, TypeError, ValueError):
        return None


@contextlib.contextmanager
def uninterruptible():
    """Ctrl+C is ignored while cleanup runs (each of its waits has a timeout), so it always finishes."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def instance_env() -> dict:
    """The instance's environment: this one without credentials (it needs none, and GitHub tasks must stay off
    without a token) and without HARNESS_FAKE_MODEL, which the start command sets itself."""
    env = {k: v for k, v in os.environ.items() if not doctor.SECRET_NAME.search(k) and k != "HARNESS_FAKE_MODEL"}
    return dict(env, PYTHONUTF8="1")


def redact_text(text: str, tokens: list[str]) -> str:
    for token in tokens:
        text = text.replace(token, REDACTED)
    for shape in TOKEN_SHAPES:
        text = shape.sub(lambda m: "Bearer " + REDACTED if m.group(0).startswith("Bearer") else REDACTED, text)
    return text


class Containment:
    """Holds every process a run starts and all their descendants, including ones whose parent has already exited
    (sampling the process tree would miss those).

    - Windows: one Job Object with kill-on-close, so they also die if this script is killed.
    - Linux: this script becomes a child subreaper, so an orphan, even one that started its own session, is
      re-parented here and stays its descendant.
    - Everywhere but Windows: every step's environment carries this run's random token, so a process that left
      the tree (macOS has no subreaper, and a later `cleanup` is not the parent) is still found by it.

    Nothing is selected by process group, name or port: an id another process reused is never touched."""

    RUN_VAR = "VERIFY_RUN_ID"  # the variable holding the run token

    def __init__(self, token: str = ""):
        self.job = _windows_job() if os.name == "nt" else None
        self.reaper = self.job is None and _become_subreaper()
        self.token = token or secrets.token_hex(16)

    def popen(self, argv: list[str], **kwargs) -> subprocess.Popen:
        if self.job is None:
            kwargs["env"] = dict(kwargs.get("env") or os.environ, **{self.RUN_VAR: self.token})
            return subprocess.Popen(argv, **kwargs)  # noqa: S603
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x4  # CREATE_SUSPENDED: in the job before it starts anything
        proc = subprocess.Popen(argv, creationflags=flags, **kwargs)  # noqa: S603
        self.job.assign(int(proc._handle))  # the process handle Popen holds
        return proc

    def _members(self) -> list:
        """POSIX: this script's own descendants (with the subreaper, every orphan of the run), plus every process
        that carries this run's token."""
        import psutil
        found = {p.pid: p for p in psutil.Process().children(recursive=True)} if self.reaper else {}
        for proc in psutil.process_iter():
            with contextlib.suppress(psutil.Error, OSError):
                if proc.pid != os.getpid() and proc.environ().get(self.RUN_VAR) == self.token:
                    found[proc.pid] = proc
        live = []
        for proc in found.values():
            with contextlib.suppress(psutil.Error):
                if proc.status() != psutil.STATUS_ZOMBIE:
                    live.append(proc)
        return live

    def kill(self) -> int:
        """Kill everything still contained, each process by pid; return how many there were."""
        if self.job is not None:
            left = self.job.active()
            self.job.terminate()
            return left
        import psutil
        members = self._members()
        for proc in members:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        psutil.wait_procs(members, timeout=15)
        with contextlib.suppress(psutil.Error):  # reap the re-parented orphans so none stays a zombie
            psutil.wait_procs(psutil.Process().children(), timeout=1)
        return len(members)

    def remaining(self) -> int:
        return self.job.active() if self.job is not None else len(self._members())


def _become_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    import ctypes
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def _windows_job():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                   ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    class Job:
        def __init__(self):
            self.handle = kernel32.CreateJobObjectW(None, None)
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.handle or not kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info),
                                                                       ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "could not create a job object")

        def assign(self, process: int) -> None:
            if not kernel32.AssignProcessToJobObject(self.handle, process):
                error = ctypes.get_last_error()
                kernel32.TerminateProcess(process, 1)  # still suspended: it never ran, so nothing escaped
                raise OSError(error, "could not put the process in the run's job object")
            ntdll.NtResumeProcess(process)

        def active(self) -> int:
            info = Accounting()
            kernel32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None)
            return info.ActiveProcesses

        def terminate(self) -> None:
            kernel32.TerminateJobObject(self.handle, 1)
            deadline = time.monotonic() + 15
            while self.active() and time.monotonic() < deadline:
                time.sleep(0.1)

    return Job()


class Run:
    def __init__(self, evidence: Path, python: str = sys.executable, launcher: tuple[str, ...] = LAUNCHER):
        self.evidence = evidence.resolve()
        self.python, self.launcher = python, list(launcher)
        self.record: dict = {"evidence": str(self.evidence), "processes": []}
        self.steps: list[tuple[str, bool, str]] = []
        self.doctor_lines: list[str] = []
        self.tokens: list[str] = []  # the instance's owner and local tokens, in memory only
        self.ready_line = ""
        self.contained = Containment()
        self.record["token"] = self.contained.token  # lets `cleanup` find this run's processes after a crash

    # Record -------------------------------------------------------------------------------------------------------
    def save(self) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        tmp = self.evidence / "launch.json.tmp"
        tmp.write_text(json.dumps(self.record, indent=1), encoding="utf-8")
        os.replace(tmp, self.evidence / "launch.json")

    def track(self, name: str, pid: int) -> None:
        if any(p["pid"] == pid for p in self.record["processes"]):
            return
        self.record["processes"].append({"name": name, "pid": pid, "created": created(pid)})
        self.save()

    def track_tree(self, name: str, pid: int) -> None:
        import psutil
        try:
            for child in psutil.Process(pid).children(recursive=True):
                self.track(f"{name} child", child.pid)
        except psutil.Error:
            pass

    def spawn(self, name: str, argv: list[str], log: Path, env: dict | None = None) -> subprocess.Popen:
        env = dict(env or os.environ, PYTHONUTF8="1")  # the logs are read back as UTF-8 whatever the code page
        with log.open("w", encoding="utf-8") as out:
            proc = self.contained.popen(argv, cwd=REPO, stdout=out, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env)
        self.track(name, proc.pid)
        return proc

    def wait_tracking(self, name: str, proc: subprocess.Popen, seconds: float) -> int:
        """Wait for a step, recording the processes it starts (node, browsers) for the evidence. Containment, not
        this sampling, is what makes cleanup reach all of them."""
        deadline = time.monotonic() + seconds
        while proc.poll() is None:
            self.track_tree(name, proc.pid)
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=30)
                return -1
            time.sleep(0.25)
        return proc.returncode

    # Steps --------------------------------------------------------------------------------------------------------
    def prerequisite_problem(self) -> str | None:
        """What is missing before anything starts: the Stables shell the instance serves, and the Playwright package
        and Node.js the drive step runs."""
        from harness import stables_shell
        if stables_shell.locate() is None:
            return (f"no Stables shell: set {stables_shell.ENV} to the public/ directory of a dflippojr/stables "
                    "checkout at the tag in tools/stables/pin.json, or clone it into this checkout as stables/")
        if shutil.which("node") is None or not PLAYWRIGHT_CLI.is_file():
            return "the Playwright package is not installed; run: " + " && ".join(SETUP)
        return None

    def launch(self) -> bool:
        """docs/ui-testing.md, "Start", in a new temp directory; server logs go to the evidence folder."""
        if problem := self.prerequisite_problem():
            return self.step("launch", False, problem)
        # A new empty directory per run (the start command accepts an empty --dir), so no two runs share one.
        instance = Path(tempfile.mkdtemp(prefix="harness-verify-", dir=tempfile.gettempdir()))
        logs = self.evidence / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.record.update(instance_dir=str(instance), server_log=str(logs / "server.log"))
        self.save()
        with (logs / "server.log").open("w", encoding="utf-8") as err:
            proc = self.contained.popen([self.python, *self.launcher, "--dir", str(instance)], cwd=REPO,
                                        stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                        env=instance_env())
        self.track("instance", proc.pid)
        reader = threading.Thread(target=self.read_launcher, args=(proc,), daemon=True)
        reader.start()
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            self.track_tree("instance", proc.pid)
            if self.ready_line:
                fields = json.loads(self.ready_line[len("READY "):])
                self.record.update(server=fields["server"], web=fields["web"], connect=fields["connect"],
                                   server_port=urlsplit(fields["server"]).port, web_port=urlsplit(fields["web"]).port)
                self.save()
                return self.step("launch", True, f"Server {fields['server']}, Web {fields['web']}")
            if proc.poll() is not None:
                reader.join(timeout=5)
                return self.step("launch", False, f"the instance exited with code {proc.returncode}: "
                                                  f"{self.failure_reason()}")
            time.sleep(0.25)
        return self.step("launch", False, f"no READY line within {START_SECONDS} s; see logs/server.log")

    def read_launcher(self, proc: subprocess.Popen) -> None:
        """Copy the start command's stdout to launcher.txt with its tokens replaced, keeping them in memory."""
        with (self.evidence / "launcher.txt").open("w", encoding="utf-8") as out:
            for raw in proc.stdout or ():
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if match := TOKEN_LINE.match(line):
                    self.tokens.append(match.group(2))
                    line = match.group(1) + REDACTED
                out.write(line + "\n")
                out.flush()
                if line.startswith("READY ") and not self.ready_line:
                    self.ready_line = line

    def failure_reason(self) -> str:
        log = Path(self.record.get("server_log", ""))
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
        failed = [line for line in lines if line.startswith("FAILED ") or line.startswith("usage:")
                  or ": error: " in line]
        return failed[-1].strip()[:300] if failed else "see logs/server.log"

    def doctor(self) -> bool:
        lines: list[str] = []
        code = doctor.run(Path(self.record["instance_dir"]), self.owner_token(), out=lines.append)
        self.doctor_lines = lines
        (self.evidence / "doctor.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        failed = [line for line in lines if line.startswith("FAIL")]
        return self.step("doctor", code == 0, "all checks pass" if code == 0 else "; ".join(failed) or f"exit {code}")

    def owner_token(self) -> str:
        return next((t for t in self.tokens if t.startswith("ho-")), self.tokens[0] if self.tokens else "")

    def drive_argv(self, features: list[str]) -> list[str]:
        # Playwright's CLI under node itself: npm.cmd would pass the --grep "|" through cmd.exe on Windows.
        node = shutil.which("node") or "node"
        tags = "|".join(f"@{f}" for f in ["smoke", *features])
        return [node, str(PLAYWRIGHT_CLI), "test", "--config", str(DRIVE_CONFIG), "--workers=1", "--grep", tags]

    def drive(self, features: list[str]) -> bool:
        env = dict(os.environ, VERIFY_WEB_URL=self.record["web"], VERIFY_EVIDENCE_DIR=str(self.evidence))
        out = self.evidence / "drive.txt"
        code = self.wait_tracking("drive", self.spawn("drive", self.drive_argv(features), out, env), STEP_SECONDS)
        what = ", ".join(["smoke", *features]) + " on desktop and Pixel 7"
        return self.step("drive", code == 0, what + ("" if code == 0 else f": exit {code}; see drive.txt"))

    def hold(self) -> None:
        """--keep: leave the instance up for a look by hand until Ctrl+C; cleanup then runs as usual."""
        print(f"kept: open {self.record['connect']} (signs in as the owner); Ctrl+C stops it and cleans up",
              flush=True)
        entry = next((p for p in self.record["processes"] if p["name"] == "instance"), None)
        try:
            while entry is not None and alive(entry) is not None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    def cleanup(self) -> bool:
        """docs/ui-testing.md, "Teardown", then every recorded process, then the contract's leftover checks."""
        lines = []
        instance = self.record.get("instance_dir")
        if instance:
            lines.append("teardown: " + self.teardown(instance))
        import psutil
        contained = self.contained.kill()
        procs = [p for p in map(alive, self.record["processes"]) if p is not None]
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        psutil.wait_procs(procs, timeout=15)
        lines.append(f"killed {contained} contained and {len(procs)} recorded processes still running")
        if instance and Path(instance).exists():  # Teardown failed, or a process it could not stop held the dir
            lines.append("teardown again: " + self.teardown(instance))
        problems = []
        left = [f"{p['name']} {p['pid']}" for p in self.record["processes"] if alive(p) is not None]
        if left:
            problems.append("still running: " + ", ".join(left))
        if remaining := self.contained.remaining():
            problems.append(f"{remaining} processes this run started still run")
        for key in ("server_port", "web_port"):
            if self.record.get(key) and port_open(self.record[key]):
                problems.append(f"port {self.record[key]} still accepts connections")
        if instance and Path(instance).exists():
            problems.append(f"{instance} still exists")
        lines += problems or ["no recorded process, port or instance directory is left"]
        (self.evidence / "cleanup.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.step("cleanup", not problems, "; ".join(problems) or "nothing left behind")

    def teardown(self, instance: str) -> str:
        try:
            result = subprocess.run([self.python, *self.launcher, "stop", "--dir", instance],  # noqa: S603
                                    cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    env=dict(os.environ, PYTHONUTF8="1"), timeout=180)
        except (subprocess.TimeoutExpired, OSError) as e:
            return f"{type(e).__name__}; the recorded processes are killed instead"
        return f"exit {result.returncode} {(result.stdout + result.stderr).strip()}"

    def step(self, name: str, ok: bool, detail: str) -> bool:
        self.steps.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)
        return ok

    # Evidence -----------------------------------------------------------------------------------------------------
    def head(self) -> str:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,  # noqa: S607
                                 timeout=30).stdout.strip()
            dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True,  # noqa: S607
                                   text=True, timeout=30).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return "unknown"
        return f"`{sha or 'unknown'}`" + (" plus uncommitted changes" if dirty else "")

    def log_excerpts(self) -> list[str]:
        lines = []
        for log in sorted((self.evidence / "logs").glob("*.log")):
            text = log.read_text(encoding="utf-8", errors="replace").splitlines()
            notable = [line for line in text if any(w in line for w in ("FAKE MODEL MODE", "ERROR", "Traceback",
                                                                        "FAILED"))]
            lines += [f"- `{log.name}` ({len(text)} lines): " + (f"{len(notable)} notable lines" if notable else
                                                                 "nothing notable")]
            lines += [f"  - `{line.strip()[:160]}`" for line in notable[:8]]
            if any(SEEDED_FAILURE in line for line in text):
                lines.append(f"  - expected: the seeded Failed run logs a `crashed` traceback ending in "
                             f"`{SEEDED_FAILURE}`")
        return lines or ["- no instance logs"]

    def passed(self) -> bool:
        ran = {name for name, _, _ in self.steps}
        return set(STEPS) <= ran and all(ok for _, ok, _ in self.steps)

    def summary(self, features: list[str]) -> Path:
        ok = self.passed()
        shots = sorted(p.relative_to(self.evidence).as_posix() for p in self.evidence.glob("screenshots/*.png"))
        doc = [f"# Verify: {'PASS' if ok else 'FAIL'}", "",
               f"- Run: `{self.evidence.name}`; head {self.head()}",
               f"- Journeys: {', '.join(['smoke', *features])} on desktop (1440×900) and Pixel 7",
               f"- Instance: Server {self.record.get('server', '-')}, Web {self.record.get('web', '-')} "
               "(fake model, synthetic seed, loopback only)", "",
               "| Step | Result | Detail |", "| --- | --- | --- |"]
        doc += [f"| {name} | {'PASS' if passed else 'FAIL'} | {detail.replace('|', '/')} |"
                for name, passed, detail in self.steps]
        if self.doctor_lines:
            doc += ["", "## Doctor", ""] + [f"- {line}" for line in self.doctor_lines]
        doc += ["", "## Screenshots", ""] + ([f"- `{s}`" for s in shots] or ["- none"])
        doc += ["", "## Log excerpts", ""] + self.log_excerpts()
        doc += ["", "Files: `launch.json`, `launcher.txt`, `doctor.txt`, `drive.txt`, `cleanup.txt`, `logs/`, and "
                "`playwright/` traces when a test failed."]
        path = self.evidence / "summary.md"
        path.write_text("\n".join(doc) + "\n", encoding="utf-8")
        return path

    def redact(self) -> None:
        """No evidence file keeps a token: text files and the text inside trace zips (docs/ui-testing.md,
        "Secrets"). Screenshots never show one."""
        for path in self.evidence.rglob("*"):
            if not path.is_file() or path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webm"):
                continue
            with contextlib.suppress(OSError, zipfile.BadZipFile, UnicodeDecodeError):
                if path.suffix.lower() == ".zip":
                    self.redact_zip(path)
                else:
                    text = path.read_text(encoding="utf-8")
                    if (clean := redact_text(text, self.tokens)) != text:
                        path.write_text(clean, encoding="utf-8")

    def redact_zip(self, path: Path) -> None:
        with zipfile.ZipFile(path) as source:
            entries = [(info, source.read(info)) for info in source.infolist()]
        tmp = path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as target:
            for info, data in entries:
                try:
                    data = redact_text(data.decode("utf-8"), self.tokens).encode("utf-8")
                except UnicodeDecodeError:
                    pass  # binary: a screenshot frame
                target.writestr(info, data)
        os.replace(tmp, path)

    def run(self, features: list[str], keep: bool = False) -> int:
        self.save()
        interrupted: BaseException | None = None
        try:
            if self.launch() and self.doctor():
                self.drive(features)
                if keep:
                    self.hold()
        except BaseException as e:  # noqa: BLE001 - Ctrl+C included: record it, clean up, then re-raise below
            interrupted = e
            self.step("interrupted", False, f"{type(e).__name__} stopped the run before it finished")
        finally:
            with uninterruptible():
                self.cleanup()
                path = self.summary(features)
                self.redact()
                print(f"evidence: {path}", flush=True)
        if interrupted is not None and not isinstance(interrupted, KeyboardInterrupt):
            raise interrupted
        return 0 if self.passed() else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py", description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="launch, doctor, drive, evidence and cleanup")
    r.add_argument("--feature", action="append", choices=FEATURES, default=[],
                   help="also drive this feature's journey (repeatable; the smoke journey always runs)")
    r.add_argument("--keep", action="store_true",
                   help="after the drive, keep the instance up for a look by hand until Ctrl+C, then clean up")
    r.add_argument("--evidence", type=Path, help="evidence folder (default: .verify/<UTC timestamp>-<suffix>/)")
    c = sub.add_parser("cleanup", help="clean up after a run that was killed, from its launch.json")
    c.add_argument("--evidence", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.command == "run":
        evidence = args.evidence or REPO / ".verify" / run_name()
        if evidence.exists() and any(evidence.iterdir()):
            r.error(f"{evidence} is not empty: each run needs its own evidence folder")
        return Run(evidence).run(list(dict.fromkeys(args.feature)), keep=args.keep)
    run = Run(args.evidence)
    try:
        run.record = json.loads((run.evidence / "launch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        c.error(f"no readable launch.json in {run.evidence} ({type(e).__name__})")
    run.contained.token = run.record.get("token") or run.contained.token
    with uninterruptible():
        return 0 if run.cleanup() else 1


if __name__ == "__main__":
    sys.exit(main())
