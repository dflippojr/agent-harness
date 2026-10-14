import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def test_remote_control_starts_and_stops_without_admin_controls():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    script = STABLES_TESTS / "web_remote_control.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ok:' in result.stdout
