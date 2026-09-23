"""Chat route paints its shell before /chats/options or /chats/<id> resolves (#152)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_chat_route_paints_shell_before_data_loads():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_chat_first_paint.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
