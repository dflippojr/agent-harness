"""Jobs list: attention groups and the inline Enabled switch with Undo (#511)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_jobs_list_groups_and_inline_switch():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_jobs.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
