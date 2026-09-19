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
    return subprocess.run(
        [
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
        ],
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
