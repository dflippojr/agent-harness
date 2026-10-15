"""Chat page and shared session helpers render from the extracted modules with stub deps (#258 stage k)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"
SCRIPTS = ["web_session_ui.mjs"]


def _run(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run([node, str(STABLES_TESTS / name)], capture_output=True, text=True, encoding="utf-8",
                            timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_chat_modules_render(name):
    _run(name)
