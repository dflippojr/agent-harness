"""Top-bar profile icon on every top-level feature, hidden on nested/Profile (#187)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_profile_icon_visibility_by_feature_and_phone_width():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_profile_icon.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
    web = Path(__file__).resolve().parents[1] / "harness/web"
    app = (web / "app.js").read_text(encoding="utf-8")
    assert "export function profileIconHidden" in (web / "lib/layout.mjs").read_text(encoding="utf-8")
    assert 'feature !== "chat"' not in app
