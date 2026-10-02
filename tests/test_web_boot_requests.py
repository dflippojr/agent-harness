"""Boot issues /health and /me together, one /me, and route data without waiting on warm-up (#289)."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "owner", "member", "guest", "me-fallback", "client-update", "daemon-update",
])
def test_boot_request_chain(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_boot_requests.mjs"
    result = subprocess.run([node, str(script), scenario], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
