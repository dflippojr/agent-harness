import shutil
import subprocess
from pathlib import Path

import pytest


def test_owner_discovery_phone_and_desktop_flows():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    script = Path(__file__).with_name('web_folder_discovery.mjs')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ok:' in result.stdout
