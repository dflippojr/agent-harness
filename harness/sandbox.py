"""Per-session Docker sandbox.

Each session gets one long-lived container, named after the session, so a daemon restart can reattach to it.
Only the session workspace is mounted. The container sits on an internal network with no route out; an
approved network command temporarily attaches an egress network.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import subprocess
import threading
from pathlib import Path

from .config import SandboxConfig
from .fileops import CappedStream


SETUP_TIMEOUT = 600


class SandboxUnavailable(Exception):
    """Docker isn't reachable or the container can't be started."""


if os.name == "nt":
    import ctypes
    import msvcrt
    _CancelIoEx = ctypes.WinDLL("kernel32", use_last_error=True).CancelIoEx
    _CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _CancelIoEx.restype = ctypes.c_int
else:
    msvcrt = None
    _CancelIoEx = None


def _spawn(args: list[str], has_input: bool, env: dict | None) -> subprocess.Popen:
    return subprocess.Popen(
        args, stdin=subprocess.PIPE if has_input else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env={**os.environ, **env} if env else None,
    )


def _pump_stream(stream, cap: CappedStream) -> None:
    """Drain `stream` (binary) into `cap`. read1 returns whatever the OS has already delivered instead of waiting to
    fill a buffer, so output the parent wrote before exiting is fed even if a detached child keeps the pipe open and
    the blocked read is later cancelled (#315)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            block = stream.read1(65536)
            if not block:
                break
            text = decoder.decode(block)
            if text:
                cap.feed(text)
    except (ValueError, OSError):
        pass
    tail = decoder.decode(b"", final=True)
    if tail:
        cap.feed(tail)


def _read_end(stream) -> tuple[int | None, int | None]:
    """Return (fd, native handle) captured before a pump blocks in read()."""
    if stream is None:
        return None, None
    try:
        fd = stream.fileno()
    except (OSError, ValueError):
        return None, None
    handle = None
    if msvcrt is not None:
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            handle = None
    return fd, handle


def _unblock_read(fd: int | None, handle: int | None) -> None:
    """Wake a pump blocked in read() when a grandchild still holds the write end.

    On Windows CloseHandle/os.close wait for pending I/O, so CancelIoEx first.
    """
    if _CancelIoEx is not None and handle is not None:
        _CancelIoEx(handle, None)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _run_blocking(args: list[str], input_: str | None, timeout: float, env: dict | None,
                  started: list, cancelled: threading.Event) -> tuple[int, str, str]:
    """Spawn, wait and kill in one worker thread. The process is published in `started` the moment it exists, and
    `cancelled` is checked right after: a cancel that lands while Popen is still running cannot be stopped in that
    thread, so this side kills the child instead (the awaiting side kills whatever is already published).

    Each of stdout and stderr is drained with a 1,000,000-character ceiling so a runaway command cannot exhaust
    daemon memory. Timeout is exit 124, matching the previous communicate() path.
    """
    proc = _spawn(args, input_ is not None, env)
    started.append(proc)
    if cancelled.is_set():
        proc.kill()
    out_cap, err_cap = CappedStream(), CappedStream()
    out_fd, out_handle = _read_end(proc.stdout)
    err_fd, err_handle = _read_end(proc.stderr)
    pumps = [
        threading.Thread(target=_pump_stream, args=(proc.stdout, out_cap), daemon=True),
        threading.Thread(target=_pump_stream, args=(proc.stderr, err_cap), daemon=True),
    ]
    for t in pumps:
        t.start()
    if input_ is not None and proc.stdin is not None:
        def _write_stdin() -> None:
            try:
                proc.stdin.write(input_.encode("utf-8", errors="replace"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=_write_stdin, daemon=True).start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    for t in pumps:
        t.join(timeout=1)
    if any(t.is_alive() for t in pumps):
        _unblock_read(out_fd, out_handle)
        _unblock_read(err_fd, err_handle)
        for t in pumps:
            t.join(timeout=10)
        proc.stdout = proc.stderr = None
    out, err = out_cap.get(), err_cap.get()
    if timed_out:
        return 124, out, err + f"\n[timed out after {timeout:.0f}s]"
    return proc.returncode, out, err


async def run_cmd(args: list[str], timeout: float = 60, input_: str | None = None,
                  env: dict | None = None) -> tuple[int, str, str]:
    """subprocess.run that can be cancelled: cancelling the awaiting task kills the process, including one that is
    still being spawned. `env` is merged over the daemon's environment. Deliberately thread-based rather than
    asyncio subprocesses: on Python 3.12 the asyncio subprocess machinery left a cancelled call hanging when the loop
    shut down (#207)."""
    started: list = []
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(_run_blocking, args, input_, timeout, env, started, cancelled)
    except asyncio.CancelledError:
        cancelled.set()
        for proc in started.copy():
            proc.kill()
        raise


async def ensure_networks(cfg: SandboxConfig) -> None:
    code, out, err = await run_cmd(["docker", "network", "ls", "--format", "{{.Name}}"], timeout=30)
    if code != 0:
        raise SandboxUnavailable(f"docker is not reachable: {(err or out).strip()[:300]}")
    existing = set(out.split())
    icc_off = ["-o", "com.docker.network.bridge.enable_icc=false"]
    if cfg.network not in existing:
        await run_cmd(["docker", "network", "create", "--internal", *icc_off, cfg.network], timeout=30)
    if cfg.egress_network not in existing:
        await run_cmd(["docker", "network", "create", *icc_off, cfg.egress_network], timeout=30)


class Sandbox:
    def __init__(self, session_id: str, workspace: Path, cfg: SandboxConfig, *, project: str = "", setup: str = "",
                 known: bool = False, on_event=None):
        self.session_id = session_id
        self.project = project      # names the per-project pip/npm cache volumes (#429)
        self.setup = setup.strip()  # run (with network) each time a container is created
        self.known = known          # the session has run tools before, so a "created" container is a recreation
        self.on_event = on_event    # on_event(type, data): session events from here (setup result)
        self.workspace = workspace.resolve()
        self.cfg = cfg
        self.name = f"harness-{session_id}"
        self._lock = asyncio.Lock()
        self._idle_timer: asyncio.Task | None = None
        self._idle_stopped = False   # we stopped it (not a crash), so the next start owes the model a notice

    async def _state(self) -> str | None:
        code, out, _ = await run_cmd(["docker", "inspect", "-f", "{{.State.Status}}", self.name], timeout=30)
        return out.strip() if code == 0 else None

    async def ensure_running(self) -> str:
        """Start (or create) the container. Returns 'running', 'started', or 'created'."""
        state = await self._state()
        if state == "running":
            return "running"
        if state is not None:
            code, out, err = await run_cmd(["docker", "start", self.name], timeout=60)
            if code == 0:
                return "started"
            await run_cmd(["docker", "rm", "-f", self.name], timeout=30)
        await ensure_networks(self.cfg)
        code, out, err = await run_cmd([
            "docker", "run", "-d", "--init",  # --init: `sleep` would ignore SIGTERM and slow every stop
            "--name", self.name,
            "--label", f"agent-harness.session={self.session_id}",
            "--network", self.cfg.network,
            "--memory", self.cfg.memory,
            "--cpus", str(self.cfg.cpus),
            "--pids-limit", str(self.cfg.pids),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
            "--mount", f"type=bind,source={self.workspace},target=/workspace",
            *self._cache_mounts(),
            "-w", "/workspace",
            self.cfg.image, "sleep", "infinity",
        ], timeout=120)
        if code != 0:
            raise SandboxUnavailable(f"could not start sandbox container: {(err or out).strip()[:500]}")
        return "created"

    def _cache_mounts(self) -> list[str]:
        """Named per-project volumes for the pip and npm caches, so a recreated container reinstalls from cache."""
        if not self.project:
            return []
        out: list[str] = []
        for name, target in (("pip", "/root/.cache/pip"), ("npm", "/root/.npm")):
            out += ["--mount", f"type=volume,source=harness-cache-{name}-{self.project},target={target}"]
        return out

    async def _network(self, attach: bool) -> None:
        if attach:
            code, out, err = await run_cmd(
                ["docker", "network", "connect", self.cfg.egress_network, self.name], timeout=30)
            if code != 0 and "already exists" not in err:
                raise SandboxUnavailable(f"could not enable network: {err.strip()[:300]}")
        else:
            await asyncio.shield(run_cmd(
                ["docker", "network", "disconnect", "-f", self.cfg.egress_network, self.name], timeout=30))

    async def _run_setup(self) -> str:
        """Run the project's setup command in a fresh container. Returns 'ok' or a failure description, and reports
        it as a `sandbox_setup` session event; a failing command doesn't raise."""
        try:
            await self._network(True)
            try:
                code, out, err = await run_cmd(
                    ["docker", "exec", "-w", "/workspace", self.name,
                     "timeout", "-k", "5", str(SETUP_TIMEOUT), "sh", "-c", self.setup], timeout=SETUP_TIMEOUT + 30)
            finally:
                await self._network(False)
            detail = "" if code == 0 else f"exit {code}: {(err or out).strip()[-500:]}"
        except SandboxUnavailable as e:
            code, detail = 1, str(e)
        if self.on_event:
            self.on_event("sandbox_setup", {"command": self.setup, "ok": code == 0, "detail": detail})
        return "ok" if code == 0 else f"failed ({detail})"

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def arm_idle_stop(self) -> None:
        """(Re)start the idle timer; a no-op unless sandbox.idle_stop_seconds is set. Needs a running loop."""
        self._cancel_idle_timer()
        if self.cfg.idle_stop_seconds > 0:
            self._idle_timer = asyncio.get_running_loop().create_task(self._idle_stop_after())

    async def _idle_stop_after(self) -> None:
        await asyncio.sleep(self.cfg.idle_stop_seconds)
        self._idle_timer = None
        await self.stop_if_idle()

    async def stop_if_idle(self) -> bool:
        """Stop the container unless a command holds the lock (or idle stopping is off). Used when the session waits
        on an approval or the GPU guard, and by the idle timer. exec restarts it."""
        if self.cfg.idle_stop_seconds <= 0 or self._lock.locked():
            return False
        async with self._lock:
            if await self._state() != "running":
                return False
            self._cancel_idle_timer()
            await self.stop()
            self._idle_stopped = True
            return True

    async def exec(self, command: str, timeout: int = 120, network: bool = False) -> tuple[int, str]:
        self._cancel_idle_timer()
        try:
            return await self._exec(command, timeout, network)
        finally:
            self.arm_idle_stop()

    async def _exec(self, command: str, timeout: int, network: bool) -> tuple[int, str]:
        notice = ""
        async with self._lock:
            started = await self.ensure_running()
            setup_result = await self._run_setup() if started == "created" and self.setup else None
            if started == "created" and self.known:
                notice = ("[sandbox environment recreated: /workspace and your checkpoints are intact, but packages "
                          "installed outside /workspace and processes you started earlier (servers, watchers) are gone. "
                          + (f"The project setup command was re-run: {setup_result}."
                             if setup_result else "No project setup command is configured, so reinstall what you need.")
                          + "]\n")
            elif self._idle_stopped and started != "running":
                notice = ("[sandbox restarted after an idle stop: processes you started earlier (servers, watchers) "
                          "are no longer running; /workspace is intact.]\n")
            self._idle_stopped = False
            self.known = True
            if network:
                await self._network(True)
            try:
                # `timeout` inside the container stops the command; the host timeout is only a backstop.
                code, out, err = await run_cmd(
                    ["docker", "exec", "-w", "/workspace", self.name,
                     "timeout", "-k", "5", str(timeout), "sh", "-c", command],
                    timeout=timeout + 30,
                )
            finally:
                if network:
                    await self._network(False)
        output = notice + out + (("\n" + err) if err else "")
        if code == 124:
            output += f"\n[command timed out after {timeout}s]"
        return code, output

    async def stop(self) -> None:
        self._cancel_idle_timer()
        self._idle_stopped = False
        await run_cmd(["docker", "stop", "-t", "2", self.name], timeout=60)

    async def restart(self) -> None:
        if await self._state() is not None:
            await run_cmd(["docker", "restart", "-t", "2", self.name], timeout=60)

    async def remove(self) -> None:
        await run_cmd(["docker", "rm", "-f", self.name], timeout=60)
