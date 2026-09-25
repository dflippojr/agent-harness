"""Per-session Docker sandbox.

Each session gets one long-lived container, named after the session, so a daemon restart can reattach to it.
Only the session workspace is mounted. The container sits on an internal network with no route out; an
approved network command temporarily attaches an egress network.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path

from .config import SandboxConfig


class SandboxUnavailable(Exception):
    """Docker isn't reachable or the container can't be started."""


def _spawn(args: list[str], has_input: bool, env: dict | None) -> subprocess.Popen:
    return subprocess.Popen(
        args, stdin=subprocess.PIPE if has_input else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env={**os.environ, **env} if env else None,
    )


def _run_blocking(args: list[str], input_: str | None, timeout: float, env: dict | None,
                  started: list, cancelled: threading.Event) -> tuple[int, str, str]:
    """Spawn, wait and kill in one worker thread. The process is published in `started` the moment it exists, and
    `cancelled` is checked right after: a cancel that lands while Popen is still running cannot be stopped in that
    thread, so this side kills the child instead (the awaiting side kills whatever is already published)."""
    proc = _spawn(args, input_ is not None, env)
    started.append(proc)
    if cancelled.is_set():
        proc.kill()
    try:
        out, err = proc.communicate(input_, timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
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
        for proc in list(started):
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
    def __init__(self, session_id: str, workspace: Path, cfg: SandboxConfig):
        self.session_id = session_id
        self.workspace = workspace.resolve()
        self.cfg = cfg
        self.name = f"harness-{session_id}"
        self._lock = asyncio.Lock()

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
            "-w", "/workspace",
            self.cfg.image, "sleep", "infinity",
        ], timeout=120)
        if code != 0:
            raise SandboxUnavailable(f"could not start sandbox container: {(err or out).strip()[:500]}")
        return "created"

    async def exec(self, command: str, timeout: int = 120, network: bool = False) -> tuple[int, str]:
        async with self._lock:
            await self.ensure_running()
            if network:
                code, out, err = await run_cmd(
                    ["docker", "network", "connect", self.cfg.egress_network, self.name], timeout=30)
                if code != 0 and "already exists" not in err:
                    raise SandboxUnavailable(f"could not enable network: {err.strip()[:300]}")
            try:
                # `timeout` inside the container stops the command; the host timeout is only a backstop.
                code, out, err = await run_cmd(
                    ["docker", "exec", "-w", "/workspace", self.name,
                     "timeout", "-k", "5", str(timeout), "sh", "-c", command],
                    timeout=timeout + 30,
                )
            finally:
                if network:
                    await asyncio.shield(run_cmd(
                        ["docker", "network", "disconnect", "-f", self.cfg.egress_network, self.name], timeout=30))
        output = out + (("\n" + err) if err else "")
        if code == 124:
            output += f"\n[command timed out after {timeout}s]"
        return code, output

    async def stop(self) -> None:
        await run_cmd(["docker", "stop", "-t", "2", self.name], timeout=60)

    async def restart(self) -> None:
        if await self._state() is not None:
            await run_cmd(["docker", "restart", "-t", "2", self.name], timeout=60)

    async def remove(self) -> None:
        await run_cmd(["docker", "rm", "-f", self.name], timeout=60)
