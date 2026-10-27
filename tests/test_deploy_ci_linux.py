"""The tower deployment script's Linux path (#812): pwsh 7, systemd, and the installer's supervisor.

tests/test_deploy_ci.py covers the Windows path with Windows PowerShell; these run the same script under pwsh on
Linux with fake git, docker, sudo and systemctl commands on PATH.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
PWSH = shutil.which("pwsh") if sys.platform.startswith("linux") else None
COMMIT = "1" * 40
PREVIOUS_COMMIT = "2" * 40
STABLES_COMMIT = "3" * 40
UNIT = "agent-harness-main-daemon.service"
OLD_SUPERVISOR = "#!/usr/bin/env bash\n# the supervisor an older install left\n"

pytestmark = pytest.mark.skipif(PWSH is None, reason="needs pwsh on Linux")

FAKE_GIT = r"""#!/usr/bin/env bash
if [[ $2 == "$FAKE_STABLES" && $3 == rev-parse ]]; then echo "$FAKE_STABLES_COMMIT"; exit 0; fi
case $3 in
    rev-parse)
        if [[ $4 == origin/main || $2 == "$FAKE_RELEASE" ]]; then echo "$FAKE_COMMIT"; else cat "$FAKE_CHECKOUT_STATE"; fi ;;
    branch) [[ -n $FAKE_BRANCH ]] && echo "$FAKE_BRANCH" ;;
    status|fetch|merge-base) ;;
    merge|reset) echo "$5" >"$FAKE_CHECKOUT_STATE" ;;
    *) exit 91 ;;
esac
exit 0
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
case $1 in
    pull) ;;
    image) if [[ $5 == agent-harness-sandbox:py312 ]]; then echo sha256:old-sandbox; else echo sha256:old-cli; fi ;;
    tag)
        [[ $3 == agent-harness-sandbox:py312 ]] && echo "$2" >"$FAKE_SANDBOX_STATE"
        [[ $3 == agent-harness-cli:1 ]] && echo "$2" >"$FAKE_CLI_STATE" ;;
    *) exit 92 ;;
esac
exit 0
"""

# Read-only `systemctl show`; the deploy changes the unit's state only through `sudo -n systemctl`.
FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
if [[ $1 == show && $2 == --property=ExecStart && $3 == --value ]]; then echo "$FAKE_EXEC_START"; exit 0; fi
exit 93
"""

FAKE_SUDO = r"""#!/usr/bin/env bash
[[ $1 == -n ]] || exit 94
shift
if [[ $1 == -l ]]; then [[ $FAKE_SUDO_DENY == 1 ]] && exit 1; echo "/usr/bin/$2"; exit 0; fi
[[ $1 == systemctl && $3 == "$FAKE_UNIT" ]] || exit 95
echo "$2" >>"$FAKE_SUDO_LOG"
calls=$(grep -c "^$2\$" "$FAKE_SUDO_LOG")
if [[ $2 == stop ]]; then
    [[ $FAKE_FAIL_STOP_CALL == "$calls" ]] && { echo "unexpected systemctl stop failure" >&2; exit 1; }
    echo inactive >"$FAKE_UNIT_STATE"
elif [[ $2 == start ]]; then
    echo active >"$FAKE_UNIT_STATE"
else
    exit 96
fi
exit 0
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _layout(tmp_path: Path, *, python: Path | None = None, exec_start: str | None = None) -> dict[str, object]:
    """A release checkout, a live checkout, an installer layout, and fake commands on PATH."""
    release = tmp_path / "release"
    deploy = tmp_path / "agent-harness"
    install = tmp_path / "install"
    (release / ".git").mkdir(parents=True)
    (deploy / ".git").mkdir(parents=True)
    (release / "requirements.txt").write_text("# intentionally empty\n", encoding="utf-8")
    (release / "install").mkdir()
    shutil.copy2(ROOT / "install/run-daemon.sh", release / "install/run-daemon.sh")
    (release / "tools" / "stables").mkdir(parents=True)
    (release / "tools" / "stables" / "pin.json").write_text(json.dumps(
        {"repository": "dflippojr/stables", "tag": "v9.9.9", "commit": STABLES_COMMIT}), encoding="utf-8")
    stables = release / "stables-release"
    (stables / "public").mkdir(parents=True)
    (stables / "public" / "index.html").write_text("<!doctype html><title>Stables</title>\n", encoding="utf-8")

    supervisor = install / "bin" / "run-daemon.sh"
    supervisor.parent.mkdir(parents=True)
    _executable(supervisor, OLD_SUPERVISOR)
    if python is None:
        python = install / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.touch()

    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    _executable(bin_dir / "git", FAKE_GIT)
    _executable(bin_dir / "docker", FAKE_DOCKER)
    _executable(bin_dir / "systemctl", FAKE_SYSTEMCTL)
    _executable(bin_dir / "sudo", FAKE_SUDO)

    state = {name: tmp_path / f"{name}.txt" for name in ("checkout", "unit", "sudo_log", "sandbox", "cli")}
    state["checkout"].write_text(PREVIOUS_COMMIT, encoding="utf-8")
    state["unit"].write_text("active", encoding="utf-8")
    state["sudo_log"].write_text("", encoding="utf-8")
    state["sandbox"].write_text("sha256:old-sandbox", encoding="utf-8")
    state["cli"].write_text("sha256:old-cli", encoding="utf-8")
    if exec_start is None:
        exec_start = (
            f"{{ path=/bin/bash ; argv[]=/bin/bash {supervisor} {deploy} {install}/config {python} {install}/logs ;"
            " ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
        )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_COMMIT": COMMIT,
        "FAKE_BRANCH": "main",
        "FAKE_RELEASE": str(release),
        "FAKE_STABLES": str(stables),
        "FAKE_STABLES_COMMIT": STABLES_COMMIT,
        "FAKE_CHECKOUT_STATE": str(state["checkout"]),
        "FAKE_UNIT": UNIT,
        "FAKE_UNIT_STATE": str(state["unit"]),
        "FAKE_SUDO_LOG": str(state["sudo_log"]),
        "FAKE_SANDBOX_STATE": str(state["sandbox"]),
        "FAKE_CLI_STATE": str(state["cli"]),
        "FAKE_EXEC_START": exec_start,
        "FAKE_SUDO_DENY": "0",
        "FAKE_FAIL_STOP_CALL": "0",
    }
    return {
        "release": release, "deploy": deploy, "install": install, "supervisor": supervisor,
        "stables": stables, "env": env, "state": state,
    }


def _deploy_arguments(layout: dict[str, object]) -> list[str]:
    return [
        "-Commit", COMMIT,
        "-ReleaseRoot", str(layout["release"]),
        "-DeployDir", str(layout["deploy"]),
        "-InstallDir", str(layout["install"]),
        "-StablesRelease", str(layout["stables"]),
        "-Registry", "example.invalid/harness",
    ]


def _deploy_object(layout: dict[str, object]) -> dict[str, str]:
    arguments = _deploy_arguments(layout)
    return {name.lstrip("-"): value for name, value in zip(arguments[::2], arguments[1::2])}


def dry_run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    layout = _layout(tmp_path)
    layout["env"].update(env)
    result = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "ops/harness/deploy-ci.ps1"),
         *_deploy_arguments(layout), "-DryRun"],
        cwd=ROOT, env=layout["env"], capture_output=True, text=True, timeout=60, check=False,
    )
    return result, layout


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_linux_dry_run_plans_systemd_steps_against_linux_paths_without_restarting(tmp_path):
    result, layout = dry_run(tmp_path)
    assert result.returncode == 0, output(result)

    plan = result.stdout
    assert f"Deployed {COMMIT}" in plan
    guards = plan.index("[deploy] guards passed")
    unit = plan.index(f"[deploy] {UNIT} runs {layout['supervisor']}")
    verified = plan.index("[deploy] Stables v9.9.9 verified")
    resolve = plan.index("[deploy] dependencies staged")
    staged = plan.index("[deploy] Stables staged")
    stop = plan.index(f"[dry run] sudo -n systemctl stop {UNIT}")
    stopped = plan.index("[deploy] daemon stopped")
    swap = plan.index("[deploy] virtual environment swapped")
    selected = plan.index("[deploy] Stables v9.9.9 selected")
    supervisor = plan.index("[deploy] supervisor updated")
    merge = plan.index("[deploy] checkout fast-forwarded")
    start = plan.index(f"[dry run] sudo -n systemctl start {UNIT}")
    started = plan.index("[deploy] daemon started and healthy")
    assert guards < unit < verified < resolve < staged < stop < stopped < swap < selected < supervisor < merge
    assert merge < start < started

    # The installer's environment builds the side-by-side one, which holds the release's requirements.
    install = layout["install"]
    staged_venv = tmp_path / f".agent-harness.venv.deploy-{COMMIT}"
    assert f"[dry run] {install}/venv/bin/python -m venv {staged_venv}" in plan
    assert f"[dry run] {staged_venv}/bin/python -m pip install" in plan
    assert f"{layout['release']}/requirements.txt" in plan
    assert f"{layout['deploy']}/.venv-path -> {staged_venv}" in plan
    assert f".agent-harness.stables-{STABLES_COMMIT}/public" in plan[staged:selected]
    assert f"{layout['release']}/install/run-daemon.sh -> {layout['supervisor']}" in plan[selected:supervisor]
    assert f"[dry run] docker tag example.invalid/harness/agent-harness-sandbox:sha-{COMMIT}" in plan

    # No Windows paths, tools or services on the Linux path.
    for windows in ("\\", "Scripts", "python.exe", "powershell.exe", "D:", "ScheduledTask", "-Mode Stop"):
        assert windows not in plan, windows
    # A dry run changes nothing: it never stops or starts the unit and writes nothing.
    assert layout["state"]["sudo_log"].read_text(encoding="utf-8") == ""
    assert layout["state"]["unit"].read_text(encoding="utf-8") == "active"
    assert layout["supervisor"].read_text(encoding="utf-8") == OLD_SUPERVISOR
    assert not (layout["deploy"] / ".venv-path").exists()
    assert not staged_venv.exists()


def test_linux_deploy_refuses_a_unit_that_does_not_run_the_installed_supervisor(tmp_path):
    result, layout = dry_run(tmp_path, FAKE_EXEC_START="{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -m harness }")
    assert result.returncode != 0
    message = " ".join(output(result).split())
    assert f"{UNIT} does not run {layout['supervisor']} for {layout['deploy']}" in message
    assert "[deploy] dependencies staged" not in output(result)


def test_linux_deploy_refuses_without_passwordless_systemctl(tmp_path):
    result, _ = dry_run(tmp_path, FAKE_SUDO_DENY="1")
    assert result.returncode != 0
    message = " ".join(output(result).split())
    assert f"this user may not run sudo -n systemctl stop {UNIT}" in message
    assert "[deploy] dependencies staged" not in output(result)


@pytest.fixture(scope="session")
def deploy_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    venv = tmp_path_factory.mktemp("deploy-venv") / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, text=True, timeout=120)
    return venv


WRAPPER = r"""param([string]$DeployScript, [string]$StatusPath, [string]$Failure, [string]$DeployArgumentsJson)
$ErrorActionPreference = 'Stop'
# The health poll and its waits; the unit's state comes from the fake sudo.
function Start-Sleep { }
function Invoke-RestMethod {
    [pscustomobject]@{ ok = ((Get-Content -Raw -LiteralPath $env:FAKE_UNIT_STATE).Trim() -eq 'active') }
}
$exitCode = 0
$errorMessage = ''
$arguments = @{}
(ConvertFrom-Json $DeployArgumentsJson).psobject.Properties | ForEach-Object { $arguments[$_.Name] = $_.Value }
try {
    & $DeployScript @arguments -DryRunFailure $Failure
} catch {
    $exitCode = 1
    $errorMessage = $_.Exception.Message
    Write-Output $errorMessage
}
[ordered]@{
    unit_state = (Get-Content -Raw -LiteralPath $env:FAKE_UNIT_STATE).Trim()
    systemctl = @(Get-Content -LiteralPath $env:FAKE_SUDO_LOG | Where-Object { $_ })
    error = $errorMessage
} | ConvertTo-Json | Set-Content -LiteralPath $StatusPath
exit $exitCode
"""


def real_deploy(
    tmp_path: Path, deploy_venv: Path, failure: str, *, fail_stop_call: int = 0
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], dict[str, object]]:
    venv = tmp_path / "install" / "venv"
    shutil.copytree(deploy_venv, venv, symlinks=True)
    layout = _layout(tmp_path, python=venv / "bin" / "python")
    layout["env"]["FAKE_FAIL_STOP_CALL"] = str(fail_stop_call)
    old_stables = tmp_path / f".agent-harness.stables-{'5' * 40}" / "public"
    old_stables.mkdir(parents=True)
    (old_stables / "index.html").write_text("old Stables", encoding="utf-8")
    (layout["deploy"] / ".stables-path").write_text(str(old_stables), encoding="utf-8")
    layout["old_stables"] = old_stables
    layout["staged_stables"] = tmp_path / f".agent-harness.stables-{STABLES_COMMIT}"
    layout["staged_venv"] = tmp_path / f".agent-harness.venv.deploy-{COMMIT}"

    wrapper = tmp_path / "invoke-deploy.ps1"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    status_path = tmp_path / "status.json"
    result = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper),
         "-DeployScript", str(ROOT / "ops/harness/deploy-ci.ps1"), "-StatusPath", str(status_path),
         "-Failure", failure, "-DeployArgumentsJson", json.dumps(_deploy_object(layout))],
        cwd=ROOT, env=layout["env"], capture_output=True, text=True, timeout=180, check=False,
    )
    status = json.loads(status_path.read_text(encoding="utf-8-sig"))
    return result, status, layout


def test_linux_real_deploy_switches_environment_stables_and_supervisor(tmp_path, deploy_venv):
    result, status, layout = real_deploy(tmp_path, deploy_venv, "None")

    assert result.returncode == 0, output(result)
    assert status["systemctl"] == ["stop", "start"]
    assert status["unit_state"] == "active"
    assert "daemon up after 2 s" in output(result)
    assert (layout["deploy"] / ".venv-path").read_text(encoding="utf-8") == str(layout["staged_venv"])
    assert (layout["staged_venv"] / "bin" / "python").exists()
    staged = layout["staged_stables"] / "public"
    assert (layout["deploy"] / ".stables-path").read_text(encoding="utf-8") == str(staged)
    assert (staged / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not layout["old_stables"].parent.exists()
    assert layout["supervisor"].read_bytes() == (ROOT / "install/run-daemon.sh").read_bytes()
    assert os.access(layout["supervisor"], os.X_OK)
    assert layout["state"]["checkout"].read_text(encoding="utf-8").strip() == COMMIT
    assert layout["state"]["sandbox"].read_text(encoding="utf-8").strip().endswith(f"agent-harness-sandbox:sha-{COMMIT}")
    # The installer's environment is never a managed deployment environment, so it stays for rollback.
    assert (layout["install"] / "venv" / "bin" / "python").exists()


@pytest.mark.parametrize("failure", ["AfterStop", "AfterSwap", "AfterMerge", "AfterStart"])
def test_linux_real_rollback_restores_previous_deployment(tmp_path, deploy_venv, failure):
    result, status, layout = real_deploy(tmp_path, deploy_venv, failure)

    assert result.returncode != 0
    assert "simulated" in output(result)
    assert "deployment failed after daemon stop; previous deployment restored" in output(result)
    # The rollback always stops the unit first (a no-op when it is already inactive), then starts the old daemon.
    deployed = ["stop", "start"] if failure == "AfterStart" else ["stop"]
    assert status["systemctl"] == [*deployed, "stop", "start"]
    assert status["unit_state"] == "active"
    # The first deployment had no pointer: the installer's environment stays the daemon's.
    assert not (layout["deploy"] / ".venv-path").exists()
    assert not layout["staged_venv"].exists()
    assert (layout["deploy"] / ".stables-path").read_text(encoding="utf-8") == str(layout["old_stables"])
    assert (layout["old_stables"] / "index.html").is_file()
    assert not layout["staged_stables"].exists()
    assert layout["supervisor"].read_text(encoding="utf-8") == OLD_SUPERVISOR
    assert layout["state"]["checkout"].read_text(encoding="utf-8").strip() == PREVIOUS_COMMIT
    assert layout["state"]["sandbox"].read_text(encoding="utf-8").strip() == "sha256:old-sandbox"
    assert layout["state"]["cli"].read_text(encoding="utf-8").strip() == "sha256:old-cli"


def test_linux_rollback_continues_after_a_failed_systemctl_stop(tmp_path, deploy_venv):
    result, status, layout = real_deploy(tmp_path, deploy_venv, "AfterStart", fail_stop_call=2)

    assert result.returncode != 0
    assert "rollback also failed" in output(result)
    assert "daemon stop:" in " ".join(output(result).split())
    assert status["unit_state"] == "active"
    assert status["systemctl"] == ["stop", "start", "stop", "start"]
    assert layout["supervisor"].read_text(encoding="utf-8") == OLD_SUPERVISOR
    assert layout["state"]["checkout"].read_text(encoding="utf-8").strip() == PREVIOUS_COMMIT
    assert layout["state"]["sandbox"].read_text(encoding="utf-8").strip() == "sha256:old-sandbox"


# install/run-daemon.sh: the systemd unit's supervisor reads the pointers a deployment writes.
FAKE_DAEMON_PYTHON = r"""#!/usr/bin/env bash
{
    echo "python=$0"
    echo "args=$*"
    echo "supervised=${HARNESS_SUPERVISED-}"
    echo "build=${HARNESS_BUILD_COMMIT-<unset>}"
    echo "stables=${HARNESS_STABLES_DIR-<unset>}"
} >"$FAKE_DAEMON_ENV"
kill -TERM "$PPID"
"""


def run_supervisor(tmp_path: Path, *, venv_pointer: str | None, stables_pointer: str | None):
    app = tmp_path / "app dir"
    app.mkdir()
    installer_python = tmp_path / "installer venv" / "bin" / "python"
    deployed_venv = tmp_path / f".agent-harness.venv.deploy-{'AB' * 20}"
    for python in (installer_python, deployed_venv / "bin" / "python"):
        python.parent.mkdir(parents=True)
        _executable(python, FAKE_DAEMON_PYTHON)
    if venv_pointer is not None:
        (app / ".venv-path").write_text(venv_pointer.format(deployed=deployed_venv), encoding="utf-8")
    if stables_pointer is not None:
        (app / ".stables-path").write_text(stables_pointer, encoding="utf-8")
    env_file = tmp_path / "daemon-env.txt"
    env = {key: value for key, value in os.environ.items() if not key.startswith("HARNESS_")}
    env["FAKE_DAEMON_ENV"] = str(env_file)
    result = subprocess.run(
        ["bash", str(ROOT / "install/run-daemon.sh"), str(app), str(tmp_path / "config"), str(installer_python),
         str(tmp_path / "logs")],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    seen = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines()) \
        if env_file.exists() else {}
    return result, seen, installer_python, deployed_venv


@pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="needs bash on Unix")
def test_supervisor_runs_the_deployed_environment_and_stables(tmp_path):
    result, seen, _, deployed = run_supervisor(
        tmp_path, venv_pointer="{deployed}\n", stables_pointer="/srv/stables copy/public\n")
    assert result.returncode == 0, result.stderr
    assert seen["python"] == str(deployed / "bin" / "python")
    assert seen["args"] == "-u -m harness"
    assert seen["supervised"] == "1"
    assert seen["build"] == "ab" * 20
    assert seen["stables"] == "/srv/stables copy/public"


@pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="needs bash on Unix")
def test_supervisor_without_pointers_runs_the_installer_environment(tmp_path):
    result, seen, installer_python, _ = run_supervisor(tmp_path, venv_pointer=None, stables_pointer=None)
    assert result.returncode == 0, result.stderr
    assert seen["python"] == str(installer_python)
    assert seen["build"] == "<unset>"
    assert seen["stables"] == "<unset>"


@pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="needs bash on Unix")
@pytest.mark.parametrize(("venv", "stables", "message"), [
    ("relative/venv", None, "invalid deployment virtual environment pointer"),
    (None, "relative/public", "invalid Stables pointer"),
])
def test_supervisor_refuses_relative_pointers(tmp_path, venv, stables, message):
    result, seen, _, _ = run_supervisor(tmp_path, venv_pointer=venv, stables_pointer=stables)
    assert result.returncode != 0
    assert message in result.stderr
    assert seen == {}
