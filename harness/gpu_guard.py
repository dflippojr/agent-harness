"""GPU contention guard.

The tower's 16 GB GPU is shared by the agent model (Qwen holds ~14.7 GB), games streamed with Sunshine or played
locally, and Plex hardware transcodes. When a game or a transcode shows up, the guard:

1. pauses the queue: the model turn in progress finishes (up to `drain_timeout_seconds`), no new turn starts;
2. stops llama-server and leaves a pause flag, so its supervisor (ops/llama-server/run-qwen.ps1) doesn't restart it;
3. once the GPU has been clear for `resume_after_seconds`, removes the flag (the supervisor starts the server and it
   loads the model) and reopens the queue when the server answers /health.

Windows doesn't report VRAM per process, so the triggers are what's running, not memory numbers: an executable
under a game library folder, a Steam Big Picture window (Sunshine's Big Picture app), or a Plex transcode with
hardware encoding or decoding. Desktop streaming with nothing else running doesn't pause anything.

Sessions hear about it through `gpu_paused` / `gpu_resumed` events (see Runner.note_gpu_pause), which also notify
the phone. From the app the user can pause by hand, or resume anyway while a trigger is still present; that
override lasts until the set of triggers changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx

from .config import GpuGuardConfig, ModelConfig
from .sandbox import run_cmd

log = logging.getLogger("harness.gpu_guard")

CLEAR, PAUSING, PAUSED, RESUMING = "clear", "pausing", "paused", "resuming"


def _manual_reason() -> dict:
    return {"key": "manual", "kind": "manual", "detail": "paused from the app"}


HEALTH_TIMEOUT_SECONDS = 300  # reopen the queue anyway after this; model calls then report their own errors
MANUAL_HOLD_FILE = "gpu-guard-hold.json"  # survives a daemon restart; the pause_flag file alone does not


# ---------- detection ----------
def process_paths() -> list[str]:
    """Full executable paths of the processes this user can query (Windows only)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    size = 4096
    while True:
        pids = (wintypes.DWORD * size)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        if needed.value < ctypes.sizeof(pids):
            break
        size *= 2
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    buf = ctypes.create_unicode_buffer(32768)
    out = []
    for pid in pids[:count]:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            length = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
                out.append(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    return out


def window_titles() -> list[str]:
    """Titles of visible top-level windows in this desktop session (Windows only)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    titles: list[str] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(proto(visit), 0)
    return titles


def find_games(cfg: GpuGuardConfig, paths: list[str], titles: list[str]) -> list[dict]:
    signals: dict[str, dict] = {}
    dirs = [d.lower() for d in cfg.game_dirs]
    ignore = [d.lower() for d in cfg.ignore_paths]
    names = {n.lower() for n in cfg.game_processes}
    for path in paths:
        low = path.lower()
        exe = os.path.basename(path)
        if any(i in low for i in ignore):
            continue
        if exe.lower() in names or any(d in low for d in dirs):
            signals[f"game:{exe.lower()}"] = {"key": f"game:{exe.lower()}", "kind": "game", "detail": exe}
    wanted = {t.lower() for t in cfg.window_titles}
    for title in titles:
        if title.lower() in wanted:
            signals[f"window:{title.lower()}"] = {"key": f"window:{title.lower()}", "kind": "game", "detail": title}
    return list(signals.values())


def plex_token() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as key:
            return str(winreg.QueryValueEx(key, "PlexOnlineToken")[0])
    except OSError:
        return ""


def hw_transcodes(body: dict) -> list[dict]:
    """Hardware video transcodes in a Plex /transcode/sessions response."""
    out = []
    for ts in (body.get("MediaContainer") or {}).get("TranscodeSession") or []:
        if ts.get("videoDecision") != "transcode":
            continue
        hw = ts.get("transcodeHwEncoding") or ts.get("transcodeHwDecoding")
        if not (hw or ts.get("transcodeHwRequested")):
            continue
        key = str(ts.get("key", "")).rsplit("/", 1)[-1] or "session"
        detail = "Plex transcode" + (f" ({ts.get('transcodeHwEncoding') or ts.get('transcodeHwDecoding')})" if hw else "")
        out.append({"key": f"plex:{key}", "kind": "plex", "detail": detail})
    return out


class Detector:
    def __init__(self, cfg: GpuGuardConfig):
        self.cfg = cfg
        self._token = ""
        self.plex_error = ""

    async def __call__(self) -> list[dict]:
        signals = await asyncio.to_thread(lambda: find_games(self.cfg, process_paths(), window_titles()))
        if self.cfg.plex:
            signals += await self._plex()
        return signals

    async def _plex(self) -> list[dict]:
        if not self._token:
            self._token = await asyncio.to_thread(plex_token)
        if not self._token:
            self.plex_error = "no Plex token in the registry"
            return []
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.cfg.plex_url}/transcode/sessions",
                                        headers={"X-Plex-Token": self._token, "Accept": "application/json"})
            if resp.status_code == 401:
                self._token = ""  # re-read next time (signed in again)
            resp.raise_for_status()
            self.plex_error = ""
            return hw_transcodes(resp.json())
        except (httpx.HTTPError, ValueError) as e:
            self.plex_error = f"{type(e).__name__}: {e}"[:200]  # Plex down: it isn't transcoding either
            return []


# ---------- model server control ----------
class ServerControl:
    """Stops and restarts the supervised llama-server through its pause flag."""

    def __init__(self, cfg: GpuGuardConfig, model: ModelConfig):
        self.flag = Path(cfg.pause_flag)
        self.base_url = model.base_url.rstrip("/")
        self.port = urlparse(self.base_url).port or 80

    def flagged(self) -> bool:
        return self.flag.exists()

    async def stop(self) -> None:
        self.flag.parent.mkdir(parents=True, exist_ok=True)
        self.flag.write_text(f"paused by agent-harness at {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
        for pid in await self._listening_pids():
            code, out, err = await run_cmd(["taskkill", "/PID", str(pid), "/F"], timeout=30)
            log.info("stopped model server pid %s (exit %s) %s", pid, code, (out + err).strip()[:200])

    async def start(self) -> None:
        await asyncio.to_thread(self.flag.unlink, missing_ok=True)

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                return (await client.get(f"{self.base_url}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def _listening_pids(self) -> set[int]:
        _, out, _ = await run_cmd(["netstat", "-ano", "-p", "TCP"], timeout=30)
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and re.search(rf":{self.port}$", parts[1]):
                if parts[4].isdigit() and int(parts[4]) > 4:
                    pids.add(int(parts[4]))
        return pids


# ---------- guard ----------
class GpuGuard:
    def __init__(self, cfg: GpuGuardConfig, model: ModelConfig, scheduler, busy: Callable[[], bool],
                 detect: Callable[[], Awaitable[list[dict]]] | None = None, control: ServerControl | None = None,
                 on_pause: Callable[[list[dict]], None] | None = None,
                 on_resume: Callable[[float], None] | None = None,
                 data_dir: Path | str | None = None):
        self.cfg = cfg
        self.scheduler = scheduler
        self.busy = busy
        self.detector = detect or Detector(cfg)
        self.control = control or ServerControl(cfg, model)
        self.on_pause = on_pause
        self.on_resume = on_resume
        self._state_path = Path(data_dir) / MANUAL_HOLD_FILE if data_dir is not None else None
        self.state = CLEAR
        self.signals: list[dict] = []
        self.reasons: list[dict] = []   # what caused the current pause
        self.manual = False             # paused by hand
        self.manual_until: float | None = None  # epoch seconds; None means until explicitly resumed
        self.manual_duration_seconds: int | None = None
        self.override: frozenset | None = None  # triggers the user chose to ignore
        self.changed_at = time.time()
        self.last_check = 0.0
        self.pauses = 0
        self.paused_seconds_total = 0.0
        self._paused_at = 0.0
        self._clear_since: float | None = None
        self._resume_now = False
        self._drain_deadline = 0.0
        self._resume_started = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        """True while model calls must wait."""
        return self.state != CLEAR

    def status(self) -> dict:
        remaining = (max(0, round(self.manual_until - time.time()))
                     if self.manual and self.manual_until is not None else None)
        return {"enabled": self.cfg.enabled, "state": self.state, "signals": self.signals, "reasons": self.reasons,
                "manual": self.manual, "override": bool(self.override), "since": self.changed_at,
                "manual_until": self.manual_until, "manual_remaining_seconds": remaining,
                "manual_duration_seconds": self.manual_duration_seconds,
                "last_check": self.last_check, "resume_after_seconds": self.cfg.resume_after_seconds,
                "clear_for_seconds": (round(time.monotonic() - self._clear_since)
                                      if self._clear_since is not None else None),
                "plex_error": getattr(self.detector, "plex_error", "")}

    def start(self) -> None:
        if not self.cfg.enabled or self._task is not None:
            return
        self._restore_manual_hold()
        if self.control.flagged():  # the daemon stopped while paused: the server is down until we decide
            self._set(PAUSED)
            self.scheduler.set_paused(True)
            self._paused_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="gpu-guard")

    def _restore_manual_hold(self) -> None:
        """Restore a manual hold persisted before the daemon last stopped, if it hasn't expired."""
        if self._state_path is None:
            return
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            log.warning("GPU guard: could not read %s", self._state_path, exc_info=True)
            return
        try:
            data = json.loads(raw)
            manual = bool(data["manual"])
            manual_until = data.get("manual_until")
            if manual_until is not None:
                manual_until = float(manual_until)
                if not math.isfinite(manual_until):
                    raise ValueError(f"non-finite manual_until: {manual_until!r}")
            manual_duration_seconds = data.get("manual_duration_seconds")
        except (ValueError, KeyError, TypeError):
            log.warning("GPU guard: ignoring malformed hold state file %s", self._state_path)
            return
        if not manual:
            return
        if manual_until is not None and time.time() >= manual_until:
            self._clear_persisted_hold()  # expired while the daemon was down: resume normally
            return
        self.manual = True
        self.manual_until = manual_until
        self.manual_duration_seconds = manual_duration_seconds
        self.reasons = [_manual_reason()]

    def _persist_hold(self) -> None:
        if self._state_path is None:
            return
        payload = {"manual": True, "manual_until": self.manual_until,
                   "manual_duration_seconds": self.manual_duration_seconds}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=self._state_path.name + ".", suffix=".tmp",
                                            dir=str(self._state_path.parent))
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self._state_path)
        except OSError:
            log.warning("GPU guard: could not persist manual hold to %s", self._state_path, exc_info=True)

    def _clear_persisted_hold(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.unlink(missing_ok=True)
        except OSError:
            log.warning("GPU guard: could not clear persisted manual hold at %s", self._state_path, exc_info=True)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def pause(self, duration_seconds: int | None = None) -> None:
        self.manual = True
        self.manual_until = time.time() + duration_seconds if duration_seconds else None
        self.manual_duration_seconds = duration_seconds
        self._persist_hold()
        if self.state in (CLEAR, RESUMING):
            self.reasons = [_manual_reason()]
            was_clear = self.state == CLEAR
            self._set(PAUSING)
            self.scheduler.set_paused(True)
            self._drain_deadline = time.monotonic() + self.cfg.drain_timeout_seconds
            if was_clear:
                self._paused_at = time.time()
                self.pauses += 1
                if self.on_pause:
                    self.on_pause(self.reasons)
        self._wake.set()

    def resume(self, *, override_signals: bool = True) -> None:
        """Clear a manual pause, optionally ignoring current automatic triggers until they change."""
        self.manual = False
        self.manual_until = None
        self.manual_duration_seconds = None
        self._clear_persisted_hold()
        keys = frozenset(s["key"] for s in self.signals)
        self.override = (keys or None) if override_signals else None
        self._resume_now = True
        self._wake.set()

    async def _loop(self) -> None:
        startup = True
        while True:
            try:
                await self.check(startup=startup)
                startup = False
            except Exception:  # noqa: BLE001 - keep guarding
                log.exception("GPU guard check failed")
            fast = self.state in (PAUSING, RESUMING)
            timeout = 2 if fast else self.cfg.poll_seconds
            if self.manual and self.manual_until is not None:
                timeout = min(timeout, max(0.05, self.manual_until - time.time()))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _set(self, state: str) -> None:
        if state != self.state:
            log.info("GPU guard: %s -> %s %s", self.state, state, [s["detail"] for s in self.reasons])
            self.state = state
            self.changed_at = time.time()

    async def check(self, startup: bool = False) -> None:
        if self.manual and self.manual_until is not None and time.time() >= self.manual_until:
            self.resume(override_signals=False)
        self.signals = await self.detector()
        self.last_check = time.time()
        keys = frozenset(s["key"] for s in self.signals)
        if self.override is not None and (not keys or not keys <= self.override):
            self.override = None
        want = self.manual or (bool(keys) and self.override is None)
        now = time.monotonic()

        if want:
            await self._hold(now)
            return

        if self._clear_since is None:
            self._clear_since = now
        if self.state == PAUSING:  # the trigger went away before the model was stopped
            self._finish_resume()
        elif (self.state == PAUSED and not self.busy()
              and (startup or self._resume_now or now - self._clear_since >= self.cfg.resume_after_seconds)):
            self._resume_now = False
            await self.control.start()
            self._resume_started = now
            self._set(RESUMING)
        if self.state == RESUMING and (await self.control.healthy()
                                       or now - self._resume_started >= HEALTH_TIMEOUT_SECONDS):
            self._finish_resume()

    async def _hold(self, now: float) -> None:
        self._clear_since = None
        self._resume_now = False
        if self.state == PAUSED and not self.manual and self.signals:
            self.reasons = list(self.signals)
        if self.state in (CLEAR, RESUMING):
            self.reasons = list(self.signals) if self.signals else [_manual_reason()]
            was_clear = self.state == CLEAR
            self._set(PAUSING)
            self.scheduler.set_paused(True)
            self._drain_deadline = now + self.cfg.drain_timeout_seconds
            if was_clear:
                self._paused_at = time.time()
                self.pauses += 1
                if self.on_pause:
                    self.on_pause(self.reasons)
        if self.state == PAUSING and (not self.busy() or now >= self._drain_deadline):
            await self.control.stop()
            self._set(PAUSED)

    def _finish_resume(self) -> None:
        seconds = time.time() - self._paused_at if self._paused_at else 0.0
        self.paused_seconds_total += seconds
        self.reasons = []
        self._set(CLEAR)
        if self.on_resume:
            self.on_resume(seconds)
        self.scheduler.set_paused(False)


def describe(reasons: list[dict]) -> str:
    details = []
    for r in reasons:
        if r["detail"] not in details:
            details.append(r["detail"])
    return ", ".join(details) or "GPU busy"
