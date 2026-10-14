"""Boot issues /health and /me together, one /me, and route data without waiting on warm-up (#289).
Offline boot keeps the shell instead of becoming a guest (#368)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"
# stables/tests/run.mjs runs the same scenarios (test_web_harness_coverage.py checks they match).
SCENARIOS = ["owner", "member", "guest", "me-fallback", "client-update", "daemon-update", "offline", "offline-cached"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_boot_request_chain(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_boot_requests.mjs"
    result = subprocess.run([node, str(script), scenario], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
