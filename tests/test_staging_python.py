"""Exercise staging's venv migration in temporary directories, without deploying a daemon."""

from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest


ROOT = Path(__file__).parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is not installed")


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def initialize(venv, bootstrap=None, dry_run=False):
    script = ROOT / "ops/harness/staging-python.ps1"
    command = f"$ErrorActionPreference = 'Stop'; . {ps_quote(script)}; Initialize-StagingPython -Venv {ps_quote(venv)}"
    if bootstrap:
        command += f" -BootstrapPython {ps_quote(bootstrap)}"
    if dry_run:
        command += " -DryRun"
    return subprocess.run([POWERSHELL, "-NoProfile", "-Command", command], capture_output=True, text=True)


def test_project_declares_supported_python_floor():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["requires-python"] == ">=3.12"


@pytest.mark.parametrize("existing", [False, True])
def test_staging_builds_312_and_preserves_a_compatible_venv(tmp_path, existing):
    venv = tmp_path / "venv"
    if existing:
        # A broken/old environment must be rebuilt, including stale installed packages.
        venv.mkdir()
        (venv / "stale-package.txt").write_text("old environment", encoding="utf-8")
    result = initialize(venv, sys.executable)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (venv / "stale-package.txt").exists()
    python = venv / "Scripts/python.exe"
    version = subprocess.check_output([python, "-c", "import sys; print(sys.version_info[:2])"], text=True)
    assert version.strip() == "(3, 12)"
    subprocess.run([python, "-m", "pip", "--version"], check=True, capture_output=True)
    sentinel = venv / "preserved.txt"
    sentinel.write_text("compatible environment", encoding="utf-8")
    result = initialize(venv, sys.executable)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.exists()


def test_incompatible_bootstrap_does_not_clear_existing_venv(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    sentinel = venv / "preserved.txt"
    sentinel.touch()
    bootstrap = tmp_path / "old-python.cmd"
    bootstrap.write_text("@exit /b 1\n", encoding="utf-8")
    result = initialize(venv, bootstrap)
    assert result.returncode != 0
    assert "bootstrap must be Python 3.12" in result.stderr
    assert sentinel.exists()


def test_dry_run_does_not_create_a_venv(tmp_path):
    venv = tmp_path / "venv"
    result = initialize(venv, dry_run=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ensure Python 3.12" in result.stdout
    assert not venv.exists()


def test_production_venv_is_rejected():
    result = initialize(r"D:\Agents\harness\venv", dry_run=True)
    assert result.returncode != 0
    assert "production location" in result.stderr
