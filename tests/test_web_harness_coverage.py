"""Every Stables Node harness runs under its Node runner and has a pytest wrapper (#372, #334 stage d)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
STABLES_TESTS = TESTS.parent / "stables" / "tests"
# Imported by the harnesses (and the runner itself); they have no tests of their own.
HELPERS = {"run.mjs", "web_app_loader.mjs", "web_stub_dom.mjs"}


def _harnesses():
    return sorted(path.name for path in STABLES_TESTS.glob("*.mjs") if path.name not in HELPERS)


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    return node


def test_web_harnesses_have_pytest_wrappers():
    wrappers = [path.read_text(encoding="utf-8") for path in TESTS.glob("test_*.py")]
    missing = [name for name in _harnesses() if not any(name in source for source in wrappers)]
    assert not missing, f"Stables harnesses missing pytest wrappers: {', '.join(missing)}"


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
