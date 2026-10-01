"""First-document splash readiness, fallback, and motion policy (#179)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_boot_splash_lifecycle():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_boot_splash.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
