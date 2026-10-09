"""Header connection chip is app-owned, survives route changes (#83) and says Live / Reconnecting / Offline (#510)."""
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
    # #510: backoff with jitter replaced the fixed 3 s retry.
    assert "setTimeout(connect, 3000)" not in stream
    assert "retryDelay(attempts - 1)" in stream
    # The chip has words for its states and starts hidden; refresh failures are no longer swallowed.
    html = (web / "index.html").read_text(encoding="utf-8")
    assert '<span id="conn" class="conn" role="status"' in html and " hidden></span>" in html
    sessions = (web / "pages/sessions.mjs").read_text(encoding="utf-8")
    assert "render().catch(() => {})" not in sessions
    chat = (web / "pages/chat.mjs").read_text(encoding="utf-8")
    assert ".catch(() => {}); // offline" not in chat
    assert "stream.watchDaemonConnection();" in (web / "lib/router.mjs").read_text(encoding="utf-8")
    # Page stream cleanup must not own the header class directly.
    assert "$conn.classList.remove" not in app + stream
    assert "$conn.classList.add" not in app + stream
