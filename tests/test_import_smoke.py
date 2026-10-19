"""Issue #730: every shipped module compiles and imports under each Python version CI runs.

The `test-py313` job runs this file on Python 3.13, and the `test` job runs it on 3.12. A lone surrogate in a string
literal, even in a docstring, compiles on 3.12 and fails on 3.13 (#689 shipped that way). Compiling each file from its
source catches it even when a stale .pyc would hide it from an import.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
# The shipped packages, as pyrightconfig.json lists them.
PACKAGES = ("harness", "harness_modules", "sdk", "macrunner")


def _source_files() -> list[Path]:
    return [path for package in PACKAGES for path in sorted((ROOT / package).rglob("*.py"))
            if "__pycache__" not in path.parts]


def _module_name(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def test_every_shipped_source_file_compiles_with_syntax_warnings_as_errors():
    failures = []
    for path in _source_files():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                compile(path.read_bytes(), str(path), "exec")
        except (SyntaxError, ValueError) as error:  # a UnicodeEncodeError is a ValueError
            failures.append(f"{path.relative_to(ROOT).as_posix()}: {type(error).__name__}: {error}")
    assert failures == []


def test_every_shipped_module_imports_with_syntax_warnings_as_errors():
    # A subprocess, so importing every module does not leave their side effects in the test process. -B keeps it from
    # writing bytecode; the compile test above covers the source itself.
    script = (
        "import importlib, json, sys\n"
        "failures = {}\n"
        "for name in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except BaseException as error:\n"
        "        failures[name] = f'{type(error).__name__}: {error}'\n"
        "print(json.dumps(failures))\n"
    )
    modules = [_module_name(path) for path in _source_files()]
    result = subprocess.run(
        [sys.executable, "-B", "-W", "error::SyntaxWarning", "-c", script, json.dumps(modules)],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {}


def test_py313_job_runs_the_smoke_test_and_the_full_suite_on_python_313():
    job = CI["jobs"]["test-py313"]
    assert job["needs"] == "changes"
    assert job["if"] == "needs.changes.outputs.tests == 'true'"
    assert job["runs-on"] == "windows-latest"
    steps = job["steps"]
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python"))
    assert setup["with"]["python-version"] == "3.13"
    runs = [step.get("run", "") for step in steps]
    smoke = next(index for index, run in enumerate(runs) if "tests/test_import_smoke.py" in run)
    suite = next(index for index, run in enumerate(runs) if run.startswith("python -m pytest tests -q -n 4"))
    assert smoke < suite
    # conftest.py imports most of harness; without --noconftest a broken module stops the run before this test names it.
    assert "--noconftest" in runs[smoke]
    assert "--dist loadfile" in runs[suite]
    # Coverage is measured and uploaded on 3.12 only.
    assert "--cov" not in " ".join(runs)
    assert not any(step.get("uses", "").startswith("actions/upload-artifact") for step in steps)
    assert "test-py313" in CI["jobs"]["gate"]["needs"]
