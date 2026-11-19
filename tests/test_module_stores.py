"""Module-owned queries live in module stores, not in the core Database (#992).

The schema of every module table stays in the core's frozen baseline (#334: existing installs and backups keep it),
but no `Database` method may query one: a module's queries are methods of its own store, which runs them on the
core's writer queue and pooled readers and forwards nothing else to the Database.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

import harness.db as core_db
from harness.db import Database

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "harness_modules"

# Every table a module owns: skills, jobs and templates, runner pairing codes, endpoint requests and images.
MODULE_TABLES = (
    "skill_proposals", "skill_installed", "skill_versions", "skill_project_allowlist", "skill_rejected",
    "skill_review_jobs", "jobs", "templates", "runner_pairing_codes", "endpoint_requests", "images",
)
_SQL_NAMES = re.compile(r'\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+"?(%s)\b' % "|".join(MODULE_TABLES), re.IGNORECASE)
# The frozen baseline the core creates a database from (#334 keeps module tables in it): schema, not queries.
_BASELINE = frozenset({"SCHEMA", "APP_SETTINGS_SCHEMA"})


def _strings(fn: ast.AST) -> list[str]:
    """The string constants of `fn` (f-string parts included), without its docstring."""
    body = getattr(fn, "body", [])
    doc = body[0].value if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) else None
    return [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n is not doc]


def module_table_queries(source: str, class_name: str, constants: dict) -> dict[str, set[str]]:
    """{method: module tables its SQL names} for class `class_name` in `source`. A method's SQL is its own string
    literals plus the module-level string constants it names (as `_API_KEY_LISTING` is)."""
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == class_name)
    found: dict[str, set[str]] = {}
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tables = {m.group(1).lower() for text in _strings(fn) for m in _SQL_NAMES.finditer(text)}
        for name in {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}:
            value = constants.get(name)
            if isinstance(value, str) and name not in _BASELINE:
                tables |= {m.group(1).lower() for m in _SQL_NAMES.finditer(value)}
        if tables:
            found[fn.name] = tables
    return found


def test_the_scan_finds_literal_formatted_and_constant_sql():
    source = '''
LISTING = "SELECT k.id, (SELECT COUNT(*) FROM endpoint_requests r) FROM api_keys k"
OTHER = "SELECT * FROM jobs"
class Database:
    def a(self):
        """Lists the jobs and templates (a docstring, not SQL)."""
        return self.conn.execute("SELECT * FROM templates WHERE id = ?")
    def b(self, sets):
        return self.conn.execute(f"UPDATE skill_proposals SET {sets} WHERE id = ?")
    def c(self):
        return self.conn.execute(OTHER)
    def d(self):
        self._writer.jobs.put(None)
        return self.conn.execute('INSERT INTO "images" (id) VALUES (?)')
    def e(self):
        return self.conn.execute("SELECT * FROM sessions WHERE job_id = ?")
'''
    namespace: dict = {}
    exec(source, namespace)  # noqa: S102 - the synthetic source above, to resolve its constants
    assert module_table_queries(source, "Database", namespace) == {
        "a": {"templates"}, "b": {"skill_proposals"}, "c": {"jobs"}, "d": {"images"}}


def test_database_defines_no_method_that_queries_a_module_table():
    source = Path(core_db.__file__).read_text(encoding="utf-8")
    assert module_table_queries(source, "Database", vars(core_db)) == {}


def _store_classes():
    for path in sorted(MODULES.glob("*/store.py")):
        module = importlib.import_module(f"harness_modules.{path.parent.name}.store")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Store"):
                yield path, node, getattr(module, node.name)


def test_every_module_owning_tables_has_a_store():
    assert {path.parent.name for path, _, _ in _store_classes()} >= {"skills", "jobs", "runners", "endpoint",
                                                                     "images"}


def test_no_module_store_forwards_unknown_attributes_to_the_database(tmp_path):
    stores = list(_store_classes())
    assert stores
    for path, node, _ in stores:
        hooks = [f.name for f in node.body if isinstance(f, ast.FunctionDef)
                 and f.name in ("__getattr__", "__getattribute__")]
        assert not hooks, f"{path.relative_to(ROOT)}: {node.name} defines {hooks}"
    db = Database(tmp_path / "state.sqlite")
    try:
        for _, _, cls in stores:
            store = cls(db)
            for core in ("get_session", "insert_event", "list_api_keys", "create_api_key", "get_meta"):
                assert not hasattr(store, core), f"{cls.__name__}.{core}"
            with pytest.raises(AttributeError):
                store.list_sessions  # noqa: B018 - the access itself is the check
    finally:
        db.close()
