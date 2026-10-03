"""Session Info page renders from the extracted module with stub deps (#258 stage f)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_session_info_page_renders():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_session_info.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
