"""Session list page renders from the extracted module with stub deps (#258 stage i), and beside the open session at 1280 px+ (#563)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_sessions_page_renders():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_sessions.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_agents_split_view():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_split_view.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
