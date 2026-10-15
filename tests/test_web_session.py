"""The session pane's debounce fixture, Changes file navigation and backend hand-off. The page itself is journeys AH-21 and AH-22 in tools/ui-tests."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def test_session_pane_header_approval_and_bar():
    """#564: the one-row desktop header, the docked approval's focus rule and the Changes/Info approval bar."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_session_pane.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_changes_file_navigation_and_inline_comments():
    """#565: file navigation opens the right diff; inline ranges retain their draft and API payload."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_changes.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_waiting_limit_session_continues_on_another_backend():
    """#664: Continue on another backend cancels, then forks from the latest checkpoint; Run again takes the picker."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_backend_continue.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
