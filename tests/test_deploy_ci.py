"""Fail-closed contracts for the tower deployment script."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

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
