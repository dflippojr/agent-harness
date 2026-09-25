"""md() output is unchanged by the Sonar refactor (#206)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_markdown_renders_recorded_html():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_markdown.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
