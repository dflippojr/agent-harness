"""Session Info page renders from the extracted module with stub deps (#258 stage f)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_session_info_page_renders():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_session_info.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_changes_and_info_desktop_rules_preserve_phone_layout():
    css = (Path(__file__).resolve().parents[1] / "harness" / "web" / "style.css").read_text(encoding="utf-8")
    start = css.index("/* Desktop 5: Changes and Info")
    section = css[start:css.index("/* End Desktop 5. */", start)]
    outside, _, inside = section.partition("@media (min-width: 768px) {")
    rules = [line for line in outside.splitlines() if "{" in line]
    assert rules == [
        ".changes-files { display: none; }",
        ".session-info-row { justify-content: space-between; padding: 4px 0; }",
        ".session-info-value { overflow-wrap: anywhere; text-align: right; }",
    ]
    assert "body.session-page.session-changes main { max-width: none; }" in inside
    assert "grid-template-columns: minmax(0, 15rem) minmax(0, 1fr)" in inside
    assert "grid-template-columns: subgrid" in inside
    assert "text-align: left" in inside
    assert ".changes-file-link:focus-visible" in inside
