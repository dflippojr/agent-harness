"""md() link regex is linear on crafted input, and matches the old regex on legitimate input (S8786, #239)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_markdown_links_are_linear_and_unchanged():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_markdown_links.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
