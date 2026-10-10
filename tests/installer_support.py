"""Shared subprocess support for daemon installer tests."""
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


