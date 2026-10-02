"""Smoke test for the #257 event-loop benchmark script: a short run produces a well-formed report."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import bench_event_loop as bench  # noqa: E402


@pytest.mark.parametrize("no_scrape", [False, True])
def test_short_run_reports_stall_and_lock_holds(monkeypatch, capsys, no_scrape):
    argv = ["bench_event_loop.py", "--sessions", "1", "--seconds", "0.5", "--large-every", "3"]
    monkeypatch.setattr(sys, "argv", argv + (["--no-scrape"] if no_scrape else []))
    bench.main()
    out = capsys.readouterr().out
    assert "Event-loop stall" in out
    assert "| insert_event |" in out  # per-method lock-hold table is populated
    assert ("(disabled)" in out) is no_scrape


def test_fmt_buckets():
    assert bench.fmt(0.0005) == "0.5 ms"
    assert bench.fmt(1.0) == "1000 ms"
    assert bench.fmt(float("inf")) == ">2.5 s"
