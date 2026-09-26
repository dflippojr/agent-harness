"""Images tab: hide uninstalled models, Queue Generation during a GPU hold (#186)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_image_model_filter_and_queue_generation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    root = Path(__file__).resolve().parents[1]
    script = Path(__file__).resolve().parent / "web_image_gpu_hold.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
    app = (root / "harness/web/app.js").read_text(encoding="utf-8")
    css = (root / "harness/web/style.css").read_text(encoding="utf-8")
    assert "function installedImageModeEntries" in app
    assert "Queue Generation" in app
    assert "classList.toggle(\"queued\"" in app
    assert ".btn.queued" in css
    assert "var(--queued)" in css
    assert ".image-grid" in css
    assert "margin-top: 20px" in css
    assert "@media (max-width: 640px)" in css
    assert "margin-top: 24px" in css
    assert "ops/images-models.ps1" in app
