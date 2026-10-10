"""In-app sheets replace the browser's native confirm and prompt dialogs (#513)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "harness" / "web"

# A call of the native dialogs, bare or through window/globalThis/browser/self. Method calls on other objects
# (`sheets.confirm(`) and longer names (`confirmSheet(`) do not match.
NATIVE_DIALOG = re.compile(r"(?<![\w$.])(?:(?:window|globalThis|browser|self)\s*\.\s*)?(confirm|prompt)\s*\(")


def test_no_native_confirm_or_prompt_in_web():
    hits = []
    for path in sorted(WEB.rglob("*")):
        if path.suffix not in {".js", ".mjs", ".html"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if NATIVE_DIALOG.search(line):
                hits.append(f"{path.relative_to(WEB).as_posix()}:{number}: {line.strip()}")
    assert not hits, "use lib/sheet.mjs instead of the native dialogs:\n" + "\n".join(hits)


def test_native_dialog_pattern():
    for call in ('confirm("x")', "window.confirm(m)", "if (!prompt ('a'))", "globalThis.prompt(x)", "x = browser.confirm(y)"):
        assert NATIVE_DIALOG.search(call), call
    for other in ("confirmSheet({})", "promptSheet({})", "sheets.confirm(x)", "{ confirm: true }", "const prompt = h('textarea')"):
        assert not NATIVE_DIALOG.search(other), other


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"{selector} missing from style.css"
    return match.group(1)


def test_sheet_css_uses_tokens_and_44px_targets():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    sheet_rules = "\n".join(_rule(css, s) for s in (
        "dialog.sheet", ".sheet-actions .btn", ".sheet-cancel", ".sheet-danger", ".sheet-field input", ".sheet-error"))
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", sheet_rules), "sheet colours come from the theme tokens"
    assert "min-height: 50px" in _rule(css, ".sheet-actions .btn")
    assert "min-height: var(--tap)" in _rule(css, ".sheet-field input")
    assert "var(--bad)" in _rule(css, ".sheet-danger")


def test_sheets_behave():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_sheets.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: in-app sheets confirm, prompt, validate and dismiss" in result.stdout


def test_desktop_sheet_actions_and_output_modal():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    legacy, desktop = css.split("/* Desktop 9:", 1)
    narrow = legacy.split("@media (min-width: 700px)", 1)[1]
    assert "margin: auto" in _rule(narrow, "dialog.sheet")
    assert "padding: 20px" in _rule(narrow, ".sheet-body")
    desktop = desktop.split("\n.note", 1)[0]
    assert "@media (min-width: 768px)" in desktop
    assert "justify-content: flex-end" in _rule(desktop, ".sheet-actions")
    buttons = _rule(desktop, ".sheet-actions .btn")
    assert "flex: 0 1 auto" in buttons
    assert "min-height: var(--tap)" in buttons
    viewer = _rule(desktop, ".tool-viewer")
    assert "margin: auto" in viewer
    assert "max-width: 960px" in viewer
    assert "100dvh - 64px" in viewer
