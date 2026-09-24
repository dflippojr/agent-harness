"""Claude Code Remote Control servers for tower projects (Phase 8b).

`claude remote-control` runs a persistent server in a folder that the Claude mobile app and claude.ai/code can pair
with; each session opened from the phone gets its own git worktree (`--spawn worktree`). The harness starts one
server per project on request (web app, app API, or an agent tool that always asks), remembers it, shows the
pairing link, and stops it. Sessions opened this way are ordinary Claude Code sessions under the user's own login:
they don't go through the harness queue, sandbox, or approvals. Claude Code asks for permission in the Claude app
(`--permission-mode default`).

Only the unmodified `claude` CLI is started. Claude Code refuses folders whose workspace trust dialog hasn't been
accepted, and trust isn't inherited from parent folders, so launches check `~/.claude.json` first. The web app can
open an interactive Claude window in the exact folder, but the user still reviews and accepts Claude's trust prompt.

Servers keep running when the daemon restarts; the registry (`<data_dir>/remote-control/state.json`) records the
process id and start time so a restarted daemon can still find and stop them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

from .config import Config, RemoteControlConfig
from .fileops import ToolError

log = logging.getLogger("harness.remote_control")

TOOLS = ("open_claude_remote_control",)
PAIRING_URL = re.compile(r"https://claude\.ai/code\?environment=[A-Za-z0-9_\-]+")
SESSION_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")
CAPACITY = re.compile(r"Capacity:\s*(\d+)/(\d+)")
ANSI = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]")
START_TIMEOUT = 30
STOP_TIMEOUT = 5


class RemoteControlError(ToolError):
    pass


def _norm_path(path: str | Path) -> str:
    return str(Path(path)).replace("\\", "/").rstrip("/").lower()


def trusted_folders(claude_json: Path | None = None) -> set[str]:
    path = claude_json or Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {_norm_path(k) for k, v in (data.get("projects") or {}).items()
            if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}


def parse_log(text: str) -> dict:
    clean = ANSI.sub("", text)
    pairing = PAIRING_URL.findall(clean)
    capacity = CAPACITY.findall(clean)
    return {"pairing_url": pairing[-1] if pairing else "",
            # session links sit inside OSC 8 hyperlink escapes, which ANSI stripping removes, so read the raw text
            "session_urls": list(dict.fromkeys(SESSION_URL.findall(text))),
            "active_sessions": int(capacity[-1][0]) if capacity else 0,
            "connected": "Connected" in clean,
            "error": next((line.strip() for line in clean.splitlines() if line.strip().startswith("Error:")), "")}


class RemoteControl:
    tool_names = TOOLS

    def __init__(self, cfg: Config, rc: RemoteControlConfig, notify=None, popen=subprocess.Popen,
                 claude_json: Path | None = None):
        self.cfg = cfg
        self.rc = rc
        self.notify = notify or (lambda payload: None)
        self.popen = popen
        self.claude_json = claude_json
        self.dir = cfg.data_dir / "remote-control"
        self.state_path = self.dir / "state.json"
        self._lock = asyncio.Lock()
        self._trust_processes: dict[str, subprocess.Popen] = {}

    # registry
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    @staticmethod
    def _alive(entry: dict) -> bool:
        try:
            proc = psutil.Process(entry["pid"])
            # the start time guards against a reused process id
            return proc.is_running() and abs(proc.create_time() - entry["created"]) < 2
        except (psutil.Error, KeyError):
            return False

    # projects
    def folder(self, name: str) -> Path:
        folders = self.rc.folders or {}
        configured = folders.get(name)
        if configured is not None:
            if not configured or "://" in configured:
                raise RemoteControlError(f"{name} has no local folder on the tower to open")
            path = Path(os.path.expandvars(configured)).expanduser()
        else:
            project = self.cfg.projects.get(name)
            if project is None:
                raise RemoteControlError(f"no project or Remote Control folder named {name!r}")
            allowed = self.rc.projects
            if allowed is not None and name not in allowed:
                raise RemoteControlError(
                    f"project {name!r} isn't enabled for Remote Control (remote_control.projects)")
            if project.target != "tower":
                raise RemoteControlError(f"{name} runs on the {project.target}; Remote Control launches only work "
                                         "for tower projects")
            if not project.repo or "://" in project.repo:
                raise RemoteControlError(f"{name} has no local folder on the tower to open")
            path = Path(os.path.expandvars(project.repo)).expanduser()
        if not path.is_dir():
            raise RemoteControlError(f"{path} doesn't exist")
        return path

    def eligible(self) -> list[str]:
        names = []
        project_names = self.rc.projects if self.rc.projects is not None else self.cfg.projects
        candidates = dict.fromkeys([*project_names, *(self.rc.folders or {})])
        for name in candidates:
            try:
                self.folder(name)
            except RemoteControlError:
                continue
            names.append(name)
        return names

    def status(self) -> list[dict]:
        state = self._load()
        trusted = trusted_folders(self.claude_json)
        out, changed = [], False
        for name in self.eligible():
            path = self.folder(name)
            entry = state.get(name)
            running = bool(entry) and self._alive(entry)
            if entry and not running and not entry.get("stopped_at"):
                entry["stopped_at"] = time.time()
                changed = True
            info = {"project": name, "path": str(path), "trusted": _norm_path(path) in trusted,
                    "trust_prompt_open": self._trust_prompt_open(name),
                    "git": (path / ".git").exists(), "running": running}
            if entry:
                info.update({"started_at": entry["started_at"], "started_by": entry.get("started_by", ""),
                             "pid": entry["pid"] if running else None})
                info.update(parse_log(self._log_text(entry)) if running else {})
            out.append(info)
        if changed:
            self._save(state)
        return out

    def _log_text(self, entry: dict) -> str:
        try:
            with open(entry["log"], "rb") as fh:
                fh.seek(max(0, os.path.getsize(entry["log"]) - 200_000))
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _claude(self) -> str:
        claude = self.rc.claude_path or shutil.which("claude")
        if not claude:
            raise RemoteControlError("the claude CLI isn't installed or isn't on the daemon's PATH "
                                     "(set remote_control.claude_path)")
        return claude

    def _command(self) -> list[str]:
        return [self._claude(), "remote-control", "--spawn", self.rc.spawn,
                "--permission-mode", self.rc.permission_mode,
                "--capacity", str(self.rc.capacity)]

    def _trust_prompt_open(self, name: str) -> bool:
        proc = self._trust_processes.get(name)
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        self._trust_processes.pop(name, None)
        return False

    # actions
    async def launch(self, name: str, started_by: str = "") -> dict:
        async with self._lock:
            path = self.folder(name)
            state = self._load()
            entry = state.get(name)
            if entry and self._alive(entry):
                return {**self._view(name), "already_running": True}
            if _norm_path(path) not in trusted_folders(self.claude_json):
                raise RemoteControlError(
                    f"Claude Code hasn't been trusted in {path} yet. Open a terminal there, run `claude` once, "
                    "accept the workspace trust prompt, then try again (trust isn't inherited from parent folders).")
            if self.rc.spawn == "worktree" and not (path / ".git").exists():
                raise RemoteControlError(f"{path} isn't a git repository, which worktree mode needs")
            self.dir.mkdir(parents=True, exist_ok=True)
            log_path = self.dir / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
            cmd = self._command() + ["--name", f"{name} (harness)"]
            flags = 0
            if sys.platform == "win32":  # no console window; survives daemon restarts
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            with open(log_path, "wb") as out:
                proc = self.popen(cmd, cwd=str(path), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                  creationflags=flags, start_new_session=sys.platform != "win32")
            try:
                created = psutil.Process(proc.pid).create_time()
            except psutil.Error:
                created = 0.0
            state[name] = {"pid": proc.pid, "created": created, "started_at": time.time(), "log": str(log_path),
                           "started_by": started_by, "command": cmd}
            self._save(state)

        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            parsed = parse_log(self._log_text(state[name]))
            if parsed["pairing_url"]:
                view = self._view(name)
                self.notify({"title": f"Remote Control ready: {name}",
                             "message": "Tap to open it in the Claude app. Claude asks there before edits and commands.",
                             "priority": 3, "tags": ["iphone"], "click": parsed["pairing_url"]})
                return view
            if proc.poll() is not None:
                break
        text = ANSI.sub("", self._log_text(state[name])).strip()
        await self.stop(name)
        error = parse_log(text)["error"] or (text.splitlines()[-1] if text else "no output")
        raise RemoteControlError(f"Remote Control didn't start in {path}: {error}"[:500])

    def open_trust_prompt(self, name: str) -> dict:
        """Open Claude interactively in a project; Claude itself owns the trust decision."""
        path = self.folder(name)
        if _norm_path(path) in trusted_folders(self.claude_json):
            return {**self._view(name), "already_trusted": True}
        if self._trust_prompt_open(name):
            return {**self._view(name), "already_open": True}
        if sys.platform != "win32":
            raise RemoteControlError("opening the Claude trust window is currently supported only on Windows")
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RemoteControlError("PowerShell isn't installed or isn't on the daemon's PATH")

        # Repository and executable paths stay out of the command string. PowerShell reads them from the environment,
        # and Popen selects the repository with cwd, so spaces and shell metacharacters can't change the command.
        env = os.environ.copy()
        env["HARNESS_CLAUDE_TRUST_PROJECT"] = name
        env["HARNESS_CLAUDE_PATH"] = self._claude()
        script = (
            "$Host.UI.RawUI.WindowTitle = 'Claude workspace trust - ' + $env:HARNESS_CLAUDE_TRUST_PROJECT; "
            "Write-Host ''; Write-Host ('Claude workspace trust for ' + $env:HARNESS_CLAUDE_TRUST_PROJECT); "
            "Write-Host ('Folder: ' + (Get-Location).Path); "
            "Write-Host 'Review the folder and accept Claude Code workspace trust. Exit Claude when finished.'; "
            "Write-Host ''; & $env:HARNESS_CLAUDE_PATH"
        )
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = self.popen([powershell, "-NoLogo", "-NoProfile", "-Command", script], cwd=str(path), env=env,
                          creationflags=flags)
        self._trust_processes[name] = proc
        return {**self._view(name), "trust_prompt_open": True}

    def _view(self, name: str) -> dict:
        return next(s for s in self.status() if s["project"] == name)

    async def stop(self, name: str) -> dict:
        async with self._lock:
            state = self._load()
            entry = state.get(name)
            if not entry:
                raise RemoteControlError(f"no Remote Control server was started for {name}")
            if self._alive(entry):
                try:
                    proc = psutil.Process(entry["pid"])
                    children = proc.children(recursive=True)
                    for child in children:
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    try:
                        proc.kill()
                    except psutil.Error:
                        pass
                    await asyncio.to_thread(psutil.wait_procs, [*children, proc], timeout=STOP_TIMEOUT)
                except psutil.Error:
                    pass
            entry["stopped_at"] = time.time()
            self._save(state)
        return {"project": name, "running": False}

    # agent tool
    def schemas(self) -> list[dict]:
        names = self.eligible()
        return [{"type": "function", "function": {
            "name": "open_claude_remote_control",
            "description": ("Ask the user to open a Claude Code Remote Control server in one of their project folders "
                            "on this PC, so they can continue in the Claude app with a frontier Claude model. Use it "
                            "when a task is beyond what you can do reliably. The user approves every launch, then "
                            "works in the Claude app; you won't see that session."),
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string", "enum": names} if names else {"type": "string"},
                "reason": {"type": "string", "description": "Why this needs Claude, shown to the user."},
            }, "required": ["project", "reason"]},
        }}]

    async def call(self, name: str, args: dict, session: dict | None = None, call_id: str = "") -> str:
        if name != "open_claude_remote_control":
            raise ToolError(f"unknown tool {name}")
        who = f"session {session['id']}" if session else "agent"
        view = await self.launch(str(args.get("project", "")), started_by=who)
        again = " (it was already running)" if view.get("already_running") else ""
        return (f"Remote Control is running for {view['project']}{again}. The user can open it in the Claude app"
                + (f": {view['pairing_url']}" if view.get("pairing_url") else "") + ". Tell them what to ask Claude "
                "to do there; you won't see that session.")

    wants_session = True
