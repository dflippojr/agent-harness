"""Issue #15: Linux/NVIDIA and Apple Silicon installer contracts."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from tests.installer_support import BASH, ROOT, run_installer


def test_linux_nvidia_full_profile_dry_run(tmp_path):
    result = run_installer(tmp_path, "Linux", "x86_64", "--profile", "full")
    assert result.returncode == 0, result.stderr
    assert "platform: linux, profile: full" in result.stdout
    assert "local model: qwen3.6-35b-a3b" in result.stdout
    assert "server-cuda-b10830" in result.stdout
    assert "NVIDIA Container Toolkit" in result.stdout
    assert "systemd --user units" in result.stdout
    assert "qwen_image_edit" not in result.stdout  # the image-edit model is opt-in, never part of the full profile


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
    from harness_modules.images.models import models_dir as flux_models_dir
    from harness_modules.images import edit as image_edit

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
    assert "$modelsRoot 'diffusion_models\\qwen_image_edit_fp8_e4m3fn.safetensors'" in ps1
    # Other installer-fetched artifacts (GGUF under $InstallDir/models, Docker images) do not use images.models_dir.
    assert "qwen_image_edit" in ps1
    assert "qwen_image_edit" in sh


def test_unix_hosted_provider_login_matches_isolation_contract():
    text = (ROOT / "ops/backends/login.sh").read_text(encoding="utf-8")
    for backend in ("claude", "codex", "cursor"):
        assert f"    {backend})" in text
    assert '"harness-cli-$backend"' in text
    # Claude and Cursor share one login; Codex's is the owner's state volume or one App's own (#371).
    assert 'volume="harness-login-$backend"' in text
    assert "volume=harness-auth-codex" in text
    assert 'volume="harness-cli-codex-app-$app"' in text
    assert '"harness-egress-$backend"' in text
    assert "HTTPS_PROXY=" in text


def test_macos_launchd_has_docker_path_and_installer_waits_for_daemon():
    installer = (ROOT / "install/install.sh").read_text(encoding="utf-8")
    assert "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" in installer
    assert 'curl -fsS "http://127.0.0.1:$port/health"' in installer
    assert "daemon did not become ready within 60 seconds" in installer
    assert "HARNESS_SUPERVISED=1" in (ROOT / "install/run-daemon.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("args", [("--with-hub",), ("--no-hub",), ()])
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



def test_relative_uninstall_from_outside_checkout_keeps_daemon_on_hub_failure(tmp_path):
    if not BASH:
        pytest.skip("bash is not installed")
    python = tmp_path / "runtime/venv/bin/python"
    python.parent.mkdir(parents=True)
    args_file = tmp_path / "hub-uninstall-args"
    python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > ' + shlex.quote(args_file.as_posix()) + '\nexit 23\n')
    python.chmod(0o755)
    result = subprocess.run([BASH, str(ROOT / "install/uninstall.sh"), "--install-dir", "./runtime"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 23, result.stderr
    # The stub runs in the checkout; arguments must still name the caller's install.
    args = args_file.read_text().splitlines()
    directory = args[args.index("--install-dir") + 1]
    assert directory.startswith("/") and directory.endswith("/runtime")
    assert args[args.index("--config-dir") + 1] == directory + "/config"


@pytest.mark.parametrize("directory", ["", "/", "$HOME", "$HOME/.", "."])
def test_uninstall_refuses_unsafe_directories_before_cleanup(tmp_path, directory):
    if not BASH:
        pytest.skip("bash is not installed")
    installer = shlex.quote((ROOT / "install/uninstall.sh").as_posix())
    target = '"' + directory + '"'
    script = (
        'mkdir -p home; cd home; export HOME="$(pwd -P)"; '
        'systemctl() { echo unexpected-service-cleanup >&2; return 99; }; export -f systemctl; '
        + f'{shlex.quote(BASH)} {installer} --install-dir {target} --remove-files'
    )
    result = subprocess.run([BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "install directory" in result.stderr
    assert "unexpected-service-cleanup" not in result.stderr
    assert (tmp_path / "home").is_dir()


@pytest.mark.parametrize("directory", ["$HOME", "$PWD/real-home", ".", "$PWD/alias-home"])
def test_uninstall_refuses_physical_and_symlinked_home(tmp_path, directory):
    if not BASH:
        pytest.skip("bash is not installed")
    installer = shlex.quote((ROOT / "install/uninstall.sh").as_posix())
    # nativestrict prevents Git Bash from emulating a symlink by copying its target.
    script = (
        'export MSYS=winsymlinks:nativestrict; mkdir real-home; '
        'ln -s real-home logical-home && ln -s real-home alias-home || exit 77; '
        '[[ -L logical-home && -L alias-home ]] || exit 77; '
        'export HOME="$PWD/logical-home"; '
        'systemctl() { echo unexpected-service-cleanup >&2; return 99; }; export -f systemctl; '
    )
    if directory == ".":
        script += 'cd real-home; '
    script += f'{shlex.quote(BASH)} {installer} --install-dir "{directory}" --remove-files'
    result = subprocess.run([BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    if result.returncode == 77:
        pytest.skip("native directory symlinks are unavailable")
    assert result.returncode != 0 and "refusing unsafe install directory" in result.stderr
    assert "unexpected-service-cleanup" not in result.stderr
    assert (tmp_path / "real-home").is_dir()


@pytest.mark.parametrize("physical", [False, True])
def test_uninstall_removes_install_symlink_without_deleting_target(tmp_path, physical):
    if not BASH:
        pytest.skip("bash is not installed")
    installer = shlex.quote((ROOT / "install/uninstall.sh").as_posix())
    script = (
        'export MSYS=winsymlinks:nativestrict; mkdir home real-install; '
        'echo keep > real-install/models; ln -s real-install runtime || exit 77; '
        '[[ -L runtime ]] || exit 77; export HOME="$PWD/home" XDG_CONFIG_HOME="$PWD/home/config"; '
        'uname() { echo Linux; }; systemctl() { return 0; }; export -f uname systemctl; '
        + ('set -o physical; export SHELLOPTS; ' if physical else '')
        + f'{shlex.quote(BASH)} {installer} --install-dir ./runtime --remove-files'
    )
    result = subprocess.run([BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    if result.returncode == 77:
        pytest.skip("native directory symlinks are unavailable")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "runtime").exists()
    assert (tmp_path / "real-install/models").read_text().strip() == "keep"


def _install_linux_units(tmp_path: Path, app_dir: Path, *installer_args: str,
                         profile: str = "service") -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run install.sh for real from a copy at `app_dir`, with every tool and service call stubbed (#810)."""
    if not BASH or os.name == "nt":
        pytest.skip("needs bash on a POSIX host")
    (app_dir / "install").mkdir(parents=True)
    for script in ("install.sh", "run-daemon.sh", "run-server.sh"):
        shutil.copy2(ROOT / "install" / script, app_dir / "install" / script)
    install_dir = app_dir / "runtime"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls"
    for tool in ("git", "docker", "curl", "tar", "systemctl"):
        (stubs / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> {shlex.quote(str(calls))}\nexit 0\n')
    (stubs / "nvidia-smi").write_text('#!/bin/sh\necho "Stub RTX, 560.35, 16376"\n')
    (stubs / "df").write_text('#!/bin/sh\necho "Filesystem 1K-blocks Used Available Capacity Mounted"\n'
                              'echo "stub 209715200 0 104857600 0% /"\n')
    for tool in (install_dir / "bin/uv", install_dir / "venv/bin/python"):
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("#!/bin/sh\nexit 0\n")
    for stub in [*stubs.iterdir(), install_dir / "bin/uv", install_dir / "venv/bin/python"]:
        stub.chmod(0o755)
    home = tmp_path / "home"
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "HARNESS_INSTALLER_OS": "Linux", "HARNESS_INSTALLER_ARCH": "x86_64"}
    result = subprocess.run(
        [BASH, str(app_dir / "install/install.sh"), "--install-dir", str(install_dir), "--profile", profile,
         "--no-hub", *installer_args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
    enabled = ("agent-harness-main-llama.service " if profile == "full" else "") + "agent-harness-main-daemon.service"
    assert calls.exists() and f"systemctl --user enable --now {enabled}" in calls.read_text(), (
        result.stdout + result.stderr)
    return result, home / ".config/systemd/user/agent-harness-main-daemon.service", install_dir


def _llama_unit_args(tmp_path: Path, name: str, *installer_args: str) -> list[str]:
    """Install the full profile from a scratch copy and return what the llama unit passes after run-server.sh's seven
    positional arguments: the model's own llama-server arguments."""
    root = tmp_path / name
    root.mkdir()
    model = root / "model.gguf"
    model.write_text("stub")
    result, daemon_unit, _ = _install_linux_units(root, root / "app", "--model-path", str(model), *installer_args,
                                                  profile="full")
    assert result.returncode == 0, result.stdout + result.stderr
    unit = daemon_unit.with_name("agent-harness-main-llama.service")
    lines = dict(line.split("=", 1) for line in unit.read_text().splitlines() if "=" in line)
    exec_args = shlex.split(lines["ExecStart"].replace("%%", "%"))
    assert exec_args[1].endswith("/bin/run-server.sh")
    return exec_args[9:]


QWEN_DEFAULT_ARGS = ["--fit", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]


def test_vram_headroom_adds_fit_target_to_the_qwen_unit(tmp_path):
    args = _llama_unit_args(tmp_path, "headroom", "--model", "qwen", "--vram-headroom-mib", "2048")
    assert args == [*QWEN_DEFAULT_ARGS, "--fit-target", "2048"]


@pytest.mark.parametrize("flag", [(), ("--vram-headroom-mib", "0")], ids=["left-out", "zero"])
def test_no_vram_headroom_leaves_the_qwen_unit_unchanged(tmp_path, flag):
    assert _llama_unit_args(tmp_path, "default", "--model", "qwen", *flag) == QWEN_DEFAULT_ARGS


def test_vram_headroom_does_not_reach_a_model_without_fit(tmp_path):
    default = _llama_unit_args(tmp_path, "default", "--model", "gpt-oss")
    assert _llama_unit_args(tmp_path, "headroom", "--model", "gpt-oss", "--vram-headroom-mib", "2048") == default
    assert "--fit-target" not in default


def test_vram_headroom_dry_run_names_the_margin_only_when_set(tmp_path):
    with_flag = run_installer(tmp_path, "Linux", "x86_64", "--profile", "full", "--model", "qwen",
                              "--vram-headroom-mib", "2048")
    assert with_flag.returncode == 0, with_flag.stderr
    assert "--fit-target 2048" in with_flag.stdout
    without = run_installer(tmp_path, "Linux", "x86_64", "--profile", "full", "--model", "qwen")
    assert without.returncode == 0, without.stderr
    assert "--fit-target" not in without.stdout
    other_model = run_installer(tmp_path, "Linux", "x86_64", "--profile", "full", "--model", "gpt-oss",
                                "--vram-headroom-mib", "2048")
    assert other_model.returncode == 0, other_model.stderr
    assert "--fit-target" not in other_model.stdout
    assert "--vram-headroom-mib ignored" in other_model.stdout


@pytest.mark.parametrize("value", ["abc", "-1", "2048MiB", "02048", "1.5", ""])
def test_vram_headroom_rejects_anything_but_a_whole_number(tmp_path, value):
    result = run_installer(tmp_path, "Linux", "x86_64", "--profile", "full", "--vram-headroom-mib", value)
    assert result.returncode == 64
    assert "--vram-headroom-mib must be a whole number of MiB" in result.stderr


def test_vram_headroom_needs_a_value_and_is_in_the_help(tmp_path):
    assert run_installer(tmp_path, "Linux", "x86_64", "--vram-headroom-mib").returncode == 64
    assert "--vram-headroom-mib" in run_installer(tmp_path, "Linux", "x86_64", "--help").stdout


@pytest.mark.parametrize("name", ["app", "my app", "app 100%done"])
def test_linux_daemon_unit_working_directory_loads_in_systemd(tmp_path, name):
    app_dir = tmp_path / name
    result, unit, install_dir = _install_linux_units(tmp_path, app_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = dict(line.split("=", 1) for line in unit.read_text().splitlines() if "=" in line)
    working = lines["WorkingDirectory"]
    # systemd takes WorkingDirectory= literally: no quotes, only specifiers (%) need escaping.
    assert not working.startswith('"') and working.startswith("/")
    assert working.replace("%%", "%") == str(app_dir)
    exec_args = shlex.split(lines["ExecStart"].replace("%%", "%"))
    assert exec_args[:3] == ["/bin/bash", str(install_dir / "bin/run-daemon.sh"), str(app_dir)]
    if shutil.which("systemd-analyze"):
        verified = subprocess.run(["systemd-analyze", "verify", "--user", str(unit)],
                                  capture_output=True, text=True, timeout=30, check=False)
        assert "WorkingDirectory" not in verified.stderr, verified.stderr
        assert verified.returncode == 0, verified.stderr
