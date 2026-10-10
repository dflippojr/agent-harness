"""Hub installer contracts use stub launches, never a real Hub, host, or token."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from harness import install_hub as hub
from tests.test_unix_installers import BASH, ROOT, run_installer


def options(tmp_path, **changes):
    values = dict(install_dir=tmp_path, config_dir=tmp_path / "config", port=8199,
                  with_hub=True, no_hub=False, hub_method="pip", hub_package="stub-hub==1",
                  hub_image="example.invalid/stub-hub:1", hub_module="stub_hub", uv="uv", no_start=False)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("answer,expected", [("", True), ("Y", True), ("yes", True), ("n", False), ("no", False)])
def test_interactive_offer(answer, expected):
    prompts = []
    def ask(prompt):
        prompts.append(prompt)
        return answer
    assert hub.choose(False, False, SimpleNamespace(isatty=lambda: True), ask) is expected
    assert prompts == [hub.PROMPT]


def test_offer_eof_invalid_answers_and_flags():
    stream = SimpleNamespace(isatty=lambda: True)
    answers = iter(["maybe", "no"])
    assert not hub.choose(False, False, stream, lambda _: next(answers))
    def eof(_):
        raise EOFError
    assert not hub.choose(False, False, stream, eof)
    def forbidden(_):
        pytest.fail("unexpected prompt")
    assert hub.choose(True, False, stream, forbidden)
    assert not hub.choose(False, True, stream, forbidden)
    assert not hub.choose(False, False, SimpleNamespace(isatty=lambda: False), forbidden)
    with pytest.raises(ValueError, match="mutually exclusive"):
        hub.choose(True, True)


def test_decline_never_contacts_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub, "hub_cli", lambda *_: pytest.fail("no daemon call when declining"))
    assert hub.install(options(tmp_path, with_hub=False, no_hub=True)) == 0
    assert hub.PARITY in capsys.readouterr().out
    assert not (tmp_path / "hub-install.json").exists()


def test_already_claimed_never_installs_or_releases(tmp_path, monkeypatch, capsys):
    calls = []
    def cli(args, env):
        calls.append(args)
        return {"claimed": True}
    monkeypatch.setattr(hub, "hub_cli", cli)
    monkeypatch.setattr(hub, "command", lambda *_: pytest.fail("already claimed must not install"))
    assert hub.install(options(tmp_path)) == 0
    assert calls == [["hub", "status"]]
    assert "harness hub release --confirm" in capsys.readouterr().out
    assert not (tmp_path / "hub-install.json").exists()


def test_disabled_startup_defers_hub(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub, "hub_cli", lambda *_: pytest.fail("startup disabled"))
    assert hub.install(options(tmp_path, no_start=True)) == 0
    assert "Hub setup deferred" in capsys.readouterr().out
    result = run_installer(tmp_path, "Linux", "x86_64", "--with-hub", "--no-start")
    assert result.returncode == 0 and "Hub setup deferred" in result.stdout
    assert "harness hub approve" not in result.stdout
    if os.name == "nt":
        result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(ROOT / "install/install.ps1"),
                                 "-DryRun", "-NoTasks", "-WithHub", "-Profile", "Service"],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0 and "Hub setup deferred" in result.stdout


@pytest.mark.parametrize("method", ["pip", "docker"])
def test_stub_distribution_installs_approves_and_uninstalls(tmp_path, monkeypatch, capsys, method):
    commands, events = [], []
    claim = {"id": "pr-stub", "match_code": "123456"}
    claimed = False
    def cli(args, env):
        nonlocal claimed
        events.append(args)
        assert env["HARNESS_URL"] == "http://127.0.0.1:8199"
        if args[1] == "approve":
            # Match code must be shown before host approval is attempted.
            assert "match code: 123456" in capsys.readouterr().out
            assert args == ["hub", "approve", "pr-stub", "--match", "123456"]
            claimed = True
        elif args[1] == "release":
            claimed = False
        return {"claimed": claimed, "hub": {"request_id": "pr-stub"}}
    def publish(directory):
        # Stub package launch implements #546's provisional installer adapter.
        (directory / "claim.json").write_text(json.dumps(claim), encoding="utf-8")
    def command(args, **_kwargs):
        commands.append([str(a) for a in args])
        if args[:2] == ["docker", "run"]:
            mount = args[args.index("--mount") + 1]
            publish(Path(mount.split("src=", 1)[1].split(",dst=", 1)[0]))
        if args[:2] == ["docker", "ps"]:
            record = json.loads((tmp_path / "hub-install.json").read_text())
            return SimpleNamespace(stdout=hub.Service(record["id"]).name + "\n", stderr="")
        return SimpleNamespace(stdout="{}", stderr="")
    def start(_self, argv, workdir, log):
        assert argv[1:3] == ["-m", "stub_hub"]
        assert argv[argv.index("--daemon-url") + 1] == "http://127.0.0.1:8199"
        publish(workdir)
    monkeypatch.setattr(hub, "hub_cli", cli)
    monkeypatch.setattr(hub, "command", command)
    monkeypatch.setattr(hub.Service, "start", start)
    monkeypatch.setattr(hub.Service, "stop", lambda self: events.append(["stop", self.name]))
    monkeypatch.setattr(hub.shutil, "which", lambda _: "docker")
    args = options(tmp_path, hub_method=method)
    assert hub.install(args) == 0
    state = json.loads((tmp_path / "hub-install.json").read_text())
    directory = tmp_path / ("hub-" + state["id"])
    assert not (directory / "claim.json").exists()
    if method == "pip":
        assert commands[1][-1] == "stub-hub==1"
        assert str(directory / "venv") in commands[0]
    else:
        assert "host" in commands[0] and "--restart" in commands[0]
        assert "example.invalid/stub-hub:1" in commands[0]
        assert "--state-dir" in commands[0]
    assert hub.uninstall(args) == 0
    assert events.index(["hub", "release", "--confirm"]) > events.index(["hub", "approve", "pr-stub", "--match", "123456"])
    if method == "pip":
        assert events[-1][0] == "stop"
    else:
        assert commands[-1][:3] == ["docker", "rm", "-f"]
    assert not directory.exists() and not (tmp_path / "hub-install.json").exists()


@pytest.mark.parametrize("method,setting", [("pip", "HARNESS_HUB_PACKAGE"), ("docker", "HARNESS_HUB_IMAGE")])
def test_missing_distribution_fails_without_mutation(tmp_path, monkeypatch, method, setting):
    monkeypatch.setattr(hub, "hub_cli", lambda *_: {"claimed": False})
    with pytest.raises(ValueError, match=setting):
        hub.install(options(tmp_path, hub_method=method, hub_package="", hub_image=""))
    assert not (tmp_path / "hub-install.json").exists()


def test_existing_install_not_overwritten(tmp_path, monkeypatch):
    (tmp_path / "hub-install.json").write_text("{}")
    monkeypatch.setattr(hub, "hub_cli", lambda *_: {"claimed": False})
    with pytest.raises(ValueError, match="already has a Hub service"):
        hub.install(options(tmp_path))


def test_auto_distribution_and_module_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(hub.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(hub.shutil, "which", lambda _: "docker")
    monkeypatch.setattr("builtins.input", lambda _: "docker")
    assert hub.distribution_choice(options(tmp_path, hub_method="auto"))[0] == "docker"
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert hub.distribution_choice(options(tmp_path, hub_method="auto"))[0] == "pip"
    monkeypatch.setattr(hub.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert hub.distribution_choice(options(tmp_path, hub_method="auto"))[0] == "pip"
    with pytest.raises(ValueError, match="MODULE"):
        hub.distribution_choice(options(tmp_path, hub_module="bad;module"))
    with pytest.raises(ValueError, match="PACKAGE"):
        hub.distribution_choice(options(tmp_path, hub_package="--evil"))


@pytest.mark.parametrize("data", [{}, {"id": "pr-x", "match_code": "abc"},
                                  {"id": "../../x", "match_code": "123456"},
                                  {"id": "pr-x", "match_code": "123456", "token": "forbidden"}])
def test_invalid_claim_is_never_approved(tmp_path, data):
    path = tmp_path / "claim.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="claim file"):
        hub.wait_for_claim(path)


def test_claim_waits_and_times_out(tmp_path, monkeypatch):
    clock = iter([0, 0, 2])
    monkeypatch.setattr(hub.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(hub.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="publish"):
        hub.wait_for_claim(tmp_path / "missing", timeout=1)


def test_claim_redeem_timeout_and_competitor(tmp_path, monkeypatch):
    path = tmp_path / "claim.json"
    path.write_text(json.dumps({"id": "pr-x", "match_code": "123456"}))
    monkeypatch.setattr(hub, "hub_cli", lambda *_: {"claimed": True, "hub": {"request_id": "pr-other"}})
    with pytest.raises(ValueError, match="Another Hub"):
        hub.approve_and_wait(path, {})
    monkeypatch.setattr(hub, "hub_cli", lambda *_: {"claimed": False})
    clock = iter([0, 0, 0, 61])  # claim-file wait, redemption deadline, poll, expiry
    monkeypatch.setattr(hub.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(hub.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="redeem"):
        hub.approve_and_wait(path, {})


def test_cli_uses_this_install_not_paired_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TOKEN", "other-client")
    monkeypatch.setenv("HARNESS_LOCAL_TOKEN", "other-daemon")
    env = hub.cli_env(tmp_path, 8199)
    assert env["HARNESS_CONFIG_DIR"] == str(tmp_path)
    assert "HARNESS_TOKEN" not in env and "HARNESS_LOCAL_TOKEN" not in env
    calls = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout='{"claimed": false}', stderr="host warning\n")
    monkeypatch.setattr(hub, "command", command)
    assert hub.hub_cli(["hub", "status"], env) == {"claimed": False}
    assert "--config" in calls[0][0]
    assert not Path(calls[0][0][calls[0][0].index("--config") + 1]).exists()


def test_uninstall_release_failure_preserves_owned_install(tmp_path, monkeypatch):
    service_id = "a" * 32
    (tmp_path / "hub-install.json").write_text(json.dumps({"id": service_id, "method": "pip", "port": 8199}))
    directory = tmp_path / ("hub-" + service_id)
    directory.mkdir()
    def cli(args, env):
        if args[1] == "release":
            raise subprocess.CalledProcessError(1, ["harness", *args])
        return {"claimed": True}
    monkeypatch.setattr(hub, "hub_cli", cli)
    monkeypatch.setattr(hub.Service, "stop", lambda _: pytest.fail("must release before stop"))
    with pytest.raises(subprocess.CalledProcessError):
        hub.uninstall(options(tmp_path))
    assert directory.exists() and (tmp_path / "hub-install.json").exists()


def test_uninstall_without_owned_hub_uses_config_port(tmp_path, monkeypatch):
    from harness import config
    monkeypatch.setattr(config, "load", lambda _: SimpleNamespace(port=8199))
    calls = []
    monkeypatch.setattr(hub, "hub_cli", lambda args, env: calls.append(env["HARNESS_URL"]) or {"claimed": False})
    assert hub.uninstall(options(tmp_path)) == 0
    assert calls == ["http://127.0.0.1:8199"]


def test_partial_docker_install_is_removable(tmp_path, monkeypatch):
    (tmp_path / "hub-install.json").write_text(json.dumps({"id": "a" * 32, "method": "docker", "port": 8199}))
    monkeypatch.setattr(hub, "hub_cli", lambda *_: {"claimed": False})
    calls = []
    def command(args, **_):
        calls.append(args)
        return SimpleNamespace(stdout="unrelated-hub\n", stderr="")
    monkeypatch.setattr(hub, "command", command)
    assert hub.uninstall(options(tmp_path)) == 0
    assert calls == [["docker", "ps", "-a", "--format", "{{.Names}}"]]
    assert not (tmp_path / "hub-install.json").exists()


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_per_user_service_registration_and_removal(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(hub.sys, "platform", platform)
    monkeypatch.setattr(hub.Path, "home", classmethod(lambda _: tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(hub.os, "getuid", lambda: 42, raising=False)
    calls = []
    def command(args, **_):
        calls.append([str(a) for a in args])
        return SimpleNamespace(stdout="- 0 agent-harness-hub-" + "a" * 32, stderr="")
    monkeypatch.setattr(hub, "command", command)
    service = hub.Service("a" * 32)
    service.start([str(tmp_path / "path with spaces/python"), "-m", "stub_hub", "--state-dir", "a'%b"], tmp_path, tmp_path / "hub.log")
    if platform == "linux":
        unit = (tmp_path / "config/systemd/user" / (service.name + ".service")).read_text()
        assert "path with spaces" in unit and "a'%%b" in unit
    elif platform == "darwin":
        import plistlib
        plist = plistlib.loads((tmp_path / "Library/LaunchAgents" / (service.name + ".plist")).read_bytes())
        assert plist["ProgramArguments"][-1] == "a'%b"
    else:
        import base64
        script = base64.b64decode(calls[0][-1]).decode("utf-16le")
        assert "a''%b" in script and "Register-ScheduledTask" in script
    service.stop()
    assert len(calls) >= 2
    with pytest.raises(ValueError, match="service id"):
        hub.Service("../unsafe")


@pytest.mark.parametrize("args", [("--with-hub",), ("--with-hub", "--hub-method", "docker", "--hub-image", "stub:1"), ("--no-hub",), ()])
def test_unix_hub_dry_run(tmp_path, args):
    result = run_installer(tmp_path, "Linux", "x86_64", "--profile", "service", *args)
    assert result.returncode == 0, result.stderr
    if "--with-hub" in args:
        assert "harness hub approve <request_id> --match <code>" in result.stdout
        assert result.stdout.index("Checking the install") < result.stdout.index("Optional Hub")
    else:
        assert "docs/management-parity.md" in result.stdout and "Add the Hub later" in result.stdout
    assert not (tmp_path / "install").exists()


def test_unix_conflicting_flags(tmp_path):
    result = run_installer(tmp_path, "Linux", "x86_64", "--with-hub", "--no-hub")
    assert result.returncode != 0 and "mutually exclusive" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows installer")
@pytest.mark.parametrize("flags", [["-WithHub"], ["-WithHub", "-HubMethod", "docker", "-HubImage", "stub:1"], ["-NoHub"], []])
def test_windows_hub_dry_run(tmp_path, flags):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(ROOT / "install/install.ps1"),
                             "-InstallDir", str(tmp_path / "install"), "-Profile", "Service", "-DryRun", *flags],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    if "-WithHub" in flags:
        assert "harness hub approve <request_id> --match <code>" in result.stdout
        assert result.stdout.index("Checking the install") < result.stdout.index("Optional Hub")
    else:
        assert "docs/management-parity.md" in result.stdout and "Add the Hub later" in result.stdout
    assert not (tmp_path / "install").exists()


def test_uninstall_dry_run(tmp_path):
    if not BASH:
        pytest.skip("bash is not installed")
    result = subprocess.run([BASH, "install/uninstall.sh", "--install-dir", str(tmp_path), "--dry-run"],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "before removing daemon" in result.stdout
    if os.name == "nt":
        result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(ROOT / "install/uninstall.ps1"),
                                 "-InstallDir", str(tmp_path), "-DryRun"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0 and "before removing daemon" in result.stdout


def test_main_errors_and_decline(tmp_path, monkeypatch, capsys):
    base = ["install", "--install-dir", str(tmp_path), "--config-dir", str(tmp_path / "config")]
    assert hub.main([*base, "--no-hub"]) == 0
    monkeypatch.setattr(hub, "install", lambda _: (_ for _ in ()).throw(ValueError("stub failure")))
    assert hub.main(base) == 1
    assert "stub failure" in capsys.readouterr().err
