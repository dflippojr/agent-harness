"""Stage (e) of #334: the Server image (Dockerfile at the repository root).

These run everywhere and need no Docker:

- The Dockerfile's final stage runs the service profile as an unprivileged account, keeps config and data in one
  volume, bakes in no secret and publishes no port.
- The build context is exactly what the wheels need. The build stage, run on such a context, gives the same wheels as
  a checkout.
- `harness.container` writes the first config and checks health.
- `scripts/server_image_smoke.py` reaches the right verdicts against a fake Docker.

The real build and run are the smoke script against a built image: the CI image job and the PR's Proof table.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib
import yaml

from harness import container

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "packaging"))
import agent_harness_build as build  # noqa: E402
import server_image_smoke as smoke  # noqa: E402

VOLUME = "/var/lib/agent-harness"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _instructions() -> list[tuple[str, str]]:
    """The Dockerfile as (INSTRUCTION, arguments), continuation lines joined and comments dropped."""
    lines, current = [], ""
    for raw in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if not current and (not raw.strip() or raw.lstrip().startswith("#")):
            continue
        current += raw.rstrip("\\").strip() + " "
        if not raw.rstrip().endswith("\\"):
            lines.append(current.strip())
            current = ""
    return [(line.split(None, 1)[0].upper(), line.split(None, 1)[1] if " " in line else "") for line in lines]


def _stages() -> list[list[tuple[str, str]]]:
    stages: list[list[tuple[str, str]]] = []
    for instruction in _instructions():
        if instruction[0] == "FROM":
            stages.append([])
        stages[-1].append(instruction)
    return stages


# ------------------------------------------------------------------------------------------------- the Dockerfile

def test_the_image_runs_unprivileged_with_its_state_in_one_volume():
    final = _stages()[-1]
    args = {name: [a for n, a in final if n == name] for name, _ in final}
    assert args["USER"] == ["10001:10001"]
    assert args["VOLUME"] == [f'["{VOLUME}"]']
    env = dict(item.split("=", 1) for item in shlex.split(" ".join(args["ENV"])))
    assert env["HARNESS_CONFIG_DIR"] == f"{VOLUME}/config" and env["HARNESS_DATA_DIR"] == f"{VOLUME}/data"
    assert f"{env['XDG_DATA_HOME']}/agent-harness" == VOLUME  # the install root, and every default under it
    assert json.loads(args["ENTRYPOINT"][0]) == ["python", "-m", "harness.container"]
    assert args["HEALTHCHECK"][0].endswith('CMD ["python", "-m", "harness.container", "--health"]')
    # The account owns the volume, and nothing runs as root after USER.
    run = " ".join(args["RUN"])
    assert "useradd --system --uid 10001" in run and f"chown 10001:10001 {VOLUME}" in run
    assert [name for name, _ in final].index("USER") > max(i for i, (name, _) in enumerate(final) if name == "RUN")


def test_the_image_bakes_in_no_secret_and_publishes_no_port():
    stages = _stages()
    final = stages[-1]
    assert not [a for n, a in final if n == "EXPOSE"]  # the daemon listens on 127.0.0.1 unless configured otherwise
    # The final stage gets only the built wheels; source, config and data never enter it.
    assert [a for n, a in final if n in ("COPY", "ADD")] == ["--from=wheels /wheels /tmp/wheels"]
    secretish = ("TOKEN", "SECRET", "PASSWORD", "KEY")
    for stage in stages:
        for name, args in stage:
            if name in ("ARG", "ENV"):
                assert not any(word in args.upper() for word in secretish), args
    assert [a.split()[0] for n, a in _instructions() if n == "FROM"] == ["python:3.12-slim", "python:3.12-slim"]
    assert PYPROJECT["project"]["requires-python"] == ">=3.12"


def test_the_build_stage_makes_the_wheels_with_the_in_tree_backend():
    wheels = _stages()[0]
    assert wheels[0] == ("FROM", "python:3.12-slim AS wheels")
    assert ("RUN", "python scripts/packaging/agent_harness_build.py --out /wheels") in wheels


# ------------------------------------------------------------------------------------------------ the build context

def _context_entries() -> tuple[list[str], list[str]]:
    lines = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    return lines, [line[1:].rstrip("/") for line in lines if line.startswith("!")]


def _build_inputs() -> set[str]:
    project, settings = PYPROJECT["project"], PYPROJECT["tool"]["agent-harness"]
    return {"pyproject.toml", project["readme"], *project.get("license-files", []), *settings["build"]["sdist"],
            *settings["build"]["modules"].values(), *settings["build"]["packages"], settings["addons"]["namespace"],
            *(path for paths in settings["addons"]["repo-files"].values() for path in paths)}


def test_the_build_context_is_exactly_what_the_wheels_need():
    """An allowlist, so a new file at the root, a token or the local config never reaches the build."""
    lines, allowed = _context_entries()
    assert lines[0] == "*"
    assert set(allowed) == _build_inputs()
    assert {"**/__pycache__", "**/*.pyc", "**/.*"} <= set(lines)


def test_the_build_stage_on_that_context_makes_the_checkouts_wheels(tmp_path):
    """What `COPY . .` puts in the build stage (the allowlisted tracked files, no .git) gives the same wheels as a
    build from the checkout, so the image installs exactly what pip would."""
    _, allowed = _context_entries()
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *allowed], capture_output=True,
                             check=True).stdout.decode("utf-8").split("\0")
    context = tmp_path / "context"
    for name in filter(None, tracked):
        (context / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, context / name)
    from_context = build.build_all(context, tmp_path / "image")
    from_checkout = build.build_all(ROOT, tmp_path / "checkout")
    assert [p.name for p in from_context] == [p.name for p in from_checkout]
    assert all(a.read_bytes() == b.read_bytes() for a, b in zip(from_context, from_checkout, strict=True))


# ---------------------------------------------------------------------------------------------- harness.container

@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "volume" / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "volume" / "data"))
    monkeypatch.delenv("HARNESS_PORT", raising=False)
    monkeypatch.delenv("HARNESS_ENABLE_MODULES", raising=False)
    monkeypatch.setattr("harness.config._apply_managed_overlay", lambda cfg: None)
    return tmp_path / "volume"


def test_the_first_config_is_the_service_profile_on_loopback(tmp_path):
    args = container.first_start_args(tmp_path / "c", tmp_path / "d", {"HARNESS_ENABLE_MODULES": "jobs, hub,"})
    assert args == ["--profile", "service", "--config-dir", str(tmp_path / "c"), "--data-dir", str(tmp_path / "d"),
                    "--port", "8100", "--pause-flag", str(tmp_path / "d" / "llama-server.paused"),
                    "--enable-module", "jobs", "--enable-module", "hub"]
    assert container.first_start_args(tmp_path, tmp_path, {"HARNESS_PORT": "9123"})[7] == "9123"
    for port in ("0", "65536", "80a", "-1"):
        with pytest.raises(SystemExit, match="HARNESS_PORT"):
            container.first_start_args(tmp_path, tmp_path, {"HARNESS_PORT": port})


def test_the_first_start_writes_the_config_once_and_later_starts_keep_it(volume, monkeypatch):
    monkeypatch.setenv("HARNESS_ENABLE_MODULES", "jobs")
    assert container.prepare() is True
    raw = yaml.safe_load((volume / "config" / "harness.yaml").read_text(encoding="utf-8"))
    assert raw["listen"] == {"host": "127.0.0.1", "port": 8100} and raw["data_dir"] == (volume / "data").as_posix()
    profile = yaml.safe_load((volume / "config" / "profile.yaml").read_text(encoding="utf-8"))
    assert profile["profile"] == "service" and profile["modules"] == {"jobs": True}
    (volume / "config" / "harness.yaml").write_text(
        (volume / "config" / "harness.yaml").read_text(encoding="utf-8").replace("8100", "8111"), encoding="utf-8")
    assert container.prepare() is False  # the owner's change stands
    assert yaml.safe_load((volume / "config" / "harness.yaml").read_text(encoding="utf-8"))["listen"]["port"] == 8111


def test_every_location_the_daemon_picks_is_in_the_volume(tmp_path, monkeypatch):
    """The review of #1002: HARNESS_DATA_DIR moves data/, but the image work dir, its logs and the memory library clone
    default under the XDG data dir. The image sets XDG_DATA_HOME so that dir's agent-harness is the volume; this loads
    a first-start config there, with modules that keep files switched on, and finds no absolute path outside it."""
    from harness import config
    lib = tmp_path / "lib"
    volume = lib / "agent-harness"
    monkeypatch.setattr(sys, "platform", "linux")  # the image's platform
    monkeypatch.setattr(config, "CHECKOUT", None)  # installed from the wheels
    monkeypatch.setattr(config, "_apply_managed_overlay", lambda cfg: None)
    for name, value in {"XDG_DATA_HOME": lib, "HARNESS_CONFIG_DIR": volume / "config", "HARNESS_DATA_DIR": volume / "data",
                        "HARNESS_ENABLE_MODULES": "jobs,backup,images,memory_library,homelab"}.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("HARNESS_PORT", raising=False)
    container.prepare()
    cfg = config.load()
    found = []

    def walk(value, where):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{where}.{key}")
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, where)
        elif isinstance(value, (str, Path)) and str(value) and Path(str(value)).is_absolute():
            found.append((where, Path(str(value))))
    walk(dataclasses.asdict(cfg), "cfg")
    assert {where for where, _ in found} >= {"cfg.data_dir", "cfg.images.work_dir", "cfg.memory_library.clone_dir"}
    assert [(where, str(path)) for where, path in found if not path.is_relative_to(volume)] == []


def test_the_volume_must_be_named(volume, monkeypatch):
    monkeypatch.delenv("HARNESS_DATA_DIR")
    with pytest.raises(SystemExit, match="HARNESS_DATA_DIR"):
        container.prepare()


def test_a_config_the_daemon_would_refuse_stops_the_first_start(volume, monkeypatch):
    monkeypatch.setenv("HARNESS_ENABLE_MODULES", "no-such-module")
    with pytest.raises(SystemExit):
        container.prepare()


def test_health_asks_the_configured_loopback_port(volume, monkeypatch):
    answers = []

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            answers.append(self.path)
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HARNESS_PORT", str(server.server_address[1]))
        assert container.healthy() is False  # no config yet: no port to ask
        container.prepare()
        assert container.healthy() is True and answers == ["/health"]
        assert container.main(["--health"]) == 0
    finally:
        server.shutdown()
        server.server_close()
    assert container.healthy(timeout=1) is False and container.main(["--health"]) == 1


def test_main_prepares_then_runs_the_daemon_with_no_arguments(volume, monkeypatch):
    seen = []
    monkeypatch.setattr("harness.__main__.main", lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["harness.container"])
    assert container.main([]) == 0
    assert seen == [["harness.container"]] and (volume / "config" / "harness.yaml").is_file()
    with pytest.raises(SystemExit, match="usage"):
        container.main(["--port", "9000"])


# ---------------------------------------------------------------------------------- the smoke script, fake Docker

class FakeDocker:
    """Answers the smoke script's docker commands the way a healthy image does, unless told otherwise."""

    def __init__(self, **broken):
        self.broken = broken
        self.calls: list[list[str]] = []
        self.health_tries = 0

    def __call__(self, args: list[str]) -> SimpleNamespace:
        self.calls.append(args)
        out = lambda stdout="", code=0, stderr="": SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)  # noqa: E731
        if args[:2] == ["image", "inspect"]:
            if "missing" in self.broken:
                return out(code=1, stderr="No such image")
            return out(json.dumps({"User": self.broken.get("user", "10001:10001"),
                                   "Volumes": {VOLUME: {}}, "ExposedPorts": self.broken.get("ports"),
                                   "Env": [f"HARNESS_CONFIG_DIR={VOLUME}/config", f"HARNESS_DATA_DIR={VOLUME}/data"]}))
        if args[:2] == ["run", "--rm"]:
            return out(json.dumps(self.broken.get("secrets", [])))
        if args[:2] == ["run", "-d"]:
            return out("container-id\n")
        if args[0] == "exec":
            code = args[-1] if args[-1] != VOLUME else args[-2]
            if code == smoke.HEALTH:
                self.health_tries += 1
                if "unhealthy" in self.broken or self.health_tries < 2:
                    return out(code=1, stderr="Connection refused")
                return out(json.dumps({"ok": True, "profile": "service", "release": "0.9.0"}))
            if code == smoke.LISTENING:
                return out(json.dumps(self.broken.get("listening", ["0100007F:1FA4"])))
            if "Uid:" in code:
                return out(self.broken.get("uid", "10001") + "\n")
            if "install_home" in code:
                return out(self.broken.get("home", VOLUME) + "\n")
            return out(json.dumps(self.broken.get("files", {"config/harness.yaml": 10001,
                                                            "data/harness.sqlite3": 10001,
                                                            "data/local-owner.token": 10001})))
        if args[0] == "logs":
            return out("daemon log tail")
        return out()


def _verdicts(fake: FakeDocker) -> dict[str, bool]:
    return {check.name: check.ok for check in smoke.smoke("img", run=fake, timeout=5, sleep=lambda _: None)}


def test_a_healthy_image_passes_every_check_and_leaves_nothing_behind():
    fake = FakeDocker()
    assert _verdicts(fake) == dict.fromkeys(["unprivileged user", "volume", "no published port",
                                             "no secrets in the image", "GET /health", "daemon is unprivileged",
                                             "loopback only", "state in the volume", "defaults in the volume"], True)
    started = next(c for c in fake.calls if c[:2] == ["run", "-d"])
    assert started[started.index("--network") + 1] == "none"
    name, volume_name = started[started.index("--name") + 1], started[started.index("-v") + 1].split(":")[0]
    assert ["rm", "-f", name] in fake.calls and ["volume", "rm", "-f", volume_name] in fake.calls


@pytest.mark.parametrize("broken, failing", [
    ({"user": "root"}, "unprivileged user"),
    ({"ports": {"8100/tcp": {}}}, "no published port"),
    ({"secrets": ["/var/lib/agent-harness/data/local-owner.token"]}, "no secrets in the image"),
    ({"uid": "0"}, "daemon is unprivileged"),
    ({"listening": ["00000000:1FA4"]}, "loopback only"),
    ({"listening": ["00000000000000000000000000000000:1FA4"]}, "loopback only"),
    ({"files": {"config/harness.yaml": 0}}, "state in the volume"),
    ({"home": "/home/harness/.local/share/agent-harness"}, "defaults in the volume"),
])
def test_each_property_the_image_promises_fails_its_own_check(broken, failing):
    verdicts = _verdicts(FakeDocker(**broken))
    assert [name for name, ok in verdicts.items() if not ok] == [failing]


def test_an_unhealthy_container_reports_its_logs_and_is_still_removed():
    fake = FakeDocker(unhealthy=True)
    checks = smoke.smoke("img", run=fake, timeout=0, sleep=lambda _: None)
    assert [(c.name, c.ok) for c in checks][-2:] == [("GET /health", False), ("logs", False)]
    assert checks[-1].detail == "daemon log tail"
    assert any(c[:2] == ["rm", "-f"] for c in fake.calls) and any(c[:3] == ["volume", "rm", "-f"] for c in fake.calls)


def test_a_missing_image_stops_before_starting_anything(capsys, monkeypatch):
    fake = FakeDocker(missing=True)
    assert [(c.name, c.ok) for c in smoke.smoke("img", run=fake)] == [("image", False)]
    assert not any(c[0] == "run" for c in fake.calls)
    monkeypatch.setattr(smoke, "docker", fake)
    monkeypatch.setattr(smoke, "smoke", lambda image, timeout: [smoke.Check("image", False, "No such image")])
    assert smoke.main(["img"]) == 1 and "FAIL image: No such image" in capsys.readouterr().out
