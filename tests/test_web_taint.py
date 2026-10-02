"""Replaying a stored taint_added over the session snapshot keeps one entry per source (#262 review)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_taint_replay_does_not_duplicate():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_taint.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
