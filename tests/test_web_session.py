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


def test_session_pane_header_approval_and_bar():
    """#564: the one-row desktop header, the docked approval's focus rule and the Changes/Info approval bar."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_session_pane.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def _desktop4_css():
    css = (Path(__file__).resolve().parents[1] / "harness" / "web" / "style.css").read_text(encoding="utf-8")
    start = css.index("/* Desktop 4: session pane")
    return css, css[start:css.index("/* End Desktop 4. */", start)]


def test_session_pane_css_stays_behind_768px():
    """#564: phones keep their layout. Outside the 768 px query the section only hides the approval bar and lowers the
    jump buttons under the bar's layer (where the ⋯ menu lives)."""
    css, section = _desktop4_css()
    outside, _, inside = section.partition("@media (min-width: 768px) {")
    rules = [line for line in outside.splitlines() if "{" in line]
    assert rules == ["body .jump { z-index: 9; }", ".approval-bar { display: none; }"]
    # The jump buttons sit under #bar (z-index 10, the menu's layer) and over the approval sheet (8) and composer (6).
    assert "#bar {\n  position: sticky; top: 0; z-index: 10;" in css
    assert ".approval-sheet {\n  position: fixed; left: 0; right: 0; bottom: var(--tabbar-offset); z-index: 8;" in css
    assert "position: fixed; left: 0; right: 0; bottom: 0; z-index: 6;" in css
    # One header row: the title over its meta, then the segmented control, the connection chip and ⋯.
    assert 'grid-template-areas: "back toggle title tabs conn menu" "back toggle meta tabs conn menu";' in inside
    for area in ("back", "toggle", "title", "meta", "tabs", "conn", "menu"):
        assert f"grid-area: {area};" in inside, area
    # The 760 px transcript column inside 24 px gutters, and the composer and approval centred on the pane.
    assert "body.session-page main { max-width: calc(760px + 48px); padding-inline: 24px; }" in inside
    assert "body.has-tabs.session-page .composer { left: calc(var(--rail-w) + var(--split-w, 0px)); right: 0; }" in inside
    assert "left: calc(var(--rail-w) + var(--split-w, 0px) + 24px);" in inside
    assert "left: 24px; right: 24px; bottom: 18px; max-width: 760px; margin-inline: auto;" in inside
    # Deny and Approve: right-aligned at their natural width and 44 px tall; Cancel task stays in ⋯.
    assert "body.session-page .approval-actions { order: 9; flex: none; margin: 0 0 0 auto; gap: 8px; }" in inside
    assert "min-width: 124px; min-height: var(--tap);" in inside
    assert "body.session-page .approval-cancel { display: none; }" in inside
    # The transcript's end and the jump button clear the docked card, whose height the page measures.
    assert "body.session-page:has(.approval-sheet) main { padding-bottom: calc(var(--approval-h, 360px) + 36px); }" in inside
