"""Keyboard shortcuts, the `?` sheet and focus rings (#571)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "harness" / "web"


def _desktop10_css() -> str:
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "/* Desktop 10: keyboard" in css, "Desktop 10 keeps its rules in their own section"
    return css.split("/* Desktop 10: keyboard", 1)[1].split("/* End Desktop 10. */", 1)[0]


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"{selector} missing from the Desktop 10 section"
    return match.group(1)


def test_keys_behave():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_keys.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: shortcuts, the ? sheet, fields and no Approve key" in result.stdout


def test_focus_rings_are_desktop_only_and_use_the_accent():
    section = _desktop10_css()
    phone, desktop = section.split("@media (min-width: 768px)", 1)
    assert ":focus-visible" not in phone, "the phone layout doesn't change"
    controls = _rule(desktop, ":is(.btn, .icon, .tabs button, .switch):focus-visible")
    rows = _rule(desktop, ":is(a.agent-row, a.card, a.job-main, a.set-row):focus-visible")
    for rule in (controls, rows):
        assert "outline: 2px solid var(--accent)" in rule
    assert "outline-offset: -2px" in rows, "rows ring inside their card, which clips anything outside"
    rules = re.sub(r"/\*.*?\*/", "", section.split("*/", 1)[1], flags=re.S)  # the section opens inside its comment
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rules), "Desktop 10 colours come from the theme tokens"
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in _rule(desktop, ".keys-groups")


def test_keys_are_wired_into_the_app():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'import { mountKeys } from "./lib/keys.mjs";' in app
    assert "mountKeys({ browser, go, role:" in app
    session = (WEB / "pages" / "session.mjs").read_text(encoding="utf-8")
    assert 'e.key === "Enter" && (e.ctrlKey || e.metaKey)' in session, "Ctrl+Enter sends from the session composer"
    sessions = (WEB / "pages" / "sessions.mjs").read_text(encoding="utf-8")
    assert '"aria-keyshortcuts": "/"' in sessions
    keys = (WEB / "lib" / "keys.mjs").read_text(encoding="utf-8")
    assert not re.search(r"approv", keys.split("export function mountKeys", 1)[1], re.I), "no Approve key"
