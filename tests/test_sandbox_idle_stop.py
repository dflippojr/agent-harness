"""Issue #428: an idle sandbox container is stopped and the next exec restarts it, with a notice."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness import sandbox
from harness.config import SandboxConfig


class FakeDocker:
    def __init__(self):
        self.state = "running"
        self.calls: list[str] = []

    async def __call__(self, args, timeout=60, input_=None, env=None):
        verb = args[1]
        self.calls.append(verb)
        if verb == "inspect":
            return (0, self.state, "") if self.state else (1, "", "")
        if verb == "stop":
            self.state = "exited"
        elif verb == "start":
            self.state = "running"
        elif verb == "exec":
            await asyncio.sleep(getattr(self, "exec_delay", 0))
            return 0, "ok", ""
        return 0, "", ""


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sandbox, "run_cmd", fake)
    return fake


def make(idle=0.05):
    return sandbox.Sandbox("s1", Path("."), SandboxConfig(idle_stop_seconds=idle))


def test_idle_timer_stops_then_exec_restarts_with_notice(docker):
    async def go():
        sb = make()
        assert await sb.exec("true") == (0, "ok")
        await asyncio.sleep(0.3)
        assert docker.state == "exited"
        code, out = await sb.exec("true")
        assert docker.state == "running" and "restarted after an idle stop" in out
        _, again = await sb.exec("true")
        assert again == "ok"  # notice only once
        sb._cancel_idle_timer()
    asyncio.run(go())


def test_off_by_default(docker):
    async def go():
        sb = make(idle=0)
        await sb.exec("true")
        assert await sb.stop_if_idle() is False
        assert sb._idle_timer is None and "stop" not in docker.calls
    asyncio.run(go())


def test_no_stop_while_command_runs(docker):
    async def go():
        sb = make(idle=0.05)
        docker.exec_delay = 0.3
        task = asyncio.create_task(sb.exec("sleep"))
        await asyncio.sleep(0.15)
        assert await sb.stop_if_idle() is False
        await task
        assert "stop" not in docker.calls
        sb._cancel_idle_timer()
    asyncio.run(go())


def test_stop_if_idle_for_approval_wait(docker):
    async def go():
        sb = make(idle=600)
        await sb.exec("true")
        assert await sb.stop_if_idle() is True
        assert docker.state == "exited"
        sb._cancel_idle_timer()
    asyncio.run(go())
