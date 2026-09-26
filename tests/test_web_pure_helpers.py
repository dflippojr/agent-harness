"""Pure web helpers extracted by the Sonar style cleanup keep their exact output (#229)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_pure_helpers_keep_their_output():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_pure_helpers.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
