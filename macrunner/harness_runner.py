#!/usr/bin/env python3
"""agent-harness runner for the MacBook (Phase 4).

Connects outbound to the daemon on the tower over the tailnet, long-polls for tool requests for sessions that
target this machine, and runs them: file tools directly on the session workspace, shell commands natively under
sandbox-exec (see sandbox.sb), and git work on the user's source repositories outside the sandbox.

Stdlib-only Agent Harness Runner, installed with Agent Harness for Mac (or legacy SSH deploy) as a launchd agent.
Layout under ~/.agent-harness:
    runner/config.json   server URL, runner name and bearer token (unreadable inside the sandbox)
    runner/app/          this file, sandbox.sb, and the daemon's shared modules under harness/
    workspaces/<id>/     one per session: a clone of the project repo, or an empty scratch directory
    logs/runner.log      stdout/stderr (launchd)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from harness import projects  # noqa: E402
from harness.changes import workspace_changes  # noqa: E402
from harness.compat import CLIENT_PROTOCOLS, MAC_CLIENT_VERSION  # noqa: E402
from harness.fileops import FILE_TOOLS, FileOps, ToolError, dir_size, resolve_path  # noqa: E402
from harness.updater import apply_update, schedule_launchd_handoff  # noqa: E402

VERSION = MAC_CLIENT_VERSION
POLL_TIMEOUT = 60           # the daemon holds a poll for up to 25 s
OUTPUT_CAP = 1_000_000      # characters of command output kept (the daemon trims further for the model)
SESSION_RE = re.compile(r"^[0-9a-f]{10}$")
HTTPS_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(:\d+)?/[^\s'\"`$\\]+")
RID_OPS = frozenset({"shell", "git_clone"})  # the ops that need the request id
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
WORKSPACE = "/workspace"  # the sandbox path the daemon addresses tool paths by
OFFLINE_RULES = """;; No network except localhost (tests that start a server); no DNS either, so nothing leaks through lookups.
(deny network*)
(allow network-bind network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))"""

log = logging.getLogger("runner")


class OpError(Exception):
    def __init__(self, message: str, kind: str = "internal"):
        super().__init__(message)
        self.kind = kind


class Project:
    """The fields of the daemon's Project that harness.projects uses."""

    def __init__(self, repo: str, base_branch: str = "", name: str = ""):
        self.repo, self.base_branch, self.name = repo, base_branch, name or repo


class Executor:
    def __init__(self, workspaces: Path, repo_roots: list, profile: Path | None, home: Path,
                 shell: str = "/bin/bash", min_free_gb: float = 10, server: str = ""):
        self.workspaces = resolve_path(workspaces)
        self.repo_roots = [resolve_path(Path(r).expanduser()) for r in repo_roots]
        self.profile_template = profile.read_text(encoding="utf-8") if profile else None
        if self.profile_template is not None and self.profile_template.count("\n{{NETWORK}}") != 1:
            raise ValueError(f"{profile} needs exactly one {{{{NETWORK}}}} placeholder line")
        self.home = home
        self.shell = shell
        self.min_free_gb = min_free_gb
        self.server = server.rstrip("/")
        self.lock = threading.Lock()
        self.procs: dict = {}      # request id -> Popen
        self.proc_sessions: dict = {}  # request id -> session id

    # helpers
    def workspace(self, sid: str, create: bool = False) -> Path:
        if not SESSION_RE.match(sid or ""):
            raise OpError(f"bad session id {sid!r}")
        ws = self.workspaces / sid
        if create:
            ws.mkdir(parents=True, exist_ok=True)
        return ws

    def tmp_base(self) -> Path:
        """The runner's private base for session temp directories (0700, ours), under the per-user temp area the
        sandbox profile allows writes to. Session directories are named by session id, so a restarted runner
        finds the ones its predecessor left."""
        uid = os.getuid() if hasattr(os, "getuid") else 0
        base = Path(tempfile.gettempdir()) / f"harness-runner-{uid}"
        base.mkdir(mode=0o700, exist_ok=True)
        self.check_private(base)
        return base

    @staticmethod
    def check_private(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise OpError(f"{path} isn't a plain directory")
        if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
            raise OpError(f"{path} isn't owned by this user")

    def tmpdir(self, sid: str) -> Path:
        """The session's own TMPDIR, <private base>/<session id>, created with mode 0700 so other users and sessions
        can't read it or plant files in it. It lasts until the workspace is discarded or cleaned up, across runner
        restarts."""
        if not SESSION_RE.match(sid or ""):
            raise OpError(f"bad session id {sid!r}")
        with self.lock:
            path = self.tmp_base() / sid
            path.mkdir(mode=0o700, exist_ok=True)
            self.check_private(path)
            return path

    def drop_tmpdir(self, sid: str) -> None:
        if not SESSION_RE.match(sid or ""):
            raise OpError(f"bad session id {sid!r}")
        with self.lock:
            remove_tree(self.tmp_base() / sid)

    def project(self, params: dict) -> Project:
        repo = params.get("repo") or ""
        if not repo:
            raise OpError("this session has no repository")
        if not projects.is_url(repo):
            path = resolve_path(Path(repo).expanduser())
            if not any(path == root or path.is_relative_to(root) for root in self.repo_roots):
                raise OpError(f"{repo} isn't under an allowed project directory "
                              f"({', '.join(str(r) for r in self.repo_roots)}); see runner/config.json")
            repo = str(path)
        return Project(repo, params.get("base_branch") or "")

    def disk(self) -> tuple[float, float]:
        self.workspaces.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.workspaces)
        return round(usage.free / 2**30, 1), round(usage.total / 2**30, 1)

    def free_gb(self) -> float:
        return self.disk()[0]

    def info(self) -> dict:
        free_gb, total_gb = self.disk()
        last_update = None
        try:
            last_update = json.loads((self.home / ".agent-harness/runner/last-update.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {"version": VERSION, "protocol": CLIENT_PROTOCOLS["runner"],
                "python": platform.python_version(), "macos": platform.mac_ver()[0],
                "arch": platform.machine(), "free_gb": free_gb, "total_gb": total_gb,
                "workspaces": str(self.workspaces),
                "sandbox": self.profile_template is not None, "repo_roots": [str(r) for r in self.repo_roots],
                "last_update": last_update}

    # dispatch
    def handle(self, rid: str, op: str, params: dict):
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            raise OpError(f"unknown op {op!r} (runner version {VERSION}); redeploy the runner")
        try:
            return fn(rid, params) if op in RID_OPS else fn(params)
        except ToolError as e:
            raise OpError(str(e), "tool")
        except projects.GitError as e:
            raise OpError(str(e), "git")
        except (OSError, UnicodeError) as e:
            raise OpError(f"{type(e).__name__}: {e}", "tool")

    # tools
    def op_file(self, p: dict):
        if p["name"] not in FILE_TOOLS:
            raise OpError(f"not a file tool: {p['name']}")
        ws = self.workspace(p["session"], create=True)
        files = FileOps(ws, int(p.get("context_tokens") or 65536), prefixes=(str(ws), WORKSPACE))
        return getattr(files, p["name"])(**p["args"])

    def op_preview(self, p: dict):
        ws = self.workspace(p["session"], create=True)
        return FileOps(ws, 65536, prefixes=(str(ws), WORKSPACE)).preview_diff(p["name"], p["args"])

    def op_size(self, p: dict):
        ws = self.workspace(p["session"])
        return dir_size(ws) if ws.exists() else 0

    def op_put_file(self, p: dict):
        raw = p.get("content_b64") or ""
        if not isinstance(raw, str) or not raw.strip():
            raise OpError("content_b64 is required", "tool")
        try:
            data = base64.b64decode(raw, validate=True)
        except ValueError:  # binascii.Error is a ValueError
            raise OpError("content_b64 is not valid base64", "tool")
        ws = self.workspace(p["session"], create=True)
        files = FileOps(ws, 65536, prefixes=(str(ws), WORKSPACE))
        return files.write_bytes(p.get("path") or "", data)

    def op_shell(self, rid: str, p: dict):
        ws = self.workspace(p["session"], create=True)
        return self.run_sandboxed(rid, p["session"], ws, p["command"], int(p.get("timeout", 120)),
                                  bool(p.get("network")))

    def op_git_clone(self, rid: str, p: dict):
        url = (p.get("url") or "").strip()
        if not HTTPS_URL_RE.fullmatch(url):
            raise OpError("url must be an https://... URL (local: repositories are only on the tower)", "tool")
        ws = self.workspace(p["session"], create=True)
        files = FileOps(ws, 65536, prefixes=(str(ws), WORKSPACE))
        dest = p.get("dest") or url.rstrip("/").rsplit("/", 1)[-1]
        if dest.endswith(".git"):
            dest = dest[:-4]
        target = files.resolve(dest)
        if target == files.root or target.exists() and any(target.iterdir()):
            raise OpError(f"destination already exists and is not empty: {files.rel(target)}", "tool")
        args = ["git", "clone"] + (["--branch", p["branch"]] if p.get("branch") else []) + ["--", url, files.rel(target)]
        out = self.run_sandboxed(rid, p["session"], ws, " ".join(_quote(a) for a in args), 600, network=True)
        if out["code"] != 0:
            raise OpError(f"git clone failed (exit {out['code']}): {out['output'][-2000:].strip()}", "tool")
        return f"cloned {url} into {files.rel(target)}"

    def op_update_client(self, _p: dict):
        if not self.server:
            raise OpError("runner has no configured server for updates")
        try:
            return apply_update(self.server, self.home / ".agent-harness", restart=False, home=self.home)
        except RuntimeError as exc:
            raise OpError(str(exc)) from exc

    def run_sandboxed(self, rid: str, sid: str, ws: Path, command: str, timeout: int, network: bool) -> dict:
        env = {"PATH": PATH, "HOME": str(self.home), "USER": os.environ.get("USER", ""),
               "LOGNAME": os.environ.get("USER", ""), "SHELL": self.shell, "LANG": "en_US.UTF-8", "TERM": "dumb",
               "TMPDIR": str(self.tmpdir(sid)), "GIT_TERMINAL_PROMPT": "0", "HARNESS_SESSION": sid,
               "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        argv = [self.shell, "-c", command]
        if self.profile_template is not None:
            profile = self.profile_template.replace("\n{{NETWORK}}", "\n" + ("" if network else OFFLINE_RULES))
            argv = ["/usr/bin/sandbox-exec", "-p", profile, "-D", f"WORKSPACE={ws}", "-D", f"HOME={self.home}"] + argv
        proc = subprocess.Popen(argv, cwd=str(ws), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
        with self.lock:
            self.procs[rid], self.proc_sessions[rid] = proc, sid
        chunks, size = [], [0]

        def pump() -> None:
            for block in iter(lambda: proc.stdout.read(65536), b""):
                if size[0] < OUTPUT_CAP * 4:
                    chunks.append(block)
                    size[0] += len(block)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)  # monotonic: time asleep doesn't count
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill(proc)
        finally:
            with self.lock:
                self.procs.pop(rid, None)
                self.proc_sessions.pop(rid, None)
        reader.join(timeout=10)
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if len(output) > OUTPUT_CAP:
            output = output[:OUTPUT_CAP // 2] + "\n... [output cut] ...\n" + output[-OUTPUT_CAP // 2:]
        code = proc.returncode
        if timed_out:
            code = 124
            output += f"\n[command timed out after {timeout}s]"
        elif code is not None and code < 0:
            output += f"\n[killed by signal {-code}]"
        return {"code": code, "output": output}

    @staticmethod
    def kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
            try:
                os.killpg(proc.pid, signal.SIGKILL) if os.name == "posix" else proc.kill()
            except (ProcessLookupError, PermissionError):
                pass

    def op_cancel(self, p: dict):
        with self.lock:
            proc = self.procs.get(p["request_id"])
        if proc:
            self.kill(proc)
        return bool(proc)

    def op_kill_session(self, p: dict):
        with self.lock:
            procs = [self.procs[r] for r, s in self.proc_sessions.items() if s == p["session"]]
        for proc in procs:
            self.kill(proc)
        return len(procs)

    # git projects (outside the sandbox, with the user's git setup)
    def op_prepare(self, p: dict):
        project = self.project(p)
        if self.free_gb() < self.min_free_gb:
            raise OpError(f"only {self.free_gb()} GB free on the MacBook (minimum {self.min_free_gb} GB)")
        ws = self.workspace(p["session"], create=True)
        # --shared: the clone borrows the source's objects (no copy on a full disk). The sandbox can't write to the
        # source, and the session branch is fetched back into it after every run.
        return projects.prepare(project, ws, p["session"], shared=not projects.is_url(project.repo))

    def op_refresh_origin(self, p: dict):
        return projects.refresh_origin(self.workspace(p["session"]))

    def op_save_branch(self, p: dict):
        project, ws, sid = self.project(p), self.workspace(p["session"]), p["session"]
        committed = projects.snapshot(ws, f"Uncommitted work at the end of a run (session {sid})")
        published = projects.publish_local(project, ws, p["branch"])
        return {"auto_commit": committed, "published": published, "head": projects.head(ws)[:12],
                "commits": projects.commits_ahead(ws, p["base_commit"])}

    def op_changes(self, p: dict):
        ws = self.workspace(p["session"])
        if not ws.exists():
            return {"repos": [], "removed": True}
        return workspace_changes(ws, p.get("base_commit") or None)

    def op_merge(self, p: dict):
        project, ws, sid = self.project(p), self.workspace(p["session"]), p["session"]
        result = projects.merge(project, ws, sid, p["branch"], p["base_branch"], p["title"])
        result["head"] = projects.head(ws) if (ws / ".git").exists() else ""
        return result

    def op_push(self, p: dict):
        project, ws, sid = self.project(p), self.workspace(p["session"]), p["session"]
        projects.snapshot(ws, f"Work in progress from session {sid}")
        return {"message": projects.push(project, ws, p["branch"]), "head": projects.head(ws)}

    def op_discard(self, p: dict):
        self.op_kill_session(p)
        project = self.project(p)
        projects.discard(project, p["branch"])
        remove_tree(self.workspace(p["session"]))
        self.drop_tmpdir(p["session"])
        return {"head": ""}

    def op_cleanup_workspace(self, p: dict):
        ws, sid = self.workspace(p["session"]), p["session"]
        if not ws.exists():
            self.drop_tmpdir(sid)
            return {"removed": True}
        if p.get("repo") and p.get("base_commit") and (ws / ".git").exists():
            project = self.project(p)
            if not projects.is_url(project.repo):
                projects.snapshot(ws, f"Uncommitted work before cleanup (session {sid})")
                projects.publish_local(project, ws, p["branch"])
            elif p.get("review") in ("pushed", "discarded"):
                unpushed = projects.git(ws, "log", "--oneline", f"origin/{p['branch']}..HEAD", check=False)
                if unpushed.code != 0 or unpushed.out.strip():
                    return {"removed": False, "reason": "branch has commits that weren't pushed"}
            elif projects.commits_ahead(ws, p["base_commit"]):
                return {"removed": False, "reason": "branch was never pushed"}
        remove_tree(ws)
        self.drop_tmpdir(sid)
        return {"removed": True}


def remove_tree(path: Path) -> None:
    def on_error(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=on_error)


def _quote(arg: str) -> str:
    return shlex.quote(arg)


class Client:
    def __init__(self, server: str, name: str, token: str):
        self.base = f"{server.rstrip('/')}/runners/{name}"
        self.token = token

    def post(self, path: str, body: dict, timeout: float) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}",
            "X-Agent-Harness-Client": f"runner/{CLIENT_PROTOCOLS['runner']}",
            "User-Agent": f"agent-harness-runner/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


class Runner:
    def __init__(self, client: Client, executor: Executor):
        self.client = client
        self.executor = executor
        self.instance = uuid.uuid4().hex
        self.inflight: dict = {}
        self.results: OrderedDict = OrderedDict()  # recent results, to answer a redelivered request
        self.lock = threading.Lock()
        self.caffeinate: subprocess.Popen | None = None
        self.stopping = threading.Event()

    def keep_awake(self, on: bool) -> None:
        running = self.caffeinate is not None and self.caffeinate.poll() is None
        if on and not running:
            # -i: no idle sleep while an agent task runs (closing the lid still sleeps). -w: ends with the runner.
            self.caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            log.info("keeping the Mac awake while a task runs")
        elif not on and running:
            self.caffeinate.terminate()
            self.caffeinate = None
            log.info("task idle; the Mac may sleep again")

    def post_result(self, payload: dict) -> None:
        delay = 1.0
        deadline = time.monotonic() + 24 * 3600
        while not self.stopping.is_set() and time.monotonic() < deadline:
            try:
                self.client.post("/results", payload, timeout=120)
                return
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 413, 422):
                    log.exception("result %s refused: HTTP %s", payload["id"], e.code)
                    return
                log.warning("posting result %s failed: HTTP %s", payload["id"], e.code)
            except (OSError, ValueError) as e:
                log.warning("posting result %s failed: %s", payload["id"], e)
            time.sleep(delay)
            delay = min(30.0, delay * 2)

    def work(self, req: dict) -> None:
        rid, op, params = req["id"], req["op"], req.get("params") or {}
        started = time.monotonic()
        try:
            value = self.executor.handle(rid, op, params)
            payload = {"id": rid, "ok": True, "value": value}
        except OpError as e:
            payload = {"id": rid, "ok": False, "error": str(e), "kind": e.kind}
        except Exception as e:  # noqa: BLE001
            log.exception("request %s (%s) crashed", rid, op)
            payload = {"id": rid, "ok": False, "error": f"runner error: {type(e).__name__}: {e}", "kind": "internal"}
        log.info("%s %s %s in %.1fs", rid, op, "ok" if payload["ok"] else f"failed ({payload.get('kind')})",
                 time.monotonic() - started)
        with self.lock:
            self.results[rid] = payload
            while len(self.results) > 200:
                self.results.popitem(last=False)
        try:
            self.post_result(payload)
        finally:
            with self.lock:
                self.inflight.pop(rid, None)
        if op == "update_client" and payload["ok"]:
            # The result reaches the server before launchd replaces this process.
            value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
            base = self.executor.home / ".agent-harness"
            plist = self.executor.home / "Library" / "LaunchAgents" / "dev.agent-harness.runner.plist"
            previous = base / "runner" / "previous-plist"
            schedule_launchd_handoff(
                plist,
                definition_changed=bool(value.get("plist_changed", True)),
                previous_plist=previous if previous.is_file() else None,
                base=base,
                python=sys.executable,
                app_dir=base / "runner" / "app",
            )

    def poll_once(self, delay: float) -> tuple[dict | None, float, float]:
        """One poll. Returns (response or None, seconds to sleep before the next poll, next backoff delay)."""
        try:
            resp = self.client.post("/poll", {"instance": self.instance, "inflight": self.inflight_ids(),
                                              "info": self.executor.info()}, timeout=POLL_TIMEOUT)
            return resp, 0.0, 1.0
        except urllib.error.HTTPError as e:
            log.warning("poll refused: HTTP %s%s", e.code, " (check the token)" if e.code == 401 else "")
            return None, 60 if e.code in (401, 404) else delay, min(30.0, delay * 2)
        except (OSError, ValueError) as e:  # offline, daemon restarting, just woke up
            log.info("poll failed: %s", e)
            return None, delay, min(30.0, delay * 2)

    def inflight_ids(self) -> list:
        with self.lock:
            return list(self.inflight)

    def dispatch(self, req: dict) -> None:
        rid = req["id"]
        with self.lock:
            if rid in self.inflight:
                return
            cached = self.results.get(rid)
            if cached is None:
                self.inflight[rid] = req
        if cached is not None:
            threading.Thread(target=self.post_result, args=(cached,), daemon=True).start()
        else:
            threading.Thread(target=self.work, args=(req,), name=f"req-{rid}", daemon=True).start()

    def loop(self) -> None:
        log.info("runner %s (instance %s) polling %s", VERSION, self.instance[:8], self.client.base)
        delay = 1.0
        while not self.stopping.is_set():
            resp, sleep_for, delay = self.poll_once(delay)
            if resp is None:
                time.sleep(sleep_for)
                continue
            self.keep_awake(bool(resp.get("keep_awake")))
            for req in resp.get("requests", []):
                self.dispatch(req)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    home = Path.home()
    config_path = Path(os.environ.get("HARNESS_RUNNER_CONFIG") or home / ".agent-harness/runner/config.json")
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    executor = Executor(
        workspaces=Path(cfg.get("workspaces") or home / ".agent-harness/workspaces").expanduser(),
        repo_roots=cfg.get("repo_roots") or [str(home / "Projects")],
        profile=None if cfg.get("sandbox") is False else APP_DIR / "sandbox.sb",
        home=home, min_free_gb=float(cfg.get("min_free_gb", 10)))
    executor.server = cfg["server"]
    runner = Runner(Client(cfg["server"], cfg.get("name", "macbook"), cfg["token"]), executor)

    def stop(signum, frame):
        runner.stopping.set()
        with runner.executor.lock:
            procs = list(runner.executor.procs.values())
        for proc in procs:
            Executor.kill(proc)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    runner.loop()


if __name__ == "__main__":
    main()
