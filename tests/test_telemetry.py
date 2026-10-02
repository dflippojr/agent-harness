"""Lock-hold and event-loop-stall telemetry (#257)."""

from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient

from harness import telemetry
from harness.api import create_app
from harness.db import Database
from harness.manager import Manager
from harness.metrics import _Out, _histogram_lines

from test_daemon import Script, make_cfg


def test_histogram_records_cumulative_buckets_sum_and_count():
    h = telemetry.Histogram((0.001, 0.01, 0.1))
    for v in (0.0005, 0.005, 0.005, 0.5):
        h.observe(v)
    cumulative, total, count = h.snapshot()
    assert cumulative == [(0.001, 1), (0.01, 3), (0.1, 3)]
    assert count == 4 and abs(total - 0.5105) < 1e-9
    assert h.quantile(0.5) == 0.01
    assert h.quantile(0.99) == float("inf")
    assert telemetry.Histogram().quantile(0.99) == 0.0


def test_histogram_is_thread_safe():
    h = telemetry.Histogram()
    threads = [threading.Thread(target=lambda: [h.observe(0.002) for _ in range(2000)]) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert h.snapshot()[2] == 8000


def test_histogram_rendering_is_prometheus_text():
    h = telemetry.Histogram((0.001, 0.01))
    h.observe(0.005)
    out = _Out()
    _histogram_lines(out, "x_seconds", "help", [({"method": "m"}, h)])
    assert out.lines == [
        "# HELP x_seconds help", "# TYPE x_seconds histogram",
        'x_seconds_bucket{method="m",le="0.001"} 0', 'x_seconds_bucket{method="m",le="0.01"} 1',
        'x_seconds_bucket{method="m",le="+Inf"} 1', 'x_seconds_sum{method="m"} 0.005',
        'x_seconds_count{method="m"} 1']


def test_timed_lock_records_outermost_hold_per_method():
    family = telemetry.HistogramFamily()
    lock = telemetry.TimedLock(family)

    def outer():
        with lock:
            inner()

    def inner():
        with lock:
            time.sleep(0.003)

    outer()
    labels = dict(family.items())
    assert list(labels) == ["outer"]
    assert labels["outer"].snapshot()[2] == 1
    assert labels["outer"].snapshot()[1] >= 0.003


def test_database_methods_feed_lock_histogram(tmp_path):
    db = Database(tmp_path / "t.db")
    before = dict(telemetry.lock_held.items()).get("list_jobs")
    n = before.snapshot()[2] if before else 0
    db.list_jobs()
    assert dict(telemetry.lock_held.items())["list_jobs"].snapshot()[2] == n + 1
    db.close()


def test_loop_probe_sees_a_blocked_loop():
    hist = telemetry.Histogram()

    async def run():
        probe = telemetry.LoopLagProbe(hist, interval=0.005)
        probe.start()
        await asyncio.sleep(0.03)
        time.sleep(0.12)
        await asyncio.sleep(0.03)
        await probe.stop()

    asyncio.run(run())
    assert hist.quantile(1.0) >= 0.1


def test_asyncio_debug_is_opt_in(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        was = loop.get_debug()
        assert telemetry.enable_asyncio_debug() is False
        assert loop.get_debug() == was
        monkeypatch.setenv("HARNESS_ASYNCIO_DEBUG", "1")
        assert telemetry.enable_asyncio_debug() is True
        assert loop.get_debug() and loop.slow_callback_duration == 0.05

    monkeypatch.delenv("HARNESS_ASYNCIO_DEBUG", raising=False)
    asyncio.run(check())


def test_metrics_endpoint_exposes_new_series(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([]))
    with TestClient(create_app(m)) as client:
        time.sleep(0.05)
        text = client.get("/metrics").text
    assert "# TYPE harness_db_lock_held_seconds histogram" in text
    assert 'harness_db_lock_held_seconds_bucket{method="' in text
    assert 'harness_db_lock_held_seconds_count{method="' in text
    assert "# TYPE harness_event_loop_stall_seconds histogram" in text
    assert 'harness_event_loop_stall_seconds_bucket{le="0.0005"}' in text
    assert "harness_event_loop_stall_seconds_count " in text
