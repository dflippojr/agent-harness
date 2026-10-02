"""Event-loop and SQLite-lock timing (#257): thread-safe histograms rendered on GET /metrics.

`TimedLock` replaces `Database.lock` and records how long each outermost `with db.lock:` block holds the lock,
labelled by the calling method. `LoopLagProbe` records how late a fixed-interval sleep wakes up, which is the
time the event loop was unable to run other callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

log = logging.getLogger(__name__)

LOCK_BUCKETS = (0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5)
LOOP_BUCKETS = LOCK_BUCKETS
PROBE_INTERVAL = 0.01
SLOW_CALLBACK_SECONDS = 0.05


class Histogram:
    def __init__(self, buckets: tuple[float, ...] = LOCK_BUCKETS):
        self.buckets = tuple(buckets)
        self._lock = threading.Lock()
        self._counts = [0] * len(self.buckets)
        self._sum = 0.0
        self._count = 0

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    self._counts[i] += 1
                    break

    def snapshot(self) -> tuple[list[tuple[float, int]], float, int]:
        """Cumulative (le, count) pairs, sum, and count."""
        with self._lock:
            running, cumulative = 0, []
            for bound, n in zip(self.buckets, self._counts):
                running += n
                cumulative.append((bound, running))
            return cumulative, self._sum, self._count

    def quantile(self, q: float) -> float:
        """Upper bound of the bucket holding the q-quantile (inf past the last bucket, 0 when empty)."""
        cumulative, _, count = self.snapshot()
        if not count:
            return 0.0
        target = q * count
        for bound, n in cumulative:
            if n >= target:
                return bound
        return float("inf")


class HistogramFamily:
    """Histograms keyed by one label value."""

    def __init__(self, buckets: tuple[float, ...] = LOCK_BUCKETS):
        self.buckets = buckets
        self._lock = threading.Lock()
        self._by_label: dict[str, Histogram] = {}

    def observe(self, label: str, value: float) -> None:
        h = self._by_label.get(label)
        if h is None:
            with self._lock:
                h = self._by_label.setdefault(label, Histogram(self.buckets))
        h.observe(value)

    def items(self) -> list[tuple[str, Histogram]]:
        with self._lock:
            return sorted(self._by_label.items())


lock_held = HistogramFamily(LOCK_BUCKETS)
loop_stall = Histogram(LOOP_BUCKETS)


class TimedLock:
    """Re-entrant lock that records the hold time of each outermost acquisition, per calling method."""

    def __init__(self, family: HistogramFamily = lock_held):
        self._lock = threading.RLock()
        self._family = family
        self._owner = threading.local()

    def __enter__(self):
        method = sys._getframe(1).f_code.co_name
        self._lock.acquire()
        owner = self._owner
        depth = getattr(owner, "depth", 0)
        if depth == 0:
            owner.method, owner.start = method, time.perf_counter()
        owner.depth = depth + 1
        return self

    def __exit__(self, *exc):
        owner = self._owner
        owner.depth -= 1
        if owner.depth == 0:
            self._family.observe(owner.method, time.perf_counter() - owner.start)
        self._lock.release()
        return False

    def acquire(self, *args, **kwargs):
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()


class LoopLagProbe:
    """Sleeps PROBE_INTERVAL repeatedly and records how much later than that it woke up."""

    def __init__(self, hist: Histogram = loop_stall, interval: float = PROBE_INTERVAL):
        self.hist, self.interval = hist, interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            before = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.hist.observe(max(0.0, time.perf_counter() - before - self.interval))


def asyncio_debug_requested() -> bool:
    return os.environ.get("HARNESS_ASYNCIO_DEBUG", "").strip().lower() in ("1", "true", "yes")


def enable_asyncio_debug() -> bool:
    """Turn on loop debug with a 50 ms slow-callback threshold when HARNESS_ASYNCIO_DEBUG is set. Call on the loop."""
    if not asyncio_debug_requested():
        return False
    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    loop.slow_callback_duration = SLOW_CALLBACK_SECONDS
    log.info("asyncio debug enabled (slow_callback_duration=%.3fs)", SLOW_CALLBACK_SECONDS)
    return True
