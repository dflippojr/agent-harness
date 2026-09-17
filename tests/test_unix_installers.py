"""Issue #15: Linux/NVIDIA and Apple Silicon installer contracts."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
GIT_BASH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.exists() else shutil.which("bash")


def run_installer(tmp_path: Path, platform: str, arch: str, *args: str) -> subprocess.CompletedProcess[str]:
    if not BASH:
        pytest.skip("bash is not installed")
    env = {
        **os.environ,
        "HARNESS_INSTALLER_OS": platform,
        "HARNESS_INSTALLER_ARCH": arch,
    }
    return subprocess.run(
        [BASH, "install/install.sh", "--install-dir", str(tmp_path / "install"), "--dry-run", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_linux_nvidia_full_profile_dry_run(tmp_path):
    result = run_installer(tmp_path, "Linux", "x86_64", "--profile", "full")
    assert result.returncode == 0, result.stderr
    assert "platform: linux, profile: full" in result.stdout
    assert "local model: qwen3.6-35b-a3b" in result.stdout
    assert "server-cuda-b10830" in result.stdout
    assert "NVIDIA Container Toolkit" in result.stdout
    assert "systemd --user units" in result.stdout


def test_apple_silicon_service_profile_dry_run(tmp_path):
    result = run_installer(tmp_path, "Darwin", "arm64", "--profile", "service")
    assert result.returncode == 0, result.stderr
    assert "platform: macos, profile: service" in result.stdout
    assert "hosted-provider service profile" in result.stdout
    assert "provider allowlist proxies" in result.stdout
    assert "launchd agent" in result.stdout
    assert "ops/backends/login.sh" in result.stdout


def test_apple_silicon_rejects_local_model(tmp_path):
    result = run_installer(tmp_path, "Darwin", "arm64", "--profile", "full")
    assert result.returncode != 0
    assert "hosted-provider service profile only" in result.stderr

    result = run_installer(
        tmp_path, "Darwin", "arm64", "--profile", "service", "--enable-modules", "jobs,local_model"
    )
    assert result.returncode != 0
    assert "local_model is unavailable" in result.stderr


def test_unix_scripts_have_lf_and_parse_with_bash():
    scripts = [
        ROOT / "install/install.sh",
        ROOT / "install/uninstall.sh",
        ROOT / "install/run-daemon.sh",
        ROOT / "install/run-server.sh",
        ROOT / "ops/backends/login.sh",
    ]
    for script in scripts:
        assert b"\r\n" not in script.read_bytes(), script
        assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
        if BASH:
            parsed = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)
            assert parsed.returncode == 0, f"{script}: {parsed.stderr}"


def test_linux_model_supervisor_is_gpu_isolated_and_pinned():
    text = (ROOT / "install/run-server.sh").read_text(encoding="utf-8")
    installer = (ROOT / "install/install.sh").read_text(encoding="utf-8")
    assert "--gpus all" in text
    assert "--network host" in text
    assert ":/models/model.gguf:ro" in text
    assert "server-cuda-$LLAMA_BUILD" in installer
    assert "LLAMA_BUILD=b10830" in installer


def test_unix_hosted_provider_login_matches_isolation_contract():
    text = (ROOT / "ops/backends/login.sh").read_text(encoding="utf-8")
    for backend in ("claude", "codex", "cursor"):
        assert f"    {backend})" in text
    assert '"harness-cli-$backend"' in text
    assert '"harness-auth-$backend"' in text
    assert '"harness-egress-$backend"' in text
    assert "HTTPS_PROXY=" in text
