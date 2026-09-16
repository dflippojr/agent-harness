"""Runners: machines that execute tool calls for sessions targeting them (Phase 4: the MacBook).

The model and the agent loop stay on the tower. A runner connects *outbound* to the daemon over the tailnet and
long-polls for requests (`POST /runners/<name>/poll`), then posts each result (`POST /runners/<name>/results`).
Long-polling instead of a WebSocket keeps the runner stdlib-only (the Mac's stock Python 3.9) and copes with
sleep: a sleeping Mac simply stops polling, and a process frozen mid-command carries on after wake.

Delivery is at-least-once. Each poll lists the request ids the runner is still working on; a request that was
handed out but isn't in that list is handed out again, and the runner answers a repeat from its result cache.
A new runner instance id (the process restarted) fails the requests the old instance had taken, since their
effects are unknown.

Timeouts only count while the runner is online, so a command that was running when the lid closed isn't
declared dead during the night.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import RunnerConfig
from .fileops import FILE_TOOLS, MAX_PUT_BYTES, ToolError
from .tools import shell_result, tool_schemas

log = logging.getLogger("harness.remote")

ONLINE_SECONDS = 45       # a runner that hasn't polled for this long is offline
POLL_HOLD_SECONDS = 25    # how long a poll waits for work before returning empty
REDELIVER_SECONDS = 20    # a handed-out request the runner doesn't report as in flight is resent after this
TOOL_TIMEOUT_SECONDS = 180


class RunnerOffline(Exception):
    pass


class RunnerError(Exception):
    """The runner couldn't carry out a request. `kind` is tool | git | restarted | timeout | internal."""

    def __init__(self, message: str, kind: str = "internal", status: int = 409):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class Request:
    id: str
    op: str
    params: dict
    future: asyncio.Future
    delivered_at: float = 0.0
    instance: str = ""


@dataclass
class RunnerState:
    name: str
    last_seen: float = 0.0
    instance: str = ""
    info: dict = field(default_factory=dict)
    requests: dict[str, Request] = field(default_factory=dict)
    work: asyncio.Event = field(default_factory=asyncio.Event)
    seen: asyncio.Event = field(default_factory=asyncio.Event)


class RunnerHub:
    def __init__(self, runners: dict[str, RunnerConfig], keep_awake: Callable[[str], bool] | None = None,
                 on_change: Callable[[str, bool], None] | None = None):
        self.cfg = runners
        self.state = {name: RunnerState(name) for name in runners}
        self.keep_awake = keep_awake or (lambda name: False)
        self.on_change = on_change  # (runner, online) when a runner comes online
        self._closing = False
        self.started = time.monotonic()

    def startup_grace(self) -> float:
        """Seconds left in which a runner that was connected before a daemon restart is expected back."""
        return max(0.0, ONLINE_SECONDS - (time.monotonic() - self.started))

    # auth
    def token(self, name: str) -> str:
        rc = self.cfg.get(name)
        if rc is None or not rc.token_file:
            return ""
        try:
            return Path(rc.token_file).read_text(encoding="utf-8").strip()
        except OSError:
            log.warning("runner token file %s is unreadable", rc.token_file)
            return ""

    def authorized(self, name: str, header: str | None) -> bool:
        expected = self.token(name)
        given = (header or "").removeprefix("Bearer ").strip()
        return bool(expected) and hmac.compare_digest(given, expected)

    # presence
    def online(self, name: str) -> bool:
        st = self.state.get(name)
        return bool(st and st.last_seen and time.monotonic() - st.last_seen < ONLINE_SECONDS)

    async def wait_online(self, name: str) -> None:
        st = self.state[name]
        while not self.online(name):
            st.seen.clear()
            try:
                await asyncio.wait_for(st.seen.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [{"name": n, "online": self.online(n),
                 "last_seen_seconds": round(now - st.last_seen) if st.last_seen else None,
                 "pending_requests": len(st.requests), "info": st.info} for n, st in self.state.items()]

    def close(self) -> None:
        self._closing = True
        for st in self.state.values():
            st.work.set()

    # runner side
    async def poll(self, name: str, instance: str, inflight: list[str], info: dict) -> dict:
        st = self.state[name]
        was_online = self.online(name)
        if st.instance and instance != st.instance:
            for req in list(st.requests.values()):
                if req.delivered_at and req.instance == st.instance and not req.future.done():
                    req.future.set_exception(RunnerError(
                        f"the {name} runner restarted while this was running, so its effects are unknown",
                        kind="restarted"))
                    st.requests.pop(req.id, None)
        st.instance = instance
        st.info = info or st.info
        st.last_seen = time.monotonic()
        st.seen.set()
        if not was_online:
            log.info("runner %s online (%s)", name, instance[:8])
            if self.on_change:
                self.on_change(name, True)

        deadline = time.monotonic() + POLL_HOLD_SECONDS
        inflight_ids = set(inflight or [])
        while True:
            now = time.monotonic()
            st.last_seen = now  # a held poll is a live connection
            batch = []
            for req in st.requests.values():
                if req.future.done():
                    continue
                if not req.delivered_at or (req.id not in inflight_ids and now - req.delivered_at > REDELIVER_SECONDS):
                    req.delivered_at, req.instance = now, instance
                    batch.append({"id": req.id, "op": req.op, "params": req.params})
            if batch or self._closing or now >= deadline:
                return {"requests": batch, "keep_awake": self.keep_awake(name)}
            st.work.clear()
            try:
                await asyncio.wait_for(st.work.wait(), timeout=min(5.0, deadline - now))
            except asyncio.TimeoutError:
                pass

    def result(self, name: str, rid: str, ok: bool, value=None, error: str = "", kind: str = "internal") -> bool:
        st = self.state[name]
        st.last_seen = time.monotonic()
        req = st.requests.pop(rid, None)
        if req is None or req.future.done():
            return False
        if ok:
            req.future.set_result(value)
        else:
            req.future.set_exception(RunnerError(error or "runner error", kind=kind))
        return True

    # daemon side
    async def call(self, name: str, op: str, params: dict, timeout: float = TOOL_TIMEOUT_SECONDS,
                   wait_if_offline: bool = True):
        """Send one request and wait for its result. Time spent while the runner is offline doesn't count
        against `timeout`. With wait_if_offline False, raises RunnerOffline instead of queueing."""
        st = self.state.get(name)
        if st is None:
            raise RunnerError(f"unknown runner {name!r}", status=400)
        if not wait_if_offline and not self.online(name):
            raise RunnerOffline(f"the {name} is offline or asleep")
        req = Request(id=uuid.uuid4().hex[:12], op=op, params=params,
                      future=asyncio.get_running_loop().create_future())
        st.requests[req.id] = req
        st.work.set()
        counted, last = 0.0, time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({req.future}, timeout=2)
                if done:
                    return req.future.result()
                now = time.monotonic()
                if self.online(name):
                    counted += now - last
                last = now
                if counted > timeout:
                    raise RunnerError(f"the {name} runner didn't answer {op} within {timeout:.0f} s", kind="timeout")
        except BaseException:
            st.requests.pop(req.id, None)
            if req.delivered_at and not req.future.done():
                self._send_cancel(name, req.id)
            raise

    def _send_cancel(self, name: str, rid: str) -> None:
        """Fire and forget: ask the runner to kill whatever is running for a request."""
        st = self.state[name]
        loop = asyncio.get_running_loop()
        cid = uuid.uuid4().hex[:12]
        fut = loop.create_future()
        fut.add_done_callback(lambda f: f.exception())  # nobody awaits it
        st.requests[cid] = Request(id=cid, op="cancel", params={"request_id": rid}, future=fut)
        st.work.set()

    def fire(self, name: str, op: str, params: dict) -> None:
        """Queue a request nobody waits for (e.g. kill a session's processes after a daemon restart)."""
        st = self.state[name]
        fut = asyncio.get_running_loop().create_future()
        fut.add_done_callback(lambda f: f.exception())
        rid = uuid.uuid4().hex[:12]
        st.requests[rid] = Request(id=rid, op=op, params=params, future=fut)
        st.work.set()


class RemoteWorkspace:
    """Tools for a session whose target is a runner. Same schemas as the tower; every call goes to the runner."""

    homelab = None

    def __init__(self, hub: RunnerHub, target: str, sid: str, context_tokens: int):
        self.hub = hub
        self.target = target
        self.sid = sid
        self.context_tokens = context_tokens
        self.read_lines = 2000
        self.output_chars = max(8000, int(context_tokens * 0.08 * 3.5))

    def schemas(self) -> list[dict]:
        return tool_schemas(self.read_lines, target=self.target)

    async def _call(self, op: str, params: dict, timeout: float = TOOL_TIMEOUT_SECONDS):
        try:
            return await self.hub.call(self.target, op, {"session": self.sid, **params}, timeout=timeout)
        except RunnerError as e:
            if e.kind in ("tool", "restarted", "timeout"):
                raise ToolError(str(e)) from None
            raise

    async def call(self, name: str, args: dict) -> str:
        if name in FILE_TOOLS:
            return await self._call("file", {"name": name, "args": args, "context_tokens": self.context_tokens})
        if name == "run_shell":
            timeout = max(1, min(int(args.get("timeout", 120)), 1800))
            network = bool(args.get("network", False))
            out = await self._call("shell", {"command": args["command"], "timeout": timeout, "network": network},
                                   timeout=timeout + 90)
            return shell_result(out["code"], out["output"], network, self.output_chars)
        if name == "git_clone":
            return await self._call("git_clone", args, timeout=700)
        raise ToolError(f"{name} isn't available on the {self.target}")

    async def put_file(self, path: str, data: bytes) -> str:
        """Copy bytes generated on the tower into this session's runner workspace."""
        if len(data) > MAX_PUT_BYTES:
            raise ToolError(f"file is {len(data)} bytes; limit is {MAX_PUT_BYTES}")
        return await self._call("put_file", {
            "path": path,
            "content_b64": base64.b64encode(data).decode("ascii"),
        }, timeout=120)

    async def preview(self, name: str, args: dict) -> str:
        try:
            return await self._call("preview", {"name": name, "args": args}, timeout=60)
        except (ToolError, RunnerError):
            return ""

    async def size_bytes(self) -> int:
        return int(await self._call("size", {}, timeout=120))


class RemoteSandbox:
    """What the loop does with a tower container, mapped to a runner: the process group is the 'container'."""

    def __init__(self, hub: RunnerHub, target: str, sid: str):
        self.hub, self.target, self.sid = hub, target, sid

    async def stop(self) -> None:
        pass  # nothing stays running between commands

    async def restart(self) -> None:
        self.hub.fire(self.target, "kill_session", {"session": self.sid})

    async def remove(self) -> None:
        pass  # the review/cleanup request removes the workspace
