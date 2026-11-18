"""Issue #207: run_cmd runs real processes in a worker thread and must kill them when its task is cancelled."""

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

import pytest

from harness import sandbox

REPO_ROOT = Path(__file__).resolve().parent.parent
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
# Prints a grandchild's pid and exits while that grandchild still holds stdout and stderr.
DETACHES_A_CHILD = [sys.executable, "-c",
                    "import subprocess, sys; print(subprocess.Popen("
                    "[sys.executable, '-c', 'import time; time.sleep(67)'], stdin=subprocess.DEVNULL, "
                    "stdout=sys.stdout.fileno(), stderr=sys.stderr.fileno()).pid)"]


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
    """A timeout reports the output so far (`early`, then output past the cap) and the exit code 124."""
    from harness.fileops import CAPTURE_CAPPED_NOTE
    code, out, err = asyncio.run(sandbox.run_cmd(
        [sys.executable, "-c",
         "import sys, time; print('early', flush=True); sys.stdout.write('A'*2_000_000); sys.stdout.flush(); "
         "time.sleep(60)"],
        timeout=2))
    assert code == 124
    assert out.startswith("early")
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


def test_drain_returns_only_once_a_process_cancelled_mid_spawn_is_gone(spawned, monkeypatch):
    """#525: the disk watchdog resets the container right after cancelling the exec client, so with `drain` the
    cancelled task must not finish while a client still being spawned could act."""
    real = sandbox._spawn

    def slow_spawn(*args, **kwargs):
        time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(sandbox, "_spawn", slow_spawn)

    async def scenario(drain):
        finished = asyncio.Event()
        task = asyncio.ensure_future(sandbox.run_cmd(SLEEPER, drain=drain, finished=finished))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return [proc.poll() for proc in spawned], finished.is_set()

    exits, finished = asyncio.run(scenario(30))
    assert exits and exits[0] is not None, "the cancelled task finished before its process was gone"
    assert finished
    spawned.clear()
    exits, finished = asyncio.run(scenario(0.05))    # gave up waiting while the spawn was still blocked
    assert not finished, "a drain that timed out must not report the process gone"
    assert _wait_until(lambda: bool(spawned) and spawned[0].poll() is not None)


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


def test_a_descriptor_number_reused_after_the_first_close_stays_open(tmp_path, monkeypatch):
    """#877: any close of a read end underneath its live stream frees the number while the stream still means to close
    it. This hands the number to another file at that moment, as another thread's open() would."""
    other = tmp_path / "other"
    other.touch()
    read_ends, read_end_fds, reused = [], set(), []
    real_spawn, real_close = sandbox._spawn, os.close

    def recording(*args, **kwargs):
        proc = real_spawn(*args, **kwargs)
        read_ends.extend([weakref.ref(proc.stdout), weakref.ref(proc.stderr)])
        read_end_fds.update([proc.stdout.fileno(), proc.stderr.fileno()])
        return proc

    def finished():
        gc.collect()
        return all(ref() is None or ref().closed for ref in read_ends)

    monkeypatch.setattr(sandbox, "_spawn", recording)
    with other.open("rb") as file:
        def close_then_reuse(fd):
            real_close(fd)
            if fd in read_end_fds:
                os.dup2(file.fileno(), fd, inheritable=False)
                reused.append(fd)

        monkeypatch.setattr(sandbox.os, "close", close_then_reuse)
        code, out, err = asyncio.run(sandbox.run_cmd(DETACHES_A_CHILD, timeout=30))
        try:
            assert code == 0, err
        finally:
            os.kill(int(out), signal.SIGTERM)
        assert _wait_until(finished, 30), "run_cmd's read ends were never closed"
        monkeypatch.setattr(sandbox.os, "close", real_close)
        closed = []
        for fd in reused:
            try:
                assert os.path.sameopenfile(fd, file.fileno())
                real_close(fd)
            except OSError:
                closed.append(fd)
    assert not closed, f"descriptor(s) {closed} now belonging to another file were closed underneath it"


def test_the_detached_child_path_finalizes_its_streams_without_an_error():
    """#877: a stream whose descriptor was closed underneath it reports `OSError: [Errno 9] Bad file descriptor` from
    its finalizer. Dev mode is what makes every supported Python report it, so this runs in a process of its own."""
    script = f"""
import asyncio, os, signal, threading, time
from harness import sandbox
code, out, err = asyncio.run(sandbox.run_cmd({DETACHES_A_CHILD!r}, timeout=30))
os.kill(int(out), signal.SIGTERM)
deadline = time.monotonic() + 30
while threading.active_count() > 1 and time.monotonic() < deadline:
    time.sleep(0.05)
print(code)
"""
    result = subprocess.run([sys.executable, "-X", "dev", "-c", script], cwd=REPO_ROOT, capture_output=True, text=True,
                            timeout=90, check=False)
    assert result.stdout.strip() == "0", result.stderr
    assert "Bad file descriptor" not in result.stderr
    assert "unclosed file <_io.BufferedReader" not in result.stderr


def test_run_cmd_closes_its_read_ends_when_the_command_exits(spawned):
    code, _, err = asyncio.run(sandbox.run_cmd([sys.executable, "-c", "print('done')"]))
    assert code == 0, err
    assert spawned[0].stdout.closed and spawned[0].stderr.closed


def test_wake_pumps_cancels_until_a_pump_between_reads_returns_and_closes_nothing(monkeypatch):
    """Windows' path: the first cancel finds nothing pending, as when the pump has not re-entered read() yet."""
    cancelled, closed = [], []
    second_cancel = threading.Event()

    def cancel(handle, overlapped):
        cancelled.append(handle)
        if len(cancelled) == 2:
            second_cancel.set()
        return 1

    monkeypatch.setattr(sandbox, "_CancelIoEx", cancel)
    monkeypatch.setattr(sandbox.os, "close", closed.append)
    pump = threading.Thread(target=second_cancel.wait, daemon=True)
    pump.start()
    sandbox._wake_pumps([pump], None, [99, None])
    assert not pump.is_alive()
    assert cancelled[:2] == [99, 99]
    assert closed == []


def test_closing_the_wake_pipe_wakes_a_blocked_read(monkeypatch):
    """POSIX's path, on a real pipe, so every platform's run covers it."""
    monkeypatch.setattr(sandbox, "_CancelIoEx", None)
    with sandbox._wake_pipe() as (wake_read, wake_write):
        woken = []
        pump = threading.Thread(target=lambda: woken.append(os.read(wake_read.fileno(), 1)), daemon=True)
        pump.start()
        sandbox._wake_pumps([pump], wake_write, [None, None])
        assert woken == [b""]
    assert wake_read.closed and wake_write.closed


def test_wake_pipe_is_only_made_where_a_pipe_can_be_polled(monkeypatch):
    monkeypatch.setattr(sandbox, "_CancelIoEx", lambda handle, overlapped: 1)
    with sandbox._wake_pipe() as ends:
        assert ends == (None, None)


def test_wakeable_read_returns_data_until_the_wake_pipe_fires(monkeypatch):
    """Scripted poll results, because Windows has no select.poll; the detached-child tests run the real one on POSIX."""
    class Poll:
        events = [[(5, 1)], [(6, 1)], [(5, 1), (6, 1)]]

        def register(self, fd, mask):
            assert fd in (5, 6) and mask == 1

        def poll(self):
            return self.events.pop(0)

    class End:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

    class Stream(End):
        def read1(self, size):
            return b"data"

    monkeypatch.setattr(sandbox.select, "poll", Poll, raising=False)
    monkeypatch.setattr(sandbox.select, "POLLIN", 1, raising=False)
    reader = sandbox._WakeableRead(Stream(5), End(6))
    assert reader.read1(65536) == b"data"
    assert reader.read1(65536) == b""
    assert reader.read1(65536) == b"", "a wake must end the read even while a grandchild keeps writing"


def test_read_handle_captures_the_native_handle(monkeypatch):
    class Stream:
        def fileno(self):
            return 3
    monkeypatch.setattr(sandbox, "msvcrt", type("M", (), {"get_osfhandle": staticmethod(lambda fd: 123)})())
    assert sandbox._read_handle(Stream()) == 123
    assert sandbox._read_handle(None) is None

    def closed_handle(fd):
        raise OSError(9, "Bad file descriptor")
    monkeypatch.setattr(sandbox, "msvcrt", type("M", (), {"get_osfhandle": staticmethod(closed_handle)})())
    assert sandbox._read_handle(Stream()) is None
