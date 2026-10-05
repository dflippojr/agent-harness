"""The Web lib modules run under plain Node with a stub DOM (#372)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_lib_modules():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_lib_modules.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: lib modules import under plain Node and behave on their own" in result.stdout
