"""Every Stables Node harness runs under its Node runner and has a pytest wrapper (#372, #334 stage d)."""
import ast
import json
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
STABLES_TESTS = TESTS.parent / "stables" / "tests"
# Imported by the harnesses (and the runner itself); they have no tests of their own.
NEWLINE = chr(10)
HELPERS = {"run.mjs", "web_app_loader.mjs", "web_stub_dom.mjs"}


def _harnesses():
    return sorted(path.name for path in STABLES_TESTS.glob("*.mjs") if path.name not in HELPERS)


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    return node


def _runs_node(tree):
    """True when the module calls subprocess.run (or Popen, check_output, check_call) with `node` among its arguments."""
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in {"run", "Popen", "check_output", "check_call"}
                and any(isinstance(n, ast.Name) and n.id == "node" for n in ast.walk(call))):
            return True
    return False


def _wrapped_harnesses():
    """Harness names that a test module passes as a string, in a module that really runs node.

    A comment or a docstring that only mentions a file name is not a wrapper: comments are not in the syntax tree, and
    bare string statements (docstrings) are skipped. A row of the test_web_node_harnesses.py table counts the same way,
    since that module runs node for every row."""
    names = set()
    for path in TESTS.glob("test_*.py"):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        if ".mjs" not in source or "subprocess" not in source:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # other test modules hold Windows paths in plain strings
            tree = ast.parse(source)
        if not _runs_node(tree):
            continue
        docstrings = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        names.update(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and n.value.endswith(".mjs") and id(n) not in docstrings)
    return names


def test_web_harnesses_have_pytest_wrappers():
    missing = [name for name in _harnesses() if name not in _wrapped_harnesses()]
    assert not missing, f"Stables harnesses missing a pytest run of node: {', '.join(missing)}"


def test_wrapper_check_needs_a_real_node_call(tmp_path, monkeypatch):
    """A name in a comment, a docstring, or a module that never runs node is not a wrapper."""
    comment_only = NEWLINE.join([
        '"""Mentions web_a.mjs."""', "# web_b.mjs", "import subprocess", "", "", "def test_x():",
        '    subprocess.run(["ls"])', '    name = "web_c.mjs"', ""])
    real = NEWLINE.join(["import subprocess", 'node = "node"', "", "", "def test_x():",
                     '    subprocess.run([node, "web_d.mjs"])', ""])
    (tmp_path / "test_comment_only.py").write_text(comment_only, encoding="utf-8")
    (tmp_path / "test_real.py").write_text(real, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "TESTS", tmp_path)
    assert _wrapped_harnesses() == {"web_d.mjs"}


def test_no_harness_is_left_in_the_server_tests():
    assert not sorted(path.name for path in TESTS.glob("web_*.mjs")), "Stables harnesses live in stables/tests"


def test_node_runner_lists_every_harness_and_scenario():
    node = _node()
    listed = subprocess.run([node, str(STABLES_TESTS / "run.mjs"), "--list"], capture_output=True, text=True,
                            encoding="utf-8", timeout=30, check=True).stdout.splitlines()
    assert sorted({line.split()[0] for line in listed}) == _harnesses()
    script = ("import { HELPERS, SCENARIOS } from './run.mjs';"
              "console.log(JSON.stringify({ helpers: [...HELPERS], scenarios: SCENARIOS }));")
    exported = json.loads(subprocess.run([node, "--input-type=module", "-e", script], cwd=STABLES_TESTS,
                                         capture_output=True, text=True, encoding="utf-8", timeout=30,
                                         check=True).stdout)
    assert set(exported["helpers"]) == HELPERS
    from test_web_boot_requests import SCENARIOS
    assert exported["scenarios"] == {"web_boot_requests.mjs": SCENARIOS}
    assert [line for line in listed if line.startswith("web_boot_requests.mjs")] == [
        f"web_boot_requests.mjs {scenario}" for scenario in SCENARIOS]


def test_node_runner_reports_a_failing_harness(tmp_path):
    node = _node()
    for name in ("run.mjs", "web_stub_dom.mjs"):
        shutil.copy(STABLES_TESTS / name, tmp_path / name)
    (tmp_path / "web_pass.mjs").write_text('console.log("ok");\n', encoding="utf-8")
    (tmp_path / "web_fail.mjs").write_text('console.log("before"); process.exit(3);\n', encoding="utf-8")
    result = subprocess.run([node, str(tmp_path / "run.mjs")], capture_output=True, text=True, encoding="utf-8",
                            timeout=60)
    assert result.returncode == 1
    assert "pass  web_pass.mjs" in result.stdout
    assert "FAIL  web_fail.mjs (exit 3)" in result.stdout
    assert "--- web_fail.mjs\nbefore" in result.stdout
    assert "1 passed, 1 failed" in result.stdout
    only = subprocess.run([node, str(tmp_path / "run.mjs"), "pass"], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert only.returncode == 0 and "1 passed, 0 failed" in only.stdout
    none = subprocess.run([node, str(tmp_path / "run.mjs"), "nothing-matches"], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert none.returncode == 1 and "no harness matches nothing-matches" in none.stderr
