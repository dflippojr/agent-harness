"""Native append must not paint literal null or joined run URLs (#183)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_web_views_do_not_render_null_or_url_lists():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_append_null.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
