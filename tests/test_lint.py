"""Issue #729: ruff's rule set, pyright's scope and baseline, and the CI `lint` job that runs them.

tools/lint/pyright_baseline.py decides what fails; these tests feed it pyright reports instead of running pyright.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location("pyright_baseline", ROOT / "tools" / "lint" / "pyright_baseline.py")
assert _spec and _spec.loader
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


def _diagnostic(file: str, line: int, message: str, rule: str = "reportAttributeAccessIssue",
                severity: str = "error") -> dict:
    return {"file": str(ROOT / file), "severity": severity, "message": message, "rule": rule,
            "range": {"start": {"line": line - 1, "character": 4}, "end": {"line": line - 1, "character": 9}}}


def _report(*diagnostics: dict, files: int = 3) -> dict:
    return {"generalDiagnostics": list(diagnostics), "summary": {"filesAnalyzed": files}}


def _pyright(report: dict, returncode: int = 1):
    def run(command):
        assert command[1:3] == ["-m", "pyright"] and "--outputjson" in command
        return subprocess.CompletedProcess(command, returncode, json.dumps(report), "")
    return run


OLD = _diagnostic("harness/a.py", 10, '"get" is not a known attribute of "None"', "reportOptionalMemberAccess")


# --- configuration ---------------------------------------------------------------------------------------------

def test_ruff_selects_the_small_rule_set_without_formatting_or_per_file_ignores():
    ruff = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]
    assert ruff["lint"]["select"] == ["F", "E9", "B", "PLE"]
    assert "per-file-ignores" not in ruff["lint"] and "ignore" not in ruff["lint"]
    assert "format" not in ruff


def test_pyright_checks_the_shipped_packages_in_basic_mode():
    config = json.loads((ROOT / "pyrightconfig.json").read_text(encoding="utf-8"))
    assert config["include"] == ["harness", "harness_modules", "sdk", "macrunner"]
    assert config["typeCheckingMode"] == "basic"


def test_lint_job_runs_ruff_and_the_baseline_on_every_change():
    lint = CI["jobs"]["lint"]
    assert "if" not in lint and "needs" not in lint
    runs = [step.get("run", "") for step in lint["steps"]]
    assert any(run.startswith("python -m ruff check") for run in runs)
    assert "python tools/lint/pyright_baseline.py" in runs
    assert "lint" in CI["jobs"]["gate"]["needs"]


def test_committed_baseline_is_in_the_form_write_produces():
    text = tool.BASELINE.read_text(encoding="utf-8")
    rows = json.loads(text)
    found = [tool.Error(row["file"], 1, 1, row["rule"], row["message"]) for row in rows for _ in range(row["count"])]
    assert tool.baseline_document(found) == text
    assert all(not Path(row["file"]).is_absolute() for row in rows)


# --- the baseline check ----------------------------------------------------------------------------------------

def test_errors_are_relative_and_warnings_are_left_out():
    warning = _diagnostic("harness/a.py", 3, "unused", severity="warning")
    message = _diagnostic("harness/b.py", 7, f"Import from {ROOT / 'harness'} failed")
    found = tool.errors(_report(OLD, warning, message))
    assert found == [
        tool.Error("harness/a.py", 10, 5, "reportOptionalMemberAccess", '"get" is not a known attribute of "None"'),
        tool.Error("harness/b.py", 7, 5, "reportAttributeAccessIssue", f"Import from {Path('<root>') / 'harness'} failed"),
    ]


def test_compare_ignores_moved_lines_and_flags_extra_errors_of_a_key():
    baseline = tool.read_baseline(tool.baseline_document(tool.errors(_report(OLD))))
    moved = _diagnostic("harness/a.py", 40, OLD["message"], OLD["rule"])
    assert tool.compare(tool.errors(_report(moved)), baseline) == ([], 0)
    new, fixed = tool.compare(tool.errors(_report(OLD, moved)), baseline)
    assert [error.line for error in new] == [10, 40] and fixed == 0
    assert tool.compare([], baseline) == ([], 1)


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    path = tmp_path / "pyright-baseline.json"
    monkeypatch.setattr(tool, "BASELINE", path)
    monkeypatch.setattr(tool, "ROOT", ROOT)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return path


def test_write_then_check_passes(baseline, capsys):
    assert tool.main(["--write"], _pyright(_report(OLD))) == 0
    assert json.loads(baseline.read_text(encoding="utf-8")) == [
        {"file": "harness/a.py", "rule": "reportOptionalMemberAccess", "message": OLD["message"], "count": 1}]
    assert tool.main([], _pyright(_report(OLD))) == 0
    assert "1 errors, 1 recorded in the baseline" in capsys.readouterr().out


def test_a_new_undefined_name_fails_with_its_location(baseline, capsys):
    tool.main(["--write"], _pyright(_report(OLD)))
    undefined = _diagnostic("harness/catalog.py", 81, '"Path" is not defined', "reportUndefinedVariable")
    assert tool.main([], _pyright(_report(OLD, undefined))) == 1
    out = capsys.readouterr()
    assert 'harness/catalog.py:81:5: reportUndefinedVariable: "Path" is not defined' in out.out
    assert "do not grow the baseline" in out.err


def test_github_annotations_escape_multiline_messages(baseline, capsys, monkeypatch):
    baseline.write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    detail = _diagnostic("harness/a.py", 2, 'Argument missing\n  for "x" (100%)', "reportCallIssue")
    assert tool.main([], _pyright(_report(detail))) == 1
    assert ("::error file=harness/a.py,line=2,col=5,title=pyright reportCallIssue::"
            'Argument missing%0A  for "x" (100%25)') in capsys.readouterr().out


def test_fixed_errors_pass_and_ask_for_a_smaller_baseline(baseline, capsys):
    tool.main(["--write"], _pyright(_report(OLD)))
    assert tool.main([], _pyright(_report(), returncode=0)) == 0
    assert "1 baseline error(s) are fixed" in capsys.readouterr().out


@pytest.mark.parametrize("returncode, report, reason", [
    (2, _report(), "pyright exited 2"),
    (0, _report(files=0), "analysed no files"),
])
def test_a_broken_pyright_run_is_an_error_not_a_pass(baseline, returncode, report, reason):
    baseline.write_text("[]\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=reason):
        tool.main([], _pyright(report, returncode))
