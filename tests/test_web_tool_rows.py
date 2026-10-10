"""Tool-call rows: lazy output, Copy and the full-screen viewer (#508)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_tool_rows():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_tool_rows.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_tool_rows_desktop_css():
    """#564: from 768 px tool rows get hover and focus rings; Copy / Open stay 44 px targets at every width."""
    css = (Path(__file__).resolve().parents[1] / "harness" / "web" / "style.css").read_text(encoding="utf-8")
    start = css.index("/* Desktop 4: session pane")
    section = css[start:css.index("/* End Desktop 4. */", start)]
    inside = section.partition("@media (min-width: 768px) {")[2]
    assert "body.session-page details.tool > summary:hover { background: var(--panel-2); }" in inside
    assert "body.session-page details.tool > summary:focus-visible" in inside
    assert "body.session-page .tool-btn:hover { background: var(--line); }" in inside
    assert "min-height" not in inside.split("body.session-page .tool-btn", 1)[1].split("}", 1)[0]
    assert ".tool-btn { flex: none; min-height: var(--tap);" in css
