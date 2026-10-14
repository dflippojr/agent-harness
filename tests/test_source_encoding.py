"""Every module compiles and every docstring encodes as UTF-8 (a lone surrogate in a non-raw docstring broke the 3.13 import)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _sources() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.py") if not SKIPPED_DIRS.intersection(p.relative_to(ROOT).parts))


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_module_compiles_with_encodable_docstrings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    compile(tree, str(path), "exec")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                try:
                    doc.encode("utf-8")
                except UnicodeEncodeError:
                    pytest.fail(f"{path.name}:{getattr(node, 'lineno', 1)} docstring has a lone surrogate; escape it or use a raw string")
