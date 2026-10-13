"""New task page renders from the extracted module with stub deps (#258 stage h)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_new_task_page_renders():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = Path(__file__).resolve().parent / "web_new_task.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_prompt_is_associated_with_settings_form_in_both_layouts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    tests = Path(__file__).resolve().parent
    script = (tests / "web_new_task.mjs").read_text(encoding="utf-8") + r'''
const walk = (n) => n && typeof n === "object" ? [n, ...(n.kids || []).flatMap(walk)] : [];
const nodes = appended.flatMap(walk);
const settings = nodes.find((n) => n.attrs.class === "new-task-settings");
const promptPane = nodes.find((n) => n.attrs.class === "new-task-prompt");
const promptInput = walk(promptPane).find((n) => n.tag === "textarea");
assert.equal(settings.tag, "form");
assert.equal(promptInput.attrs.form, settings.attrs.id, "the prompt participates in native form behavior");
assert.equal(promptInput.attrs.id, walk(promptPane).find((n) => n.tag === "label").attrs.for);
assert.ok(!walk(settings).includes(promptInput), "desktop can place the prompt separately from settings");
const layout = nodes.find((n) => n.attrs.class === "new-task-layout");
assert.deepEqual(layout.kids.map((n) => n.attrs.class), ["new-task-context", "new-task-prompt", "new-task-settings"],
  "phone flow retains context before prompt before settings");
assert.ok(walk(settings).some((n) => n.tag === "button" && n.attrs.type === "submit" && text(n) === "Start"));
'''
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=tests,
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
    tests = Path(__file__).resolve().parent
    script = r'''
globalThis.newTaskFixture = {
  backends: [{ name: "local", available: true }, { name: "claude", available: true, tool_profiles: ["default", "bash-only"] }],
  onPost: (_path, body) => { globalThis.sent = body; return { id: "s1" }; },
};
''' + (tests / "web_new_task.mjs").read_text(encoding="utf-8") + r'''
const walk = (n) => n && typeof n === "object" ? [n, ...(n.kids || []).flatMap(walk)] : [];
const nodes = appended.flatMap(walk);
const select = nodes.find((n) => n.attrs.id === "new-task-tool-profile");
assert.deepEqual(select.kids.map((o) => o.attrs.value), ["default", "bash-only"]);
const fieldBox = nodes.find((n) => n.tag === "div" && n.kids.includes(select));
const backend = nodes.find((n) => n.attrs.id === "new-task-backend");
assert.equal(backend.value, "claude", "the GPU hold picks the hosted backend");
assert.equal(fieldBox.hidden, false);
const settings = nodes.find((n) => n.attrs.class === "new-task-settings");
settings.querySelectorAll = () => [];
nodes.find((n) => n.attrs.id === "new-task-prompt").value = "do it";
select.value = "bash-only";
await settings.attrs.onsubmit({ preventDefault() {} });
assert.equal(globalThis.sent.tool_profile, "bash-only");
assert.equal(globalThis.sent.backend, "claude");
'''
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=tests,
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
