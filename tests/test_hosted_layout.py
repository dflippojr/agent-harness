"""The hosted CLI layout (#991): vendor code lives in `harness/backends/`, and the Runner applies neutral events in one
place.

- `harness/runner.py`, and the hosted half it mixes in (`harness/hosted_runs.py`), have no vendor-named methods and no
  dispatch on a transport's class: every backend's records go through its translator to `Runner._apply`.
- That code writes the session's `run` only through `SessionStore.patch_run` (or `unset_run`), never as a whole value.
- The transports' old names still import from `harness.cli_backends` for one release, as the same objects.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from harness import cli_backends
from harness.runner import Runner

ROOT = Path(__file__).resolve().parent.parent
VENDOR_PREFIXES = ("_claude_", "_codex_", "_cursor_")
RUNNER_FILES = ("harness/runner.py", "harness/hosted_runs.py")
TRANSPORTS = ("ClaudeSession", "CodexSession", "CursorSession", "PrintModeSession")


def _tree(rel: str) -> ast.AST:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)


def _defined(tree: ast.AST) -> list[str]:
    return [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _transport_checks(tree: ast.AST) -> list[int]:
    """Lines that ask which transport a CLI is: isinstance(x, ClaudeSession), isinstance(x, (CodexSession, ...))."""
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"
            and len(node.args) == 2 and any(name in ast.unparse(node.args[1]) for name in TRANSPORTS)]


def _whole_run_writes(tree: ast.AST) -> list[int]:
    """Lines that pass a whole `run` value to a call (update_session(..., run=...))."""
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and any(keyword.arg == "run" for keyword in node.keywords)]


def test_the_runner_has_no_vendor_named_methods():
    assert [name for name in dir(Runner) if name.startswith(VENDOR_PREFIXES)] == []
    for rel in RUNNER_FILES:
        assert [name for name in _defined(_tree(rel)) if name.startswith(VENDOR_PREFIXES)] == [], rel


def test_the_runner_never_dispatches_on_a_transport_class():
    for rel in RUNNER_FILES:
        assert _transport_checks(_tree(rel)) == [], rel


def test_the_hosted_half_and_the_backends_write_run_only_through_patch_run():
    files = ["harness/hosted_runs.py", *sorted(p.relative_to(ROOT).as_posix()
                                               for p in (ROOT / "harness" / "backends").glob("*.py"))]
    assert {rel: _whole_run_writes(_tree(rel)) for rel in files} == {rel: [] for rel in files}


def test_the_audits_flag_what_they_look_for():
    assert _defined(ast.parse("class R:\n    def _codex_delta(self): pass\n")) == ["_codex_delta"]
    assert _transport_checks(ast.parse("if isinstance(cli, (CodexSession, CursorSession)):\n    pass\n")) == [1]
    assert _transport_checks(ast.parse("if isinstance(event, Result):\n    pass\n")) == []
    assert _whole_run_writes(ast.parse("db.update_session(sid, run=run)\n")) == [1]
    assert _whole_run_writes(ast.parse("db.patch_run(sid, turns=1)\n")) == []


def test_the_old_cli_backends_names_still_import_as_the_same_objects():
    for name, module in cli_backends._MOVED.items():
        moved = getattr(importlib.import_module(f"harness.backends.{module}"), name)
        assert getattr(cli_backends, name) is moved, name
    from harness.cli_backends import ClaudeSession, OpencodeJsonl, PRINT_MODE_PARSERS  # the documented old spellings
    assert ClaudeSession.__module__ == "harness.backends.claude"
    assert PRINT_MODE_PARSERS["opencode_jsonl"] is OpencodeJsonl
    with pytest.raises(AttributeError):
        cli_backends.NoSuchName  # noqa: B018 - the attribute access is the test
