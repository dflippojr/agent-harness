"""New task follows GPU hold: Claude default, notice under Backend, Queue task (#185)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_new_task_gpu_hold_default_notice_and_queue_button():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_new_task_gpu_hold.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
