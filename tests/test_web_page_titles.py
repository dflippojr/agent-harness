"""Page titles and list New actions follow routes and roles (#154, #562)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_page_title_set_on_every_route():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_page_titles.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
