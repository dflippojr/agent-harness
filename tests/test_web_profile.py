"""Profile rendering and desktop Settings split regressions (#569)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def run_node(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run(
        [node, str(STABLES_TESTS / name)], capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_profile_page_renders():
    run_node("web_profile.mjs")


def test_settings_split_routes_and_lifecycle():
    run_node("web_settings_split.mjs")


def test_settings_desktop_css_is_scoped():
    css = (STABLES_TESTS.parent / "public" / "style.css").read_text(encoding="utf-8")
    desktop = css.split("/* Desktop 8:", 1)[1].split("/* End Desktop 8. */", 1)[0]
    assert "@media (min-width: 768px)" in desktop
    assert "@media (min-width: 1280px)" in desktop
    assert "repeat(4, minmax(0, 1fr))" in desktop
    assert ".resources-tabs button" in desktop
    assert "white-space: nowrap" in desktop
