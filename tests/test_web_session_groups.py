"""Agents list groups sessions into Needs you, Running and Recent, with the pending ask inline (#509)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_session_groups():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_session_groups.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
