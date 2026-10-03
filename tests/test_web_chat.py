"""Chat page and shared session helpers render from the extracted modules with stub deps (#258 stage k)."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = ["web_chat.mjs", "web_session_ui.mjs"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_chat_modules_render(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / name
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
