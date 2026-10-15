"""New task page renders from the extracted module with stub deps (#258 stage h)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def test_new_task_page_renders():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_new_task.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_tool_profile_choice_shows_for_supporting_backends_and_is_sent():
    """#638: Tools appears only for a backend that lists bash-only, and the choice goes out as tool_profile."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run([node, str(STABLES_TESTS / "web_new_task_tool_profile.mjs")],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
