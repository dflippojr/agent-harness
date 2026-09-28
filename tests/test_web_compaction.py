"""Session-page mask / summary / elide compaction notes (#248)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_session_compaction_events_render_mask_without_touching_summary():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_compaction.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
