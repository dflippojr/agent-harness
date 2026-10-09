"""Issue #429: cache volumes, per-project setup on container creation, and the environment-recreated notice."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness import sandbox
from harness.config import SandboxConfig, _project_from_spec


class FakeDocker:
    def __init__(self):
        self.state: str | None = None
        self.calls: list[list[str]] = []
        self.exec_code = 0

    async def __call__(self, args, timeout=60, input_=None, env=None):
        self.calls.append(list(args))
        verb = args[1]
        if verb == "inspect":
            return (0, self.state, "") if self.state else (1, "", "")
        if verb == "run":
            self.state = "running"
        elif verb == "rm":
            self.state = None
        elif verb == "stop":
            self.state = "exited"
        elif verb == "start":
            self.state = "running"
        elif verb == "exec":
            if "pip install" in args[-1]:
                return self.exec_code, "", "boom" if self.exec_code else ""
            return 0, "ok", ""
        return 0, "", ""

    def verbs(self):
        return [c[1] for c in self.calls]


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sandbox, "run_cmd", fake)
    return fake


def make(**kw):
    return sandbox.Sandbox("s1", Path("."), SandboxConfig(), **kw)


def test_cache_volumes_mounted_per_project(docker):
    asyncio.run(make(project="web").exec("true"))
    run = next(c for c in docker.calls if c[1] == "run")
    assert "type=volume,source=harness-cache-pip-web,target=/root/.cache/pip" in run
    assert "type=volume,source=harness-cache-npm-web,target=/root/.npm" in run


def test_cache_volumes_distinct_per_principal():
    """#529: a member's slug can equal the owner's; the volumes differ and the owner's names are unchanged."""
    def volumes(**kw):
        return [a for a in make(project="web", **kw)._cache_mounts() if a != "--mount"]

    owner = volumes()
    assert "type=volume,source=harness-cache-pip-web,target=/root/.cache/pip" in owner
    assert volumes(user_id="owner") == owner
    alice, bob = volumes(user_id="u-alice"), volumes(user_id="u-bob")
    assert len({tuple(owner), tuple(alice), tuple(bob)}) == 3
    assert all("source=harness-cache-" in v and "-web-u" in v for v in alice + bob)
    assert "u-alice" not in " ".join(alice)


def test_no_cache_volumes_without_project(docker):
    asyncio.run(make().exec("true"))
    run = next(c for c in docker.calls if c[1] == "run")
    assert not any("harness-cache" in a for a in run)


def test_first_creation_has_no_notice_but_runs_setup(docker):
    events = []
    sb = make(project="web", setup="pip install -e .", on_event=lambda t, d: events.append((t, d)))
    _, out = asyncio.run(sb.exec("true"))
    assert out == "ok"
    assert events == [("sandbox_setup", {"command": "pip install -e .", "ok": True, "detail": ""})]


def test_recreation_notice_once_and_setup_reruns(docker):
    events = []

    async def go():
        sb = make(project="web", setup="pip install -e .", known=True, on_event=lambda t, d: events.append(d))
        _, first = await sb.exec("true")
        assert "environment recreated" in first and "setup command was re-run: ok" in first
        _, second = await sb.exec("true")
        assert second == "ok"
        docker.state = None  # docker rm -f
        _, third = await sb.exec("true")
        assert "environment recreated" in third
    asyncio.run(go())
    assert len(events) == 2


def test_setup_not_run_on_plain_start_or_running(docker):
    async def go():
        sb = make(project="web", setup="pip install -e .")
        await sb.exec("true")
        docker.state = "exited"
        _, out = await sb.exec("true")
        assert out == "ok"
        await sb.exec("true")
    asyncio.run(go())
    assert sum("pip install" in c[-1] for c in docker.calls if c[1] == "exec") == 1


def test_setup_failure_is_an_event_and_in_the_notice(docker):
    docker.exec_code = 3
    events = []
    sb = make(setup="pip install -e .", known=True, on_event=lambda t, d: events.append(d))
    code, out = asyncio.run(sb.exec("true"))
    assert code == 0 and "failed (exit 3: boom)" in out
    assert events[0]["ok"] is False and "boom" in events[0]["detail"]
    assert docker.verbs().count("network") >= 2  # egress attached and detached around setup


def test_recreation_without_setup_says_so(docker):
    _, out = asyncio.run(make(known=True).exec("true"))
    assert "No project setup command" in out


def test_project_setup_field():
    assert _project_from_spec("p", {"setup": "make deps"}).setup == "make deps"
    assert _project_from_spec("p", {}).setup == ""


def test_a_restarted_container_starts_without_egress(docker):
    docker.state = "exited"
    assert asyncio.run(make().ensure_running()) == "started"
    assert ["docker", "network", "disconnect", "-f", SandboxConfig().egress_network, "harness-s1"] in docker.calls


def test_restarting_after_an_interrupted_command_detaches_egress(docker):
    docker.state = "running"
    asyncio.run(make().restart())
    verbs = docker.verbs()
    assert verbs.index("restart") < verbs.index("network")
    assert ["docker", "network", "disconnect", "-f", SandboxConfig().egress_network, "harness-s1"] in docker.calls
