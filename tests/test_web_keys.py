"""Keyboard shortcuts and the `?` sheet (#571). The journey that wires them into the app is AH-19 in tools/ui-tests."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def test_keys_behave():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_keys.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: shortcuts, the ? sheet, fields and no Approve key" in result.stdout
