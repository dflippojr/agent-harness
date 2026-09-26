"""Issue #207: run_cmd runs real processes in a worker thread and must kill them when its task is cancelled."""

from __future__ import annotations

import asyncio
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
