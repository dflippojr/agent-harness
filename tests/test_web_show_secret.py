"""showSecretOnce shows a secret once, copies it, and reloads on Done (#229, split out of the pure helpers in #258)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_show_secret_once_flow():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_show_secret.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
