"""Chat page and shared session helpers render from the extracted modules with stub deps (#258 stage k)."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"
SCRIPTS = ["web_chat.mjs", "web_session_ui.mjs"]


def _run(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    result = subprocess.run([node, str(STABLES_TESTS / name)], capture_output=True, text=True, encoding="utf-8",
                            timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_chat_modules_render(name):
    _run(name)


def test_recent_chats_are_outside_the_welcome_gradient():
    _run("web_chat_recent.mjs")


def test_desktop_composer_matches_transcript_and_gradient_fades_at_edges():
    from test_web_tokens import _decls, _rules

    css = (Path(__file__).resolve().parents[1] / "stables/public/style.css").read_text(encoding="utf-8")
    rules = [(selectors, _decls(body), media) for selectors, body, media in _rules(css)]
    main = next(decls for selectors, decls, _ in rules if selectors == ["main"])
    composer, media = next((decls, media) for selectors, decls, media in rules
                           if selectors == [".chat-page .chat-composer .inner"])
    assert media == "@media (min-width: 768px)"
    assert int(composer["max-width"].removesuffix("px")) == int(main["max-width"].removesuffix("px")) - 32
    rail = next(decls for selectors, decls, media in rules
                if "body.has-tabs .composer" in selectors and media == "@media (min-width: 768px)")
    assert rail["left"] == "var(--rail-w)"
    surface, media = next((decls, media) for selectors, decls, media in rules
                          if selectors == [".chat-welcome-content"])
    assert media == "@media (min-width: 768px)"
    assert "ellipse at center" in surface["background"]
    assert "transparent 70%" in surface["background"]
