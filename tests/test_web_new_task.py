"""New task page renders from the extracted module with stub deps (#258 stage h)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


def test_new_task_page_renders():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / "web_new_task.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_prompt_is_associated_with_settings_form_in_both_layouts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run([node, str(STABLES_TESTS / "web_new_task_form.mjs")],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_two_column_task_layout_is_desktop_only():
    from test_web_tokens import _decls, _rules

    css = (Path(__file__).resolve().parents[1] / "stables/public/style.css").read_text(encoding="utf-8")
    rules = [(selectors, _decls(body), media) for selectors, body, media in _rules(css)
             if any("new-task-" in selector for selector in selectors)]
    assert rules
    assert all(media == "@media (min-width: 768px)" for _, _, media in rules)
    layout = next(decls for selectors, decls, _ in rules if ".new-task-layout" in selectors)
    assert layout["display"] == "grid"
    assert "minmax(0, 1fr)" in layout["grid-template-columns"]
    prompt = next(decls for selectors, decls, _ in rules if ".new-task-prompt" in selectors)
    settings = next(decls for selectors, decls, _ in rules if ".new-task-settings" in selectors)
    assert prompt["grid-column"] == "1"
    assert settings["grid-column"] == "2"


def test_tool_profile_choice_shows_for_supporting_backends_and_is_sent():
    """#638: Tools appears only for a backend that lists bash-only, and the choice goes out as tool_profile."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run([node, str(STABLES_TESTS / "web_new_task_tool_profile.mjs")],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
