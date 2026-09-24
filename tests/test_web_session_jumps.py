"""Session jump arrows: visibility from scroll position and page height (#184)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_session_jump_visibility_from_scroll_and_page_height():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_session_jumps.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
