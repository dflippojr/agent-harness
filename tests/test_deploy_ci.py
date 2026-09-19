"""Fail-closed contracts for the tower deployment script."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
COMMIT = "1" * 40


def run_deploy(
    tmp_path: Path,
    *,
    branch: str = "main",
    release_failure: bool = False,
    branch_failure: bool = False,
    dry_run_failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is not installed")

    release = tmp_path / "release"
    deploy = tmp_path / "deploy"
    (release / ".git").mkdir(parents=True)
    (deploy / ".git").mkdir(parents=True)
    python = deploy / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()

    fake_git = tmp_path / "git.cmd"
    fake_git.write_text(
        """@echo off
if "%FAKE_RELEASE_FAILURE%"=="1" if "%~3"=="rev-parse" exit /b 17
if "%FAKE_BRANCH_FAILURE%"=="1" if "%~3"=="branch" exit /b 19
if "%~3"=="rev-parse" (
  echo %FAKE_COMMIT%
  exit /b 0
)
if "%~3"=="branch" (
  if not "%FAKE_BRANCH%"=="" echo %FAKE_BRANCH%
  exit /b 0
)
if "%~3"=="status" exit /b 0
exit /b 99
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_COMMIT": COMMIT,
        "FAKE_BRANCH": branch,
        "FAKE_RELEASE_FAILURE": "1" if release_failure else "0",
        "FAKE_BRANCH_FAILURE": "1" if branch_failure else "0",
    }
    command = [
        POWERSHELL,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "ops/harness/deploy-ci.ps1"),
        "-Commit",
        COMMIT,
        "-ReleaseRoot",
        str(release),
        "-DeployDir",
        str(deploy),
        "-DryRun",
    ]
    if dry_run_failure:
        command.extend(["-DryRunFailure", dry_run_failure])
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_deploy_dry_run_accepts_clean_main_checkout(tmp_path):
    result = run_deploy(tmp_path)
    assert result.returncode == 0, output(result)
    assert f"Deployed {COMMIT}" in result.stdout
    assert "[dry run] git" in result.stdout


def test_deploy_plan_resolves_dependencies_before_stopping_and_swapping(tmp_path):
    result = run_deploy(tmp_path)
    assert result.returncode == 0, output(result)

    plan = result.stdout
    guards = plan.index("[deploy] guards passed")
    resolve = plan.index("[deploy] dependencies staged")
    stop = plan.index("[deploy] daemon stopped")
    swap = plan.index("[deploy] virtual environment swapped")
    merge = plan.index("[deploy] checkout fast-forwarded")
    start = plan.index("[deploy] daemon started and healthy")
    assert guards < resolve < stop < swap < merge < start
    assert ".venv\\Scripts\\python.exe -m pip install" not in plan
    assert ".venv.deploy-" in plan


def test_deploy_plan_rolls_back_after_post_stop_failure(tmp_path):
    result = run_deploy(tmp_path, dry_run_failure="AfterMerge")
    assert result.returncode != 0

    plan = output(result)
    failure = plan.index("simulated dry-run failure after merge")
    reset = plan.index("reset --hard", failure)
    restore = plan.index("[rollback] restored previous virtual environment", failure)
    restart = plan.index("[rollback] previous daemon started and healthy", failure)
    assert failure < reset < restore < restart
    assert "deployment failed after daemon stop; previous deployment restored" in plan


def test_deploy_plan_stops_partially_started_daemon_before_rollback(tmp_path):
    result = run_deploy(tmp_path, dry_run_failure="AfterStart")
    assert result.returncode != 0

    plan = output(result)
    failure = plan.index("simulated dry-run failure after start")
    stop = plan.index("-Mode Stop", failure)
    reset = plan.index("reset --hard", failure)
    images = plan.index("[rollback] restored previous image tags", failure)
    restart = plan.index("[rollback] previous daemon started and healthy", failure)
    assert failure < stop < reset < images < restart


def test_daemon_supervisor_honors_deployment_venv_pointer():
    supervisor = (ROOT / "ops/harness/run-daemon.ps1").read_text(encoding="utf-8")
    assert ".venv-path" in supervisor
    assert "IsPathRooted" in supervisor
    assert "Join-Path $venv 'Scripts\\python.exe'" in supervisor


def test_deploy_rejects_detached_head_with_clear_error(tmp_path):
    result = run_deploy(tmp_path, branch="")
    assert result.returncode != 0
    assert "live checkout must be on main; current branch is detached or unknown" in output(result)
    assert "null-valued expression" not in output(result)


def test_deploy_rejects_non_main_branch_with_clear_error(tmp_path):
    result = run_deploy(tmp_path, branch="infra/github-ci-cd")
    assert result.returncode != 0
    assert "live checkout must be on main, not infra/github-ci-cd" in output(result)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"release_failure": True}, "could not resolve Actions checkout HEAD (git exit code 17)"),
        ({"branch_failure": True}, "could not determine live checkout branch (git exit code 19)"),
    ],
)
def test_deploy_reports_git_failures(tmp_path, kwargs, message):
    result = run_deploy(tmp_path, **kwargs)
    assert result.returncode != 0
    assert message in output(result)
    assert "null-valued expression" not in output(result)


@pytest.fixture(scope="session")
def deploy_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is not installed")
    venv = tmp_path_factory.mktemp("deploy-venv") / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return venv


def _write_fake_deploy_commands(tmp_path: Path) -> None:
    (tmp_path / "git.cmd").write_text(
        """@echo off
if "%~3"=="rev-parse" (
  if "%~4"=="origin/main" (
    echo %FAKE_COMMIT%
  ) else if /I "%~2"=="%FAKE_RELEASE%" (
    echo %FAKE_COMMIT%
  ) else (
    type "%FAKE_CHECKOUT_STATE%"
  )
  exit /b 0
)
if "%~3"=="branch" (
  echo main
  exit /b 0
)
if "%~3"=="status" exit /b 0
if "%~3"=="fetch" exit /b 0
if "%~3"=="merge-base" exit /b 0
if "%~3"=="merge" (
  > "%FAKE_CHECKOUT_STATE%" echo %~5
  exit /b 0
)
if "%~3"=="reset" (
  > "%FAKE_CHECKOUT_STATE%" echo %~5
  exit /b 0
)
exit /b 91
""",
        encoding="utf-8",
    )
    (tmp_path / "docker.cmd").write_text(
        """@echo off
if "%~1"=="pull" exit /b 0
if "%~1"=="image" if "%~2"=="inspect" (
  if "%~5"=="agent-harness-sandbox:py312" (
    echo sha256:old-sandbox
  ) else (
    echo sha256:old-cli
  )
  exit /b 0
)
if "%~1"=="tag" (
  if "%~3"=="agent-harness-sandbox:py312" > "%FAKE_SANDBOX_STATE%" echo %~2
  if "%~3"=="agent-harness-cli:1" > "%FAKE_CLI_STATE%" echo %~2
  exit /b 0
)
exit /b 92
""",
        encoding="utf-8",
    )


def _write_deploy_wrapper(tmp_path: Path) -> Path:
    wrapper = tmp_path / "invoke-deploy.ps1"
    wrapper.write_text(
        r"""param(
    [string]$DeployScript,
    [string]$ReleaseRoot,
    [string]$DeployDir,
    [string]$Commit,
    [string]$Failure,
    [string]$StatusPath,
    [int]$UnexpectedStopCall = 0
)
$ErrorActionPreference = 'Stop'

function Read-Count([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    return [int](Get-Content -Raw -LiteralPath $Path)
}
function Write-Count([string]$Path, [int]$Value) {
    Set-Content -LiteralPath $Path -Value $Value -NoNewline
}
function Get-ScheduledTask { [pscustomobject]@{ State = (Get-Content -Raw -LiteralPath $env:FAKE_TASK_STATE) } }
function Stop-ScheduledTask {
    $count = (Read-Count $env:FAKE_STOP_COUNT) + 1
    Write-Count $env:FAKE_STOP_COUNT $count
    if ($UnexpectedStopCall -eq $count) { throw 'unexpected scheduled task stop failure' }
    $state = Get-Content -Raw -LiteralPath $env:FAKE_TASK_STATE
    if ($state -ne 'Running') { throw 'this operation is only valid on a running scheduled task' }
    Set-Content -LiteralPath $env:FAKE_TASK_STATE -Value 'Ready' -NoNewline
}
function Start-ScheduledTask {
    $count = (Read-Count $env:FAKE_START_COUNT) + 1
    Write-Count $env:FAKE_START_COUNT $count
    $state = Get-Content -Raw -LiteralPath $env:FAKE_TASK_STATE
    if ($state -eq 'Running') { throw 'task is already running' }
    Set-Content -LiteralPath $env:FAKE_TASK_STATE -Value 'Running' -NoNewline
}
function Get-CimInstance {
    param([string]$ClassName)
    $processes = @()
    foreach ($line in @(Get-Content -LiteralPath $env:FAKE_PROCESS_STATE)) {
        if (-not $line) { continue }
        $parts = $line.Split('|', 3)
        $processes += [pscustomobject]@{
            ProcessId = [int]$parts[0]
            Name = $parts[1]
            CommandLine = $parts[2]
        }
    }
    return $processes
}
function Stop-Process {
    [CmdletBinding()]
    param([int]$Id, [switch]$Force)
    $remaining = @(Get-Content -LiteralPath $env:FAKE_PROCESS_STATE | Where-Object {
        $_ -and -not $_.StartsWith("$Id|")
    })
    [System.IO.File]::WriteAllLines($env:FAKE_PROCESS_STATE, [string[]]$remaining)
}
function Start-Sleep { }
function Invoke-RestMethod { [pscustomobject]@{ ok = $true } }

$exitCode = 0
$errorMessage = ''
try {
    & $DeployScript -Commit $Commit -ReleaseRoot $ReleaseRoot -DeployDir $DeployDir `
        -Registry 'example.invalid/harness' -DryRunFailure $Failure
} catch {
    $exitCode = 1
    $errorMessage = $_.Exception.Message
    Write-Output $errorMessage
}

[ordered]@{
    task_state = [string](Get-Content -Raw -LiteralPath $env:FAKE_TASK_STATE)
    stop_calls = Read-Count $env:FAKE_STOP_COUNT
    start_calls = Read-Count $env:FAKE_START_COUNT
    remaining_processes = @(Get-Content -LiteralPath $env:FAKE_PROCESS_STATE).Count
    error = $errorMessage
} | ConvertTo-Json | Set-Content -LiteralPath $StatusPath
exit $exitCode
""",
        encoding="utf-8",
    )
    return wrapper


def run_real_rollback(
    tmp_path: Path,
    deploy_venv: Path,
    failure: str,
    *,
    unexpected_stop_call: int = 0,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], dict[str, Path]]:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is not installed")

    release = tmp_path / "release"
    deploy = tmp_path / "deploy"
    (release / ".git").mkdir(parents=True)
    (deploy / ".git").mkdir(parents=True)
    (release / "requirements.txt").write_text("# intentionally empty\n", encoding="utf-8")
    restart = release / "ops" / "harness" / "restart-daemon.ps1"
    restart.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "ops/harness/restart-daemon.ps1", restart)
    shutil.copytree(deploy_venv, deploy / ".venv")

    previous_venv = deploy / ".venv"
    pointer = deploy / ".venv-path"
    pointer.write_text(str(previous_venv), encoding="utf-8")
    previous_commit = "2" * 40
    checkout_state = tmp_path / "checkout-state.txt"
    checkout_state.write_text(previous_commit, encoding="utf-8")
    task_state = tmp_path / "task-state.txt"
    task_state.write_text("Running", encoding="utf-8")
    stop_count = tmp_path / "stop-count.txt"
    start_count = tmp_path / "start-count.txt"
    stop_count.write_text("0", encoding="utf-8")
    start_count.write_text("0", encoding="utf-8")
    process_state = tmp_path / "process-state.txt"
    process_state.write_text(
        "101|python.exe|python.exe -m harness\n"
        "102|powershell.exe|powershell.exe -File run-daemon.ps1\n",
        encoding="utf-8",
    )
    sandbox_state = tmp_path / "sandbox-state.txt"
    cli_state = tmp_path / "cli-state.txt"
    sandbox_state.write_text("sha256:old-sandbox", encoding="utf-8")
    cli_state.write_text("sha256:old-cli", encoding="utf-8")
    status_path = tmp_path / "status.json"

    _write_fake_deploy_commands(tmp_path)
    wrapper = _write_deploy_wrapper(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_COMMIT": COMMIT,
        "FAKE_RELEASE": str(release),
        "FAKE_CHECKOUT_STATE": str(checkout_state),
        "FAKE_TASK_STATE": str(task_state),
        "FAKE_STOP_COUNT": str(stop_count),
        "FAKE_START_COUNT": str(start_count),
        "FAKE_PROCESS_STATE": str(process_state),
        "FAKE_SANDBOX_STATE": str(sandbox_state),
        "FAKE_CLI_STATE": str(cli_state),
    }
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            "-DeployScript",
            str(ROOT / "ops/harness/deploy-ci.ps1"),
            "-ReleaseRoot",
            str(release),
            "-DeployDir",
            str(deploy),
            "-Commit",
            COMMIT,
            "-Failure",
            failure,
            "-StatusPath",
            str(status_path),
            "-UnexpectedStopCall",
            str(unexpected_stop_call),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    status = json.loads(status_path.read_text(encoding="utf-8-sig"))
    paths = {
        "pointer": pointer,
        "checkout": checkout_state,
        "task": task_state,
        "processes": process_state,
        "sandbox": sandbox_state,
        "cli": cli_state,
    }
    return result, status, paths


@pytest.mark.parametrize("failure", ["AfterStop", "AfterSwap", "AfterMerge", "AfterStart"])
def test_real_rollback_restores_previous_deployment(
    tmp_path, deploy_venv, failure
):
    result, status, paths = run_real_rollback(tmp_path, deploy_venv, failure)

    assert result.returncode != 0
    assert "simulated" in output(result)
    expected_calls = 2 if failure == "AfterStart" else 1
    assert status["stop_calls"] == expected_calls
    assert status["start_calls"] == expected_calls
    assert status["task_state"] == "Running"
    assert status["remaining_processes"] == 0
    assert paths["pointer"].read_text(encoding="utf-8") == str(paths["pointer"].parent / ".venv")
    assert paths["checkout"].read_text(encoding="utf-8").strip() == "2" * 40
    assert paths["sandbox"].read_text(encoding="utf-8").strip() == "sha256:old-sandbox"
    assert paths["cli"].read_text(encoding="utf-8").strip() == "sha256:old-cli"


def test_rollback_continues_after_unexpected_daemon_stop_error(tmp_path, deploy_venv):
    result, status, paths = run_real_rollback(
        tmp_path, deploy_venv, "AfterStart", unexpected_stop_call=2
    )

    assert result.returncode != 0
    assert "rollback also failed" in output(result)
    assert "unexpected scheduled task stop failure" in output(result)
    assert status["task_state"] == "Running"
    assert status["stop_calls"] == 2
    assert status["start_calls"] == 1
    assert status["remaining_processes"] == 0
    assert paths["pointer"].read_text(encoding="utf-8") == str(paths["pointer"].parent / ".venv")
    assert paths["checkout"].read_text(encoding="utf-8").strip() == "2" * 40
    assert paths["sandbox"].read_text(encoding="utf-8").strip() == "sha256:old-sandbox"
    assert paths["cli"].read_text(encoding="utf-8").strip() == "sha256:old-cli"
