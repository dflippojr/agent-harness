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
from tests.installer_support import BASH, ROOT, run_installer

DAEMON_AVAILABLE = hub.daemon_available


@pytest.fixture(autouse=True)
def no_real_daemon_probe(monkeypatch):
    monkeypatch.setattr(hub, "daemon_available", lambda _: True)


def options(tmp_path, **changes):
    values = dict(install_dir=tmp_path, config_dir=tmp_path / "config", port=8199,
                  with_hub=True, no_hub=False, hub_method="pip", hub_package="stub-hub==1",
                  hub_image="example.invalid/stub-hub:1", hub_module="stub_hub", uv="uv", no_start=False)
    values.update(changes)
    values["config_dir"].mkdir(exist_ok=True)
    (values["config_dir"] / "harness.yaml").write_text(json.dumps({
        "listen": {"port": values["port"]}, "data_dir": str(tmp_path / "data")}))
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
    assert calls == [["hub", "claim-status"]]
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


def test_state_directory_modified_before_acl_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "hub_cli", lambda *_: {"claimed": False})
    def secure(path, *, inherit=False):
        assert inherit
        (path / "claim.json").write_text('{"id":"pr-other","match_code":"123456"}')
    monkeypatch.setattr(hub, "owner_only_acl", secure)
    monkeypatch.setattr(hub, "start_hub", lambda *_: pytest.fail("must not start or approve an altered directory"))
    with pytest.raises(ValueError, match="before it could be secured"):
        hub.install(options(tmp_path))
    assert not (tmp_path / "hub-install.json").exists()


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
    assert hub.hub_cli(["hub", "claim-status"], env) == {"claimed": False}
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


@pytest.mark.parametrize("record_port", [None, 8100])
def test_uninstall_uses_current_config_port(tmp_path, monkeypatch, record_port):
    from harness import config
    if record_port is not None:
        (tmp_path / "hub-install.json").write_text(json.dumps({"id": "a" * 32, "method": "pip", "port": record_port}))
        monkeypatch.setattr(hub.Service, "stop", lambda _: None)
    monkeypatch.setattr(config, "resolve_port", lambda _: 8199)
    calls = []
    monkeypatch.setattr(hub, "hub_cli", lambda args, env: calls.append(env["HARNESS_URL"]) or {"claimed": False})
    assert hub.uninstall(options(tmp_path)) == 0
    assert calls == ["http://127.0.0.1:8199"]


def test_add_hub_later_uses_preserved_config_port(tmp_path, monkeypatch):
    args = options(tmp_path, port=8100)
    (args.config_dir / "harness.yaml").write_text(json.dumps({"listen": {"port": 8199}}))
    calls = []
    monkeypatch.setattr(hub, "hub_cli", lambda cmd, env: calls.append(env["HARNESS_URL"]) or {"claimed": True})
    assert hub.install(args) == 0
    assert args.port == 8100 and calls == ["http://127.0.0.1:8199"]


def test_stopped_daemon_without_owned_hub_can_be_uninstalled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub, "daemon_available", lambda _: False)
    monkeypatch.setattr(hub, "hub_cli", lambda *_: pytest.fail("must not query stopped daemon"))
    assert hub.uninstall(options(tmp_path)) == 0
    assert "Continuing daemon uninstall" in capsys.readouterr().out


def test_stopped_daemon_with_owned_hub_requires_release(tmp_path, monkeypatch):
    (tmp_path / "hub-install.json").write_text(json.dumps({"id": "a" * 32, "method": "pip", "port": 8199}))
    monkeypatch.setattr(hub, "daemon_available", lambda _: False)
    with pytest.raises(ValueError, match="Start the daemon"):
        hub.uninstall(options(tmp_path))
    assert (tmp_path / "hub-install.json").exists()


def test_daemon_probe_uses_loopback_and_handles_stopped_server(monkeypatch):
    seen = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    def connect(address, timeout):
        seen.append((address, timeout))
        return Connection()
    monkeypatch.setattr(hub.socket, "create_connection", connect)
    assert DAEMON_AVAILABLE(8199)
    assert seen == [(("127.0.0.1", 8199), 2)]
    def stopped(*args, **kwargs):
        raise ConnectionRefusedError
    monkeypatch.setattr(hub.socket, "create_connection", stopped)
    assert not DAEMON_AVAILABLE(8199)


def test_doctor_not_started_preserves_config_and_image_checks(monkeypatch, tmp_path):
    from harness import config, doctor
    cfg = SimpleNamespace(default_model="", modules=SimpleNamespace(local_model=False), profile="service", port=8199)
    monkeypatch.setattr(config, "load", lambda _: cfg)
    seen = []
    for name in dir(doctor):
        if name.startswith("check_"):
            def check(*args, _name=name):
                seen.append(_name)
                if _name == "check_daemon":
                    args[0].fail("Stopped service", "not running")
            monkeypatch.setattr(doctor, name, check)
    assert doctor.main(["--not-started"]) == 0
    assert "check_data_dir" in seen and "check_docker" in seen
    assert "check_daemon" not in seen and "check_optional" in seen
    assert doctor.main([]) == 1
    monkeypatch.setattr(doctor, "check_docker", lambda report, cfg: report.fail("Docker image", "missing"))
    assert doctor.main(["--not-started"]) == 1
    assert "[[ $no_start -eq 1 ]] && doctor_args+=(--not-started)" in (ROOT / "install/install.sh").read_text()
    assert "if ($NoTasks) { $doctorArgs += '--not-started' }" in (ROOT / "install/install.ps1").read_text()


def test_partial_install_without_config_is_removable(tmp_path, monkeypatch, capsys):
    args = options(tmp_path)
    (args.config_dir / "harness.yaml").unlink()
    monkeypatch.setattr(hub, "hub_cli", lambda *_: pytest.fail("no daemon or Hub was installed"))
    assert hub.uninstall(args) == 0
    assert "continuing partial daemon uninstall" in capsys.readouterr().out
    (tmp_path / "hub-install.json").write_text(json.dumps({"id": "a" * 32, "method": "pip", "port": 8199}))
    with pytest.raises(ValueError, match="Restore daemon configuration"):
        hub.uninstall(args)


def test_partial_uninstall_without_site_packages(tmp_path):
    result = subprocess.run([sys.executable, "-S", "-m", "harness.install_hub", "uninstall",
                             "--install-dir", str(tmp_path), "--config-dir", str(tmp_path / "missing-config")],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "continuing partial daemon uninstall" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell native argument passing")
@pytest.mark.parametrize("no_tasks", [True, False])
def test_windows_doctor_native_arguments(tmp_path, no_tasks):
    source = (ROOT / "install/install.ps1").read_text()
    block = source[source.index("    $doctorArgs ="):source.index("    try {\n        & $python @doctorArgs")]
    stub = tmp_path / "argv.py"
    stub.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = (
        f"$configDir = {quote(tmp_path / 'config directory')}; $Instance = 'Main'; "
        f"$NoTasks = {'$true' if no_tasks else '$false'}; $ExistingServer = ''; $NeedsLocalModel = $false;\n"
        + block + f"\n& {quote(sys.executable)} {quote(stub)} @doctorArgs"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    argv = json.loads(result.stdout)
    assert argv[:4] == ["-m", "harness.doctor", "--config-dir", str(tmp_path / "config directory")]
    if no_tasks:
        assert "--instance" not in argv and "--not-started" in argv
    else:
        assert argv[argv.index("--instance") + 1] == "Main" and "--not-started" not in argv


def test_not_started_keeps_image_files_and_defers_live_probes(tmp_path, monkeypatch):
    from harness import config, doctor, modules, setup_config
    from harness_modules.local_model import doctor as model_doctor
    from harness_modules.images import doctor as image_doctor
    from harness_modules.backup import runtime as backup_runtime
    args = options(tmp_path)
    assert setup_config.main(["--config-dir", str(args.config_dir), "--data-dir", str(tmp_path / "data"),
                              "--pause-flag", str(tmp_path / "paused"), "--profile", "service",
                              "--enable-module", "images", "--force"]) == 0
    cfg = config.load(args.config_dir)
    cfg.images.enabled = True
    cfg.images.comfy_dir = str(tmp_path / "missing-comfy")
    cfg.images.models_dir = str(tmp_path / "missing-weights")
    cfg.web.enabled = True
    hash_checks = []
    assets_status = image_doctor.image_edit.assets_status
    def checked_assets(*args, **kwargs):
        hash_checks.append(kwargs.get("verify_hash"))
        return assets_status(*args, **kwargs)
    monkeypatch.setattr(image_doctor.image_edit, "assets_status", checked_assets)
    monkeypatch.setattr(model_doctor, "check_gpu", lambda *_: None)
    monkeypatch.setattr(model_doctor, "check_model_server", lambda *_: pytest.fail("live model probe"))
    monkeypatch.setattr(model_doctor, "check_guard", lambda *_: pytest.fail("live guard probe"))
    monkeypatch.setattr(doctor.httpx, "get", lambda *_args, **_kwargs: pytest.fail("live HTTP probe"))
    hooks = [model_doctor.run, image_doctor.run, backup_runtime.doctor]
    monkeypatch.setattr(modules, "present", lambda _: [SimpleNamespace(doctor=hook) for hook in hooks])
    monkeypatch.setattr(doctor, "run", lambda *_: (1, ""))
    report = doctor.Report(not_started=True)
    doctor.check_optional(report, cfg)
    assert report.failed >= 1  # Missing ComfyUI is still an installation failure.
    assert hash_checks == [True]


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


def test_docker_state_uses_host_user_on_unix(tmp_path, monkeypatch):
    monkeypatch.setattr(hub.sys, "platform", "linux")
    monkeypatch.setattr(hub.os, "getuid", lambda: 1001, raising=False)
    monkeypatch.setattr(hub.os, "getgid", lambda: 1002, raising=False)
    monkeypatch.setattr(hub.shutil, "which", lambda _: "docker")
    calls = []
    monkeypatch.setattr(hub, "command", lambda args, **_: calls.append(args))
    hub.start_hub(options(tmp_path), "docker", "stub:1", "a" * 32, tmp_path, tmp_path / "claim.json", port=8199)
    assert calls[0][calls[0].index("--user") + 1] == "1001:1002"


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
    workdir = tmp_path / "path with spaces%$HOME"
    service.start([str(tmp_path / "path with spaces/python"), "-m", "stub_hub", "--state-dir", "a'%b$HOME"], workdir, tmp_path / "hub.log")
    if platform == "linux":
        unit = (tmp_path / "config/systemd/user" / (service.name + ".service")).read_text()
        assert "path with spaces" in unit and "a'%%b$$HOME" in unit
        directory = next(line.removeprefix("WorkingDirectory=") for line in unit.splitlines() if line.startswith("WorkingDirectory="))
        assert directory == str(workdir).replace("%", "%%")
        assert not directory.startswith('"')
    elif platform == "darwin":
        import plistlib
        plist = plistlib.loads((tmp_path / "Library/LaunchAgents" / (service.name + ".plist")).read_bytes())
        assert plist["ProgramArguments"][-1] == "a'%b$HOME"
    else:
        import base64
        script = base64.b64decode(calls[0][-1]).decode("utf-16le")
        assert "a'%b$HOME" not in script and "Register-ScheduledTask" in script
        assert "-Argument $env:HUB_ARGS -WorkingDirectory $env:HUB_DIR" in script
        assert "-AllowStartIfOnBatteries" in script and "-DontStopIfGoingOnBatteries" in script
    service.stop()
    assert len(calls) >= 2
    with pytest.raises(ValueError, match="service id"):
        hub.Service("../unsafe")


def test_empty_xdg_home_uses_same_default_for_start_and_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(hub.sys, "platform", "linux")
    monkeypatch.setattr(hub.Path, "home", classmethod(lambda _: tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setattr(hub, "command", lambda *_args, **_kwargs: None)
    service = hub.Service("a" * 32)
    expected = tmp_path / ".config/systemd/user" / (service.name + ".service")
    assert service.definition_path == expected
    service.start([str(tmp_path / "python"), "-m", "stub_hub"], tmp_path, tmp_path / "hub.log")
    assert expected.exists()
    service.stop()
    assert not expected.exists()


def test_relative_installer_paths_are_resolved_before_launch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(hub, "install", lambda args: seen.append(args) or 0)
    assert hub.main(["install", "--install-dir", "runtime", "--config-dir", "runtime/config", "--with-hub"]) == 0
    assert seen[0].install_dir == tmp_path / "runtime"
    assert seen[0].config_dir == tmp_path / "runtime/config"
    assert seen[0].install_dir.is_absolute() and seen[0].config_dir.is_absolute()


@pytest.mark.skipif(os.name != "nt", reason="Windows installer")
@pytest.mark.parametrize("flags", [["-WithHub"], ["-WithHub", "-HubMethod", "docker", "-HubImage", "stub:1"],
                                  ["-WithHub", "-HubMethod", "Docker", "-HubImage", "stub:1"], ["-NoHub"], []])
def test_windows_hub_dry_run(tmp_path, flags):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(ROOT / "install/install.ps1"),
                             "-InstallDir", str(tmp_path / "install"), "-Profile", "Service", "-DryRun", *flags],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    if "-WithHub" in flags:
        assert "harness hub approve <request_id> --match <code>" in result.stdout
        assert result.stdout.index("Checking the install") < result.stdout.index("Optional Hub")
        if "Docker" in flags:
            assert "Hub distribution: docker" in result.stdout
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


@pytest.mark.skipif(os.name != "nt", reason="Windows installer")
def test_windows_relative_uninstall_resolves_before_checkout_change(tmp_path):
    python = tmp_path / "runtime/venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    capture = tmp_path / "uninstall-args.json"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "function Invoke-FakePython { "
        f"ConvertTo-Json -InputObject @($args) | Set-Content -LiteralPath {hub.ps_quote(str(capture))}; "
        "$global:LASTEXITCODE = 23 }; "
        f"Set-Alias -Name {hub.ps_quote(str(python))} -Value Invoke-FakePython; "
        "function Get-ScheduledTask { throw 'must preserve daemon tasks' }; "
        "function Get-CimInstance { throw 'must preserve daemon processes' }; "
        f"& {hub.ps_quote(str(ROOT / 'install/uninstall.ps1'))} -InstallDir './runtime'"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "Hub release/removal failed" in result.stderr
    args = json.loads(capture.read_text(encoding="utf-8-sig"))
    assert Path(args[args.index("--install-dir") + 1]) == tmp_path / "runtime"
    assert Path(args[args.index("--config-dir") + 1]) == tmp_path / "runtime/config"


def test_main_errors_and_decline(tmp_path, monkeypatch, capsys):
    base = ["install", "--install-dir", str(tmp_path), "--config-dir", str(tmp_path / "config")]
    assert hub.main([*base, "--no-hub"]) == 0
    monkeypatch.setattr(hub, "install", lambda _: (_ for _ in ()).throw(ValueError("stub failure")))
    assert hub.main(base) == 1
    assert "stub failure" in capsys.readouterr().err


@pytest.mark.parametrize("quote", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
def test_windows_task_paths_are_environment_data(tmp_path, monkeypatch, quote):
    import base64

    workdir = tmp_path / ("O" + quote + "Brien;Write-Output injected;" + quote)
    argv = [str(workdir / "python.exe"), "-m", "stub_hub", "--state-dir", str(workdir)]
    calls = []
    monkeypatch.setenv("HUB_ENV_SENTINEL", "inherited")
    monkeypatch.setattr(hub.sys, "platform", "win32")
    monkeypatch.setattr(hub, "command", lambda args, **kwargs: calls.append((args, kwargs)))
    hub.Service("a" * 32).start(argv, workdir, workdir / "hub.log")
    args, kwargs = calls[0]
    script = base64.b64decode(args[-1]).decode("utf-16le")
    env = kwargs["env"]
    assert str(workdir) not in script and "Write-Output injected" not in script
    assert env["HUB_EXE"] == argv[0]
    assert env["HUB_ARGS"] == subprocess.list2cmdline(argv[1:])
    assert env["HUB_DIR"] == str(workdir)
    assert env["HUB_ENV_SENTINEL"] == "inherited"
    if os.name == "nt":
        # Run the real PowerShell tokenizer with a harmless action stub.
        action = script[:script.index("$s = New-ScheduledTaskSettingsSet")]
        stub = (
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
            "function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) "
            "@{exe=$Execute; arguments=$Argument; directory=$WorkingDirectory} }; "
            + action + "$a | ConvertTo-Json -Compress"
        )
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                 base64.b64encode(stub.encode("utf-16le")).decode()],
                                env=env, capture_output=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"exe": argv[0], "arguments": env["HUB_ARGS"], "directory": str(workdir)}


@pytest.mark.parametrize("suffix", [",readonly", ',dst=/other', '"quoted'])
def test_docker_mount_rejects_csv_delimiters(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(hub.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(hub, "command", lambda *_args, **_kwargs: pytest.fail("unsafe mount must not run"))
    directory = tmp_path / ("hub" + suffix)
    with pytest.raises(ValueError, match="commas or double quotes"):
        hub.start_hub(options(tmp_path), "docker", "stub:1", "a" * 32,
                      directory, directory / "claim.json", port=8199)


@pytest.mark.parametrize("capture_stderr", [False, True])
def test_main_subprocess_failure_does_not_log_credentials(tmp_path, monkeypatch, capsys, capture_stderr):
    distribution = "https://owner:private-token@example.invalid/hub.whl"
    def fail(_):
        raise subprocess.CalledProcessError(23, [str(tmp_path / "uv.exe"), "pip", "install", distribution],
                                            stderr=distribution if capture_stderr else None)
    monkeypatch.setattr(hub, "install", fail)
    assert hub.main(["install", "--install-dir", str(tmp_path), "--config-dir", str(tmp_path / "config")]) == 1
    output = capsys.readouterr()
    assert output.err == "Hub setup failed: uv.exe exited with return code 23\n" + hub.LATER + "\n"
    assert "private-token" not in output.out + output.err


@pytest.mark.parametrize("phase", ["install", "poll", "uninstall"])
def test_installer_lifecycle_dispatches_to_claim_api(tmp_path, monkeypatch, phase):
    from harness import cli

    parser = cli._build_parser()
    requests = []
    def command(argv, **kwargs):
        # Keep the real installer command construction and CLI routing together.
        cli_args = [str(arg) for arg in argv[argv.index("harness.cli") + 1:]]
        method, path, fields = cli.admin_request(parser.parse_args(cli_args))
        requests.append((method, path))
        if method == "GET":
            assert path == "/hub-claim"  # Inventory status does not return claim state.
            response = {"claimed": phase != "uninstall", "hub": {"request_id": "pr-stub"}}
        else:
            assert path == "/hub-claim/requests/pr-stub/approve"
            assert fields["json"]["match"] == "123456"
            response = {"approved": True}
        return SimpleNamespace(stdout=json.dumps(response), stderr="")
    monkeypatch.setattr(hub, "command", command)
    args = options(tmp_path)
    if phase == "install":
        assert hub.install(args) == 0
        assert requests == [("GET", "/hub-claim")]
    elif phase == "uninstall":
        assert hub.uninstall(args) == 0
        assert requests == [("GET", "/hub-claim")]
    else:
        claim = tmp_path / "claim.json"
        claim.write_text(json.dumps({"id": "pr-stub", "match_code": "123456"}))
        hub.approve_and_wait(claim, hub.cli_env(args.config_dir, 8199))
        assert requests == [("POST", "/hub-claim/requests/pr-stub/approve"), ("GET", "/hub-claim")]
        assert not claim.exists()
