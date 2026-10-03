"""Header connection dot is app-owned and survives route changes (#83)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_connection_dot_stays_live_across_routes_and_reconnects():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_connection_dot.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
    web = Path(__file__).resolve().parents[1] / "harness/web"
    app = (web / "app.js").read_text(encoding="utf-8")
    stream = (web / "lib/stream.mjs").read_text(encoding="utf-8")
    assert "function watchDaemonConnection()" in stream
    assert "indicate = false" in stream
    assert "stream.watchDaemonConnection();" in (web / "lib/router.mjs").read_text(encoding="utf-8")
    # Page stream cleanup must not own the header class directly.
    assert "$conn.classList.remove" not in app + stream
    assert "$conn.classList.add" not in app + stream
