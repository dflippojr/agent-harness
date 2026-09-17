"""GPU queue: one session runs at a time.

A session holds the slot for its whole run, not per generation. With a single llama-server slot, interleaving
sessions would evict the prompt cache, and re-reading a long prompt costs over a minute on Qwen. A session
gives the slot up while it waits for an approval.

While the GPU guard has paused the queue (a game or a Plex transcode needs the GPU), nobody is granted the slot.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Callable


class GpuScheduler:
    def __init__(self, on_change: Callable[[dict[str, int]], None] | None = None,
                 eligible: Callable[[str], bool] | None = None):
        self.holder: str | None = None
        self.paused = False
        self._waiters: OrderedDict[str, asyncio.Future] = OrderedDict()
        self._change_waiters: set[asyncio.Future] = set()
        self._on_change = on_change
        self._eligible = eligible

    def positions(self) -> dict[str, int]:
        """0 = running, 1 = next, ..."""
        out = {self.holder: 0} if self.holder else {}
        out.update({sid: i + 1 for i, sid in enumerate(self._waiters)})
        return out

    def _changed(self) -> None:
        for waiter in self._change_waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._change_waiters.clear()
        if self._on_change:
            self._on_change(self.positions())

    async def wait_for_drain(self, session_ids: set[str]) -> None:
        """Wait until a snapshot of session holders/waiters has left the queue."""
        while session_ids.intersection(self.positions()):
            waiter = asyncio.get_running_loop().create_future()
            self._change_waiters.add(waiter)
            try:
                await waiter
            finally:
                self._change_waiters.discard(waiter)

    async def acquire(self, sid: str, front: bool = False) -> None:
        """Wait for the slot. `front` puts the session first in line (it had the slot and stepped aside)."""
        if self.holder == sid:
            return
        if self.holder is None and not self._waiters and not self.paused:
            if self._eligible is None or self._eligible(sid):
                self.holder = sid
                self._changed()
                return
        fut = asyncio.get_running_loop().create_future()
        self._waiters[sid] = fut
        if front:
            self._waiters.move_to_end(sid, last=False)
        self._changed()
        try:
            await fut
        except asyncio.CancelledError:
            if self._waiters.pop(sid, None) is None and self.holder == sid:
                self.release(sid)  # granted just as we were cancelled
            else:
                self._changed()
            raise

    def release(self, sid: str) -> None:
        if self.holder != sid:
            if self._waiters.pop(sid, None) is not None:
                self._changed()
            return
        self.holder = None
        self._grant_next()
        self._changed()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if not paused and self.holder is None:
            self._grant_next()
        self._changed()

    def _grant_next(self) -> None:
        if self.paused:
            return
        skipped: OrderedDict[str, asyncio.Future] = OrderedDict()
        granted = False
        while self._waiters:
            nxt, fut = self._waiters.popitem(last=False)
            if fut.done():
                continue
            if self._eligible is not None and not self._eligible(nxt):
                skipped[nxt] = fut
                continue
            self.holder = nxt
            fut.set_result(None)
            granted = True
            break
        rest = OrderedDict(self._waiters)
        self._waiters = OrderedDict()
        self._waiters.update(skipped)
        self._waiters.update(rest)
        _ = granted


class QueueFull(Exception):
    pass


class GpuExclusive(Exception):
    """The GPU is handed over to something else (image generation) for now."""


class InferenceGate:
    """Orders individual model calls between agent turns and requests to the inference endpoint (endpoint.py).

    The GPU slot above belongs to sessions; this gate sits under it, per call. Endpoint requests (an editor or a
    script waiting on an answer) go ahead of the next agent turn: they wait only for the agent call already in
    flight, and several can run at once (llama-server queues them itself). An agent call waits while any endpoint
    request is waiting or running, except that once an agent call has waited `fair_seconds`, new endpoint requests
    line up behind it, so a busy editor can't stall a task forever.

    `exclusive()` hands the whole GPU to something else (image generation, which needs the model server stopped): it
    waits for model calls in flight, then agent turns wait and endpoint requests are refused (GpuExclusive) until it's
    released.
    """

    def __init__(self, max_waiting: int = 4, fair_seconds: float = 90):
        self.max_waiting = max_waiting
        self.fair_seconds = fair_seconds
        self.agent_active = 0
        self.endpoint_active = 0
        self.endpoint_waiting = 0
        self._agent_waiting_since: list[float] = []
        self.exclusive_active = False
        self.exclusive_waiting = 0
        self._cond = asyncio.Condition()

    def _agent_starved(self) -> bool:
        return bool(self._agent_waiting_since) and time.monotonic() - min(self._agent_waiting_since) >= self.fair_seconds

    @property
    def busy(self) -> bool:
        return bool(self.agent_active or self.endpoint_active)

    @property
    def exclusive(self) -> bool:
        return self.exclusive_active or bool(self.exclusive_waiting)

    async def acquire_exclusive(self):
        async with self._cond:
            self.exclusive_waiting += 1
            try:
                while self.agent_active or self.endpoint_active or self.exclusive_active:
                    await self._cond.wait()
            finally:
                self.exclusive_waiting -= 1
            self.exclusive_active = True
        return _Release(self, "exclusive")

    async def agent_turn(self):
        since = time.monotonic()
        async with self._cond:
            self._agent_waiting_since.append(since)
            try:
                # Endpoint requests that are only waiting because this agent call is starved don't block it.
                while self.exclusive or self.endpoint_active or (self.endpoint_waiting and not self._agent_starved()):
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=5)  # re-check fairness periodically
                    except asyncio.TimeoutError:
                        pass
            finally:
                self._agent_waiting_since.remove(since)
            self.agent_active += 1
        return _Release(self, "agent")

    async def endpoint_request(self):
        async with self._cond:
            if self.exclusive:
                raise GpuExclusive()
            if self.endpoint_waiting >= self.max_waiting:
                raise QueueFull()
            self.endpoint_waiting += 1
            try:
                while self.agent_active or self._agent_starved():
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
            finally:
                self.endpoint_waiting -= 1
                self._cond.notify_all()
            self.endpoint_active += 1
        return _Release(self, "endpoint")


class _Release:
    def __init__(self, gate: InferenceGate, kind: str):
        self.gate, self.kind, self.done = gate, kind, False

    async def release(self) -> None:
        if self.done:
            return
        self.done = True
        async with self.gate._cond:
            if self.kind == "agent":
                self.gate.agent_active -= 1
            elif self.kind == "exclusive":
                self.gate.exclusive_active = False
            else:
                self.gate.endpoint_active -= 1
            self.gate._cond.notify_all()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.release()
