"""Issue #243: _run_clone must never wait on its pipes without a bound.

A killed clone process is not guaranteed to close its stdout/stderr pipes: git can spawn a helper (seen in
practice as git-upload-pack for a local clone) that inherits the pipe handle, and killing only the parent
leaves that handle open, so communicate() never sees EOF. That hung a real hosted CI run for the full 40-minute
job timeout. These tests use a fake Popen whose pipes never close, standing in for that orphaned-handle case,
and check that _run_clone always returns in bounded time regardless.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from harness import clone


class _NeverClosingProc:
    """A killed process whose pipes never reach EOF: every communicate() call times out forever, even after
    kill() reports it dead. Standing in for an orphaned helper process holding the pipe open (#243)."""

    def __init__(self):
        self.pid = -1
        self.returncode = None
        self._killed = False

    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="stub", timeout=timeout)

    def poll(self):
        return 0 if self._killed else None

    def kill(self):
        self._killed = True
        self.returncode = -9


@pytest.fixture
def never_closing(monkeypatch):
    """_run_clone spawns _NeverClosingProc, and _stop_clone only calls kill() -- no taskkill with a fake pid."""
    proc = _NeverClosingProc()
    monkeypatch.setattr(clone.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(clone, "_stop_clone", lambda p: p.kill())
    return proc


def test_a_clone_that_never_yields_output_times_out_in_bounded_time(never_closing, tmp_path):
    dest = tmp_path / "dest"
    started = time.monotonic()
    with pytest.raises(clone.GitError, match="clone timed out"):
        clone._run_clone(["git", "clone"], dest, timeout=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < clone._DRAIN_GRACE_SECONDS + 5, f"took {elapsed:.1f}s -- communicate() is being awaited unbounded"
    assert never_closing.poll() is not None, "the process must have been stopped, not left running"


def test_a_quota_kill_that_never_yields_output_still_reports_quota_not_a_timeout(never_closing, tmp_path):
    dest = tmp_path / "dest"
    over = clone.threading.Event()
    over.set()  # as _watch_size would, the instant dest exceeds max_bytes
    started = time.monotonic()
    stdout, stderr, timed_out = clone._wait_for_clone(never_closing, over, timeout=600)
    elapsed = time.monotonic() - started
    assert elapsed < clone._DRAIN_GRACE_SECONDS + 5, f"took {elapsed:.1f}s -- a quota kill must not wait for the full timeout"
    assert not timed_out, "a quota kill must not be reported as a timeout even when no output could be captured"
    assert (stdout, stderr) == ("", "")


def test_a_process_that_finishes_normally_returns_promptly_without_waiting_the_full_timeout():
    class _Immediate:
        def communicate(self, timeout=None):
            return "out", "err"

    started = time.monotonic()
    stdout, stderr, timed_out = clone._wait_for_clone(_Immediate(), clone.threading.Event(), timeout=600)
    assert time.monotonic() - started < 2
    assert (stdout, stderr, timed_out) == ("out", "err", False)
