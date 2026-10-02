"""Issue #207: run_cmd runs real processes in a worker thread and must kill them when its task is cancelled."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time

import pytest

from harness import sandbox

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def spawned(monkeypatch):
    """Every process run_cmd starts, so a test can check that none outlives its task."""
    procs = []
    real = sandbox._spawn

    def recording(*args, **kwargs):
        proc = real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(sandbox, "_spawn", recording)
    yield procs
    for proc in procs:  # never leave a sleeper behind if an assertion failed
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _wait_until(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_run_cmd_returns_output_and_passes_input():
    code, out, err = asyncio.run(sandbox.run_cmd(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper()); print('warn', file=sys.stderr)"],
        input_="abc"))
    assert code == 0
    assert out.strip() == "ABC"
    assert err.strip() == "warn"


def test_run_cmd_reports_a_timeout_with_the_output_so_far():
    code, out, err = asyncio.run(sandbox.run_cmd(
        [sys.executable, "-c", "import sys, time; print('early', flush=True); time.sleep(60)"], timeout=1))
    assert code == 124
    assert "early" in out
    assert "[timed out after 1s]" in err


def test_run_cmd_caps_each_stream_at_one_million_characters():
    from harness.fileops import CAPTURE_CAPPED_NOTE, OUTPUT_CAP
    code, out, err = asyncio.run(sandbox.run_cmd(
        [sys.executable, "-c",
         "import sys; sys.stdout.write('A'*2_000_000); sys.stderr.write('B'*2_000_000)"],
        timeout=30))
    assert code == 0
    assert len(out) < OUTPUT_CAP + 200
    assert len(err) < OUTPUT_CAP + 200
    assert out.startswith("A" * 100) and "A" * 100 in out[-300:]
    assert err.startswith("B" * 100) and "B" * 100 in err[-300:]
    assert "... [output cut] ..." in out and "... [output cut] ..." in err
    assert CAPTURE_CAPPED_NOTE in out and CAPTURE_CAPPED_NOTE in err


def test_run_cmd_timeout_stays_124_when_output_exceeds_the_cap():
    from harness.fileops import CAPTURE_CAPPED_NOTE
    code, out, err = asyncio.run(sandbox.run_cmd(
        [sys.executable, "-c",
         "import sys, time; sys.stdout.write('A'*2_000_000); sys.stdout.flush(); time.sleep(60)"],
        timeout=2))
    assert code == 124
    assert CAPTURE_CAPPED_NOTE in out
    assert "[timed out after 2s]" in err
    assert len(out) < 1_200_000


def test_cancelling_a_running_command_kills_the_process(spawned):
    async def scenario():
        task = asyncio.ensure_future(sandbox.run_cmd(SLEEPER))
        for _ in range(200):
            if spawned:
                break
            await asyncio.sleep(0.05)
        assert spawned, "the command never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _wait_until(lambda: spawned[0].poll() is not None), "the cancelled command is still running"


def test_cancelling_while_the_process_is_still_being_spawned_still_kills_it(spawned, monkeypatch):
    real = sandbox._spawn
    slow_start = {"seconds": 0.5}

    def slow_spawn(*args, **kwargs):
        time.sleep(slow_start["seconds"])  # the cancel lands while Popen has not returned yet
        return real(*args, **kwargs)

    monkeypatch.setattr(sandbox, "_spawn", slow_spawn)

    async def scenario():
        task = asyncio.ensure_future(sandbox.run_cmd(SLEEPER))
        await asyncio.sleep(0.15)
        assert not spawned, "the cancel was meant to land before the process exists"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _wait_until(lambda: bool(spawned)), "the worker thread never finished spawning"
    assert _wait_until(lambda: spawned[0].poll() is not None), "a command cancelled mid-spawn was left running"


def test_run_cmd_returns_when_detached_child_holds_the_pipe(tmp_path, monkeypatch):
    """Parent exits while a grandchild still holds stdout; pumps must not wait out the join timeout."""
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("sh is required to background a child that keeps the pipe open")
    finished = []
    pump = sandbox._pump_stream

    def watched_pump(stream, cap):
        pump(stream, cap)
        finished.append(stream)
    monkeypatch.setattr(sandbox, "_pump_stream", watched_pump)
    pidfile = tmp_path / "child.pid"
    posix = str(pidfile).replace("\\", "/")
    inner = f"sleep 67 & echo $! > '{posix}'; echo parent-done"
    t0 = time.monotonic()
    code, out, err = asyncio.run(sandbox.run_cmd([sh, "-c", inner], timeout=30))
    elapsed = time.monotonic() - t0
    pid = None
    try:
        assert pidfile.exists(), "background child never wrote its pid"
        pid = int(pidfile.read_text().strip())
        assert code == 0, err
        assert "parent-done" in out, f"parent's output was lost: {out!r}"
        # The mechanism: both pumps returned before run_cmd did, though the grandchild still holds the pipe. Without
        # the unblock they stay in read() and run_cmd gives up on them after two 10 s joins.
        assert len(finished) == 2, f"pump join leaked: {2 - len(finished)} pump(s) still reading"
        assert elapsed < 15, f"run_cmd took {elapsed:.2f}s, as long as a leaked pump join"
    finally:
        if pid:
            subprocess.run([sh, "-c", f"kill {pid} 2>/dev/null; kill -9 {pid} 2>/dev/null"],
                           timeout=5, check=False)


def test_unblock_read_cancels_pending_io_then_closes(monkeypatch):
    cancelled, closed = [], []
    monkeypatch.setattr(sandbox, "_CancelIoEx", lambda handle, overlapped: cancelled.append(handle) or 1)
    monkeypatch.setattr(sandbox.os, "close", lambda fd: closed.append(fd))
    sandbox._unblock_read(7, 99)
    assert cancelled == [99] and closed == [7]


def test_read_end_captures_fd_and_native_handle(monkeypatch):
    class Stream:
        def fileno(self):
            return 3
    monkeypatch.setattr(sandbox, "msvcrt", type("M", (), {"get_osfhandle": staticmethod(lambda fd: 123)})())
    assert sandbox._read_end(Stream()) == (3, 123)
    assert sandbox._read_end(None) == (None, None)
