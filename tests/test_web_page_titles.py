"""Topbar #title shows the current page's name on every route, not only Chat (#154)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_page_title_set_on_every_route():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_page_titles.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
