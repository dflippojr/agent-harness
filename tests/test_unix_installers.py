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
        # Exercise compatibility mode in CI; the live exit test uses Apple's Bash 3.2.
        "BASH_COMPAT": "3.2" if platform == "Darwin" else os.environ.get("BASH_COMPAT", ""),
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


def test_linux_image_edit_opt_in_is_documented_and_not_default(tmp_path):
    result = run_installer(tmp_path, "Linux", "x86_64", "--profile", "full")
    assert result.returncode == 0, result.stderr
    assert "qwen_image_edit_fp8_e4m3fn" not in result.stdout
    opted = run_installer(tmp_path, "Linux", "x86_64", "--profile", "service", "--enable-modules", "image_edit")
    assert opted.returncode == 0, opted.stderr
    assert "Qwen-Image-Edit" in opted.stdout
    assert "393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021" in opted.stdout
    assert "qwen_image_edit_fp8_e4m3fn.safetensors" in opted.stdout
    stdout = opted.stdout.replace("\\", "/")
    assert "comfy-models/diffusion_models/qwen_image_edit_fp8_e4m3fn.safetensors" in stdout
    fallback = (tmp_path / "install" / "comfy-models").as_posix()
    if fallback in stdout:
        assert f"images.models_dir {fallback}" in stdout


def test_installers_share_images_models_dir_with_daemon_and_doctor():
    from harness.config import DEFAULT_IMAGES_MODELS_DIR, ImagesConfig, resolve_images_models_dir
    from harness.images_models import models_dir as flux_models_dir
    from harness import image_edit

    default = resolve_images_models_dir(ImagesConfig())
    assert default == Path(DEFAULT_IMAGES_MODELS_DIR)
    assert image_edit.models_dir(ImagesConfig()) == default
    assert flux_models_dir(ImagesConfig()) == default

    ps1 = (ROOT / "install/install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install/install.sh").read_text(encoding="utf-8")
    assert "--images-models-dir" in ps1
    assert "--images-models-dir" in sh
    assert "DefaultImagesModelsDir = 'C:\\AI\\comfy-models'" in ps1
    assert "Join-Path $InstallDir 'comfy-models'" in ps1
    assert 'default_images_models_dir="C:/AI/comfy-models"' in sh
    assert 'models_root="$install_dir/comfy-models"' in sh
    assert "$modelsRoot 'diffusion_models\\qwen_image_edit_fp8_e4m3fn.safetensors'" in ps1
    assert 'edit_dest="$models_root/diffusion_models/qwen_image_edit_fp8_e4m3fn.safetensors"' in sh
    # Other installer-fetched artifacts (GGUF under $InstallDir/models, Docker images) do not use images.models_dir.
    assert "qwen_image_edit" in ps1
    assert "qwen_image_edit" in sh


def test_unix_hosted_provider_login_matches_isolation_contract():
    text = (ROOT / "ops/backends/login.sh").read_text(encoding="utf-8")
    for backend in ("claude", "codex", "cursor"):
        assert f"    {backend})" in text
    assert '"harness-cli-$backend"' in text
    assert '"harness-auth-$backend"' in text
    assert '"harness-egress-$backend"' in text
    assert "HTTPS_PROXY=" in text


def test_macos_launchd_has_docker_path_and_installer_waits_for_daemon():
    installer = (ROOT / "install/install.sh").read_text(encoding="utf-8")
    assert "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" in installer
    assert 'curl -fsS "http://127.0.0.1:$port/health"' in installer
    assert "daemon did not become ready within 60 seconds" in installer
    assert "HARNESS_SUPERVISED=1" in (ROOT / "install/run-daemon.sh").read_text(encoding="utf-8")
