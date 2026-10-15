"""Images: GPU hold and installed models (#186), desktop gallery and details (#568)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def test_image_model_filter_and_queue_generation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_image_gpu_hold.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_image_columns_keep_gallery_jobs_and_detail_actions():
    """Exercise the new containers, busy polling, and route teardown with real DOM helpers."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run([node, str(STABLES_TESTS / "web_image_columns.mjs")],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
