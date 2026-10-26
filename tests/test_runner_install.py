"""Issue #806: the Linux GitHub Actions runner installer (ops/github/install-runner.sh), checked by dry run."""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from tests.installer_support import BASH, ROOT

SCRIPT = ROOT / "ops" / "github" / "install-runner.sh"
WINDOWS_INSTALLER = ROOT / "ops" / "github" / "install-runner.ps1"
# GitHub's published SHA-256 of actions-runner-linux-x64-2.337.0.tar.gz (the release notes and the asset digest).
LINUX_X64_SHA256 = "70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613"


def run_install(*args: str, os_name: str = "Linux", arch: str = "x86_64") -> subprocess.CompletedProcess[str]:
    if not BASH:
        pytest.skip("bash is not installed")
    env = {**os.environ, "HARNESS_INSTALLER_OS": os_name, "HARNESS_INSTALLER_ARCH": arch}
    return subprocess.run(
        [BASH, "ops/github/install-runner.sh", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False,
    )


def test_script_has_lf_and_parses_with_bash():
    assert b"\r\n" not in SCRIPT.read_bytes()
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    if BASH:
        parsed = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        assert parsed.returncode == 0, parsed.stderr


def test_dry_run_plans_a_review_runner_as_a_systemd_service_under_a_dedicated_user():
    result = run_install("--token", "synthetic-token", "--name", "tower-review-1", "--dry-run")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "repository: dflippojr/agent-harness" in out
    assert "runner: tower-review-1 (labels: agent-harness-review;" in out
    assert "service user: github-runner" in out
    assert "install directory: /home/github-runner/runners/tower-review-1 (work: _work)" in out
    assert f"actions-runner-linux-x64-2.337.0.tar.gz, sha256 {LINUX_X64_SHA256}" in out
    assert "would run config.sh --unattended as github-runner with --labels agent-harness-review" in out
    assert "svc.sh install github-runner" in out
    # The token is used once and never printed.
    assert "synthetic-token" not in out + result.stderr


def test_dry_run_takes_a_caller_repository_label_and_install_directory():
    result = run_install(
        "--token", "t", "--repo", "dflippojr/box-office", "--labels", "box-office-review", "--name", "tower-box-office",
        "--user", "review-runner", "--install-dir", "/home/review-runner/box-office", "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert "repository: dflippojr/box-office" in result.stdout
    assert "--labels box-office-review" in result.stdout
    assert "install directory: /home/review-runner/box-office" in result.stdout
    assert "would create user review-runner if missing" in result.stdout


def test_linux_and_windows_installers_pin_the_same_runner_release():
    windows = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    linux = SCRIPT.read_text(encoding="utf-8")
    windows_version = re.search(r"\[string\]\$Version = '([0-9.]+)'", windows).group(1)
    assert f'VERSION="{windows_version}"' in linux
    assert f'SHA256="{LINUX_X64_SHA256}"' in linux


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "--token is required"),
        (("--token", "t", "--user", "root"), "must not run as root"),
        # The review's publication scan rejects home-directory paths, so a runner outside /home could leak its path.
        (("--token", "t", "--install-dir", "/opt/actions-runner"), "must be under /home"),
        (("--token", "t", "--install-dir", "/home/github-runner/../etc"), "invalid --install-dir"),
        (("--token", "t", "--labels", "review;rm -rf /"), "invalid --labels"),
        (("--token", "t", "--repo", "not a repo"), "invalid --repo"),
        (("--token", "t", "--name", "has space"), "invalid --name"),
        (("--token", "t", "--work", ".."), "invalid --work"),
        (("--token", "t", "--version", "2.400.0"), "needs the matching --sha256"),
        (("--token", "t", "--sha256", "abc"), "invalid --sha256"),
        (("--token", "t", "--bogus"), "unknown argument: --bogus"),
    ],
)
def test_invalid_arguments_are_refused_before_anything_changes(args, message):
    result = run_install(*args, "--dry-run")
    assert result.returncode != 0
    assert message in result.stderr
    assert "dry run:" not in result.stdout


@pytest.mark.parametrize(("os_name", "arch", "message"), [
    ("Darwin", "x86_64", "on Windows use ops/github/install-runner.ps1"),
    ("Linux", "aarch64", "only x86_64"),
])
def test_other_platforms_are_refused(os_name, arch, message):
    result = run_install("--token", "t", "--dry-run", os_name=os_name, arch=arch)
    assert result.returncode != 0
    assert message in result.stderr
