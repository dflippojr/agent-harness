"""GPU queue: one session runs at a time.

A session holds the slot for its whole run, not per generation. With a single llama-server slot, interleaving
sessions would evict the prompt cache, and re-reading a long prompt costs over a minute on Qwen. A session
gives the slot up while it waits for an approval.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Callable


class GpuScheduler:
    def __init__(self, on_change: Callable[[dict[str, int]], None] | None = None):
        self.holder: str | None = None
        self._waiters: OrderedDict[str, asyncio.Future] = OrderedDict()
        self._on_change = on_change

    def positions(self) -> dict[str, int]:
        """0 = running, 1 = next, ..."""
        out = {self.holder: 0} if self.holder else {}
        out.update({sid: i + 1 for i, sid in enumerate(self._waiters)})
        return out

    def _changed(self) -> None:
        if self._on_change:
            self._on_change(self.positions())

    async def acquire(self, sid: str) -> None:
        if self.holder == sid:
            return
        if self.holder is None and not self._waiters:
            self.holder = sid
            self._changed()
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters[sid] = fut
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
        while self._waiters:
            nxt, fut = self._waiters.popitem(last=False)
            if not fut.done():
                self.holder = nxt
                fut.set_result(None)
                break
        self._changed()
