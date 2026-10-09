"""Bottom tab bar and Settings gear replace the navigation drawer (#506)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_tab_bar_by_route_and_role():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_tab_bar.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
    web = Path(__file__).resolve().parents[1] / "harness/web"
    assert not (web / "lib/drawer.mjs").exists()
