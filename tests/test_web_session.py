"""Session page renders from the extracted module with stub deps (#258 stage j)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_session_page_renders():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_session.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_split_view_css_keeps_session_controls_beside_the_list():
    """#563: the split's rules stay behind 1280 px, and the session's fixed controls start after the list pane."""
    css = (Path(__file__).resolve().parents[1] / "harness" / "web" / "style.css").read_text(encoding="utf-8")
    start = css.index("/* Desktop 3: split view")
    section = css[start:css.index("/* End Desktop 3. */", start)]
    outside, _, inside = section.partition("@media (min-width: 1280px) {")
    # Outside the query only the default (hidden) state, so phones and 768-1279 px never see the pane or its toggle.
    rules = [line for line in outside.splitlines() if "{" in line]
    assert rules == ["#split-list, .split-toggle { display: none; }"]
    assert "body.has-tabs.split { padding-left: calc(var(--rail-w) + var(--split-w)); }" in inside
    assert "body.has-tabs.split .composer, body.has-tabs.split .approval-sheet { left: calc(var(--rail-w) + var(--split-w)); }" in inside
    assert "body.split.split-collapsed { --split-w: 0px; }" in inside
    assert '#split-list .agent-row[aria-current="page"]' in inside
