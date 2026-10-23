"""Issue #671: network commands leave the sandbox only through the sandbox egress proxy, never `harness-egress`.

A fake `docker` keeps the network layout (members, internal, ICC) so the tests can check who can reach whom."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from harness import doctor, maintenance, sandbox
from harness.config import SandboxConfig, sandbox_for_project

ROOT = Path(__file__).resolve().parents[1]
PROXY = "harness-egress-sandbox"
PROXY_ENV = ["-e", "HTTP_PROXY=http://harness-egress-sandbox:8888", "-e", "HTTPS_PROXY=http://harness-egress-sandbox:8888",
             "-e", "http_proxy=http://harness-egress-sandbox:8888", "-e", "https_proxy=http://harness-egress-sandbox:8888"]


class NetworkDocker:
    """Session containers, the proxy container and the networks between them."""

    def __init__(self):
        self.containers: dict[str, str] = {}            # name -> state
        self.proxy = "running"                          # or "exited", or None for no container
        self.networks: dict[str, dict] = {}             # name -> {"internal", "icc", "members"}
        self.calls: list[list[str]] = []
        self.execs: list[dict] = []                     # each exec: its env options and the networks it could use
        self.fail: dict[str, str] = {}                  # "network create" -> stderr, to make a step fail
        self.barrier: asyncio.Event | None = None       # exec waits here, so two commands overlap
        self.in_exec = 0

    def attachments(self, container: str) -> set[str]:
        return {name for name, net in self.networks.items() if container in net["members"]}

    async def __call__(self, args, timeout=60, input_=None, env=None, **kw):
        self.calls.append(list(args))
        # The direct route reaches the host, the LAN and the tailnet. No session container may join it.
        assert not (args[1:4] == ["network", "connect", "harness-egress"] and args[-1].startswith("harness-")
                    and args[-1] != PROXY), args
        verb = args[1]
        if verb == "network":
            return self._network(args[2], args[3:])
        if verb == "inspect":
            name = args[-1]
            if name == PROXY:
                return (0, "true" if self.proxy == "running" else "false", "") if self.proxy else (1, "", "no such")
            state = self.containers.get(name)
            return (0, f"{state} owner", "") if state else (1, "", "no such")
        name = next((a for a in args if a.startswith("harness-s")), "")
        if verb == "run":
            self.containers[name] = "running"
            self.networks[args[args.index("--network") + 1]]["members"].add(name)
        elif verb == "start":
            self.containers[name] = "running"
        elif verb in ("restart",):
            pass
        elif verb == "rm":
            self.containers.pop(name, None)
            for net in self.networks.values():
                net["members"].discard(name)
        elif verb == "exec":
            env_opts = args[2:args.index("-w")]
            self.execs.append({"container": name, "env": env_opts, "command": args[-1],
                               "networks": self.attachments(name)})
            if self.barrier is not None:
                self.in_exec += 1
                if self.in_exec == 2:
                    self.barrier.set()
                await asyncio.wait_for(self.barrier.wait(), 5)
            return 0, "ok", ""
        return 0, "", ""

    def _network(self, verb: str, rest: list[str]):
        failure = self.fail.get(f"network {verb}")
        if failure:
            return 1, "", failure
        if verb == "ls":
            return 0, "\n".join(self.networks), ""
        if verb == "create":
            name = rest[-1]
            if name in self.networks:
                return 1, "", f"Error response from daemon: network with name {name} already exists"
            icc = "com.docker.network.bridge.enable_icc=false" not in rest
            self.networks[name] = {"internal": "--internal" in rest, "icc": icc, "members": set()}
            return 0, name, ""
        if verb == "connect":
            net, member = rest
            if net not in self.networks:
                return 1, "", f"network {net} not found"
            if member in self.networks[net]["members"]:
                return 1, "", f"endpoint with name {member} already exists in network {net}"
            self.networks[net]["members"].add(member)
            return 0, "", ""
        if verb == "disconnect":
            net, member = rest[-2:]
            if net in self.networks:
                self.networks[net]["members"].discard(member)
            return 0, "", ""
        if verb == "rm":
            net = rest[-1]
            if net in self.networks and self.networks[net]["members"]:
                return 1, "", "network has active endpoints"
            self.networks.pop(net, None)
            return 0, "", ""
        raise AssertionError(verb)


@pytest.fixture
def docker(monkeypatch):
    fake = NetworkDocker()
    fake.networks["harness-egress"] = {"internal": False, "icc": False, "members": {PROXY}}
    fake.networks["harness-sandbox-egress"] = {"internal": True, "icc": False, "members": {PROXY}}
    fake.networks["harness-sandbox"] = {"internal": True, "icc": False, "members": set()}
    monkeypatch.setattr(sandbox, "run_cmd", fake)
    return fake


def make(session: str = "s1", **kw) -> sandbox.Sandbox:
    return sandbox.Sandbox(session, Path("."), SandboxConfig(), **kw)


def egress_calls(calls) -> list[list[str]]:
    return [c for c in calls if "harness-egress" in c and c[1:3] == ["network", "connect"]]


def test_a_network_command_runs_through_its_own_proxy_network(docker):
    code, _ = asyncio.run(make().exec("pip download six", network=True))
    assert code == 0
    run = docker.execs[-1]
    assert run["env"] == PROXY_ENV
    assert run["networks"] == {"harness-sandbox", "harness-sandbox-egress-s1"}
    assert "harness-egress" not in run["networks"]
    create = next(c for c in docker.calls if c[1:3] == ["network", "create"] and c[-1] == "harness-sandbox-egress-s1")
    assert "--internal" in create
    assert not egress_calls(docker.calls)
    # Afterwards the session is back on its internal network only, and its proxy network is gone.
    assert docker.attachments("harness-s1") == {"harness-sandbox"}
    assert "harness-sandbox-egress-s1" not in docker.networks
    assert docker.attachments(PROXY) == {"harness-egress", "harness-sandbox-egress"}


def test_an_offline_command_gets_no_proxy_and_no_network(docker):
    asyncio.run(make().exec("pytest"))
    assert docker.execs[-1]["env"] == []
    assert docker.execs[-1]["networks"] == {"harness-sandbox"}
    assert not [c for c in docker.calls if c[1:3] == ["network", "connect"]]


def test_the_setup_command_runs_through_the_proxy(docker):
    events = []
    sb = make(project="web", setup="pip install -e .", on_event=lambda t, d: events.append(d))
    asyncio.run(sb.exec("pytest"))
    setup, command = docker.execs
    assert setup["command"] == "pip install -e ." and setup["env"] == PROXY_ENV
    assert setup["networks"] == {"harness-sandbox", "harness-sandbox-egress-s1"}
    assert command["env"] == [] and command["networks"] == {"harness-sandbox"}
    assert events[0]["ok"] is True
    assert not egress_calls(docker.calls)


def leave_attached(docker, session="s1"):
    """What a daemon that died mid-command leaves: the old direct route and the proxy network both attached."""
    container = f"harness-{session}"
    docker.networks["harness-egress"]["members"].add(container)
    docker.networks[f"harness-sandbox-egress-{session}"] = {"internal": True, "icc": True,
                                                           "members": {container, PROXY}}


@pytest.mark.parametrize("path", ["start", "restart", "first use", "after command"])
def test_every_detach_path_drops_the_proxy_network_and_the_old_route(docker, path):
    docker.networks["harness-sandbox"]["members"].add("harness-s1")
    docker.containers["harness-s1"] = "exited" if path == "start" else "running"
    leave_attached(docker)
    sb = make()
    if path == "start":
        assert asyncio.run(sb.ensure_running()) == "started"
    elif path == "restart":
        asyncio.run(sb.restart())
    elif path == "first use":
        assert asyncio.run(sb.ensure_running()) == "running"
    else:
        sb._egress_checked = True   # skip the first-use cleanup: the command's own detach must do it
        asyncio.run(sb.exec("curl -sI https://pkg.example", network=True))
    assert docker.attachments("harness-s1") == {"harness-sandbox"}
    assert "harness-sandbox-egress-s1" not in docker.networks
    assert docker.attachments(PROXY) == {"harness-egress", "harness-sandbox-egress"}


def test_first_use_cleanup_runs_once(docker):
    docker.networks["harness-sandbox"]["members"].add("harness-s1")
    docker.containers["harness-s1"] = "running"
    sb = make()

    async def go():
        await sb.exec("true")
        await sb.exec("true")
    asyncio.run(go())
    assert sum(c[1:3] == ["network", "rm"] for c in docker.calls) == 1


def test_removing_a_session_removes_its_proxy_network(docker):
    sb = make()

    async def go():
        await sb.exec("true")
        leave_attached(docker)
        await sb.remove()
    asyncio.run(go())
    assert "harness-sandbox-egress-s1" not in docker.networks
    assert docker.attachments(PROXY) == {"harness-egress", "harness-sandbox-egress"}


def test_a_stale_proxy_network_is_replaced(docker):
    sb = make()
    asyncio.run(sb.ensure_running())
    leave_attached(docker)
    sb._egress_checked = True
    asyncio.run(sb.exec("curl https://pkg.example", network=True))
    assert docker.execs[-1]["networks"] == {"harness-sandbox", "harness-sandbox-egress-s1"}
    assert "harness-egress" not in docker.execs[-1]["networks"]
    creates = [c for c in docker.calls if c[1:3] == ["network", "create"] and c[-1] == "harness-sandbox-egress-s1"]
    assert len(creates) == 2


def test_two_sessions_with_network_at_once_cannot_reach_each_other(docker):
    """Both commands run at the same moment. No network they sit on holds the other session's container, except
    harness-sandbox, where ICC is off."""
    async def go():
        docker.barrier = asyncio.Event()
        await asyncio.gather(make("s1").exec("curl https://a.example", network=True),
                             make("s2").exec("curl https://b.example", network=True))
    asyncio.run(go())
    snap = {run["container"]: run["networks"] for run in docker.execs}
    assert snap["harness-s1"] == {"harness-sandbox", "harness-sandbox-egress-s1"}
    assert snap["harness-s2"] == {"harness-sandbox", "harness-sandbox-egress-s2"}
    shared = snap["harness-s1"] & snap["harness-s2"]
    assert shared == {"harness-sandbox"}
    assert docker.networks["harness-sandbox"]["internal"] and not docker.networks["harness-sandbox"]["icc"]


def test_two_sessions_never_share_a_proxy_network_name():
    cfg = SandboxConfig()
    assert sandbox.egress_network(cfg, "s1") != sandbox.egress_network(cfg, "s2")
    assert sandbox.egress_network(cfg, "s1") != cfg.proxy_network


@pytest.mark.parametrize("proxy", ["exited", None])
def test_a_missing_proxy_fails_closed(docker, proxy):
    docker.proxy = proxy
    sb = make()
    with pytest.raises(sandbox.SandboxUnavailable) as e:
        asyncio.run(sb.exec("pip install six", network=True))
    assert "harness-egress-sandbox" in str(e.value)
    assert "docker compose -f ops/egress/compose.yaml up -d --build sandbox" in str(e.value)
    assert docker.execs == []
    assert not [c for c in docker.calls if c[1:3] == ["network", "connect"]]
    assert docker.attachments("harness-s1") == {"harness-sandbox"}


def test_a_missing_proxy_network_step_fails_closed(docker):
    docker.fail["network connect"] = "Error response from daemon: network not found"
    with pytest.raises(sandbox.SandboxUnavailable, match="could not enable network: .*not found"):
        asyncio.run(make().exec("pip install six", network=True))
    assert docker.execs == []
    assert not egress_calls(docker.calls)


def test_a_failed_network_create_fails_closed(docker):
    docker.fail["network create"] = "could not find an available, non-overlapping IPv4 address pool"
    with pytest.raises(sandbox.SandboxUnavailable, match="non-overlapping"):
        asyncio.run(make().exec("pip install six", network=True))
    assert docker.execs == []


def test_a_missing_proxy_fails_the_setup_command_without_running_it(docker):
    docker.proxy = None
    events = []
    asyncio.run(make(setup="pip install -e .", on_event=lambda t, d: events.append(d)).exec("pytest"))
    assert [run["command"] for run in docker.execs] == ["pytest"]
    assert events[0]["ok"] is False and "ops/egress/compose.yaml" in events[0]["detail"]


def test_the_internal_sandbox_network_is_unchanged(docker):
    docker.networks.clear()
    asyncio.run(sandbox.ensure_networks(SandboxConfig()))
    create = next(c for c in docker.calls if c[-1] == "harness-sandbox")
    assert create[:4] == ["docker", "network", "create", "--internal"]
    assert "com.docker.network.bridge.enable_icc=false" in create
    assert docker.networks["harness-sandbox"]["internal"] and not docker.networks["harness-sandbox"]["icc"]


def test_compose_keeps_internal_networks_and_adds_the_sandbox_proxy():
    compose = yaml.safe_load((ROOT / "ops/egress/compose.yaml").read_text(encoding="utf-8"))
    networks = compose["networks"]
    for backend in ("claude", "codex", "cursor"):
        assert networks[f"harness-cli-{backend}"]["internal"] is True
    assert networks["harness-sandbox-egress"]["internal"] is True
    assert networks["harness-sandbox-egress"]["driver_opts"]["com.docker.network.bridge.enable_icc"] == "false"
    proxy = compose["services"]["sandbox"]
    assert proxy["container_name"] == PROXY == sandbox.proxy_container(SandboxConfig())
    assert set(proxy["networks"]) == {"harness-sandbox-egress", "harness-egress"}
    assert "ports" not in proxy
    assert proxy["read_only"] is True
    assert proxy["volumes"] == ["./sandbox-proxy.conf:/etc/egress/sandbox-proxy.conf:ro"]
    dockerfile = (ROOT / "ops/egress" / proxy["build"]["context"] / proxy["build"]["dockerfile"]).resolve()
    assert dockerfile == ROOT / "ops/egress/sandbox.Dockerfile"
    assert (ROOT / "ops/egress" / proxy["build"]["context"] / "egress_proxy.py").resolve() == \
        ROOT / "harness/egress_proxy.py"
    for name in ("claude", "codex", "cursor", "reviewer"):   # the hosted-CLI proxies are untouched
        assert compose["services"][name]["image"] == "agent-harness-egress:1"


def test_a_project_cannot_override_the_egress_settings():
    base = SandboxConfig()
    merged = sandbox_for_project(base, {"memory": "4g", "egress_network": "bridge", "proxy": "http://elsewhere:1",
                                        "proxy_network": "bridge"})
    assert merged.memory == "4g"
    assert (merged.egress_network, merged.proxy, merged.proxy_network) == \
        (base.egress_network, base.proxy, base.proxy_network)
    assert sandbox_for_project(base, {}) is base


def test_cleanup_removes_a_left_over_proxy_network(monkeypatch):
    calls = []

    async def docker(args, *rest, **kw):
        calls.append(list(args))
        if args[1] == "ps":
            return 0, '{"Names": "harness-gone1", "State": "exited", "Labels": "agent-harness.session=gone1"}', ""
        return 0, "", ""
    monkeypatch.setattr(maintenance, "run_cmd", docker)
    service = SimpleNamespace(
        cfg=SimpleNamespace(fake_model=None, cleanup=SimpleNamespace(container_idle_hours=24), sandbox=SandboxConfig()),
        db=SimpleNamespace(get_session=lambda sid: None), runner=SimpleNamespace(_sandboxes={}))
    report = {"containers_removed": []}
    asyncio.run(maintenance.Maintenance._containers(service, time.time(), report))
    assert calls[1:] == [["docker", "rm", "-f", "harness-gone1"],
                         ["docker", "network", "disconnect", "-f", "harness-sandbox-egress-gone1", PROXY],
                         ["docker", "network", "rm", "harness-sandbox-egress-gone1"]]
    assert report["containers_removed"] == ["harness-gone1"]


@pytest.mark.parametrize("answer, ok", [((0, "running"), True), ((0, "exited"), False), ((1, "No such object"), False)])
def test_doctor_reports_the_sandbox_proxy(monkeypatch, capsys, answer, ok):
    seen = []

    def run(args, timeout=20):
        seen.append(args)
        return answer
    monkeypatch.setattr(doctor, "run", run)
    report = doctor.Report()
    doctor.check_sandbox_proxy(report, SimpleNamespace(sandbox=SandboxConfig()))
    out = capsys.readouterr().out
    assert seen == [["docker", "inspect", "--format", "{{.State.Status}}", PROXY]]
    assert "Sandbox egress" in out and (report.failed == 0) is ok
    if not ok:
        assert "docker compose -f ops/egress/compose.yaml up -d --build sandbox" in out


def test_doctor_checks_the_sandbox_proxy_beside_the_provider_proxies(monkeypatch):
    checked = []
    monkeypatch.setattr(doctor, "run", lambda args, timeout=20: (0, "running"))
    monkeypatch.setattr(doctor, "check_sandbox_proxy", lambda r, cfg: checked.append("sandbox"))
    monkeypatch.setattr(doctor, "check_provider_containers", lambda r, cfg: checked.append("providers"))
    cfg = SimpleNamespace(sandbox=SandboxConfig(), profile="service")
    doctor.check_docker(doctor.Report(), cfg)
    assert checked == ["sandbox", "providers"]
