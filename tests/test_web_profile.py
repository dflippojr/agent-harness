"""Profile rendering and desktop Settings split regressions (#569)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def run_node(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run(
        [node, str(STABLES_TESTS / name)], capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_profile_page_renders():
    run_node("web_profile.mjs")
