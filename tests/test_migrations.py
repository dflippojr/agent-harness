"""Versioned SQLite schema migrations (issue #256)."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import doctor, migrations
from harness.db import APP_SETTINGS_SCHEMA, SCHEMA, Database
from harness.maintenance import Maintenance
from harness.migrations.baseline import BASELINE_VERSION, LEGACY_COLUMNS


def _legacy_db(path: Path, skip: set[tuple[str, str]] = frozenset()) -> None:
    """What the pre-#256 `Database` built: SCHEMA plus every add-column entry, user_version left at 0."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.executescript(APP_SETTINGS_SCHEMA)
    for table, column, definition in LEGACY_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing and (table, column) not in skip:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
    conn.close()


def _snapshot(path: Path) -> tuple:
    conn = sqlite3.connect(str(path))
    try:
        master = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        tables = [name for kind, name, _, _ in master if kind == "table"]
        info = {t: conn.execute(f"PRAGMA table_info('{t}')").fetchall() for t in tables}
        indexes = {t: sorted(conn.execute(f"PRAGMA index_list('{t}')").fetchall()) for t in tables}
        return master, info, indexes
    finally:
        conn.close()


def _version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_note_column(conn):
    conn.execute("ALTER TABLE sessions ADD COLUMN note TEXT NOT NULL DEFAULT ''")


def backfill_note(conn):
    conn.execute("UPDATE sessions SET note = 'title:' || title")


def index_note(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_note ON sessions(note)")


SYNTHETIC = [(46, add_note_column), (47, backfill_note), (48, index_note)]


def _seed_session(path: Path, sid: str = "s1") -> None:
    db = Database(path)
    db.insert_session({"id": sid, "title": "hello", "project": "p", "target": "t", "model": "m", "status": "idle",
                       "workspace": "w", "created_at": 1.0, "updated_at": 1.0, "context": []})
    db.close()


def test_baseline_is_frozen_at_45():
    assert BASELINE_VERSION == 45 and len(LEGACY_COLUMNS) == 45
    assert migrations.discover() == []  # no real 0046+ step ships with #256


def test_fresh_database_matches_pre_versioning_build(tmp_path):
    legacy, fresh = tmp_path / "legacy.db", tmp_path / "fresh.db"
    _legacy_db(legacy)
    Database(legacy).close()
    Database(fresh).close()
    assert _version(fresh) == BASELINE_VERSION
    assert _snapshot(fresh) == _snapshot(legacy)
    assert not (tmp_path / "pre-migration").exists()


def test_version_zero_database_is_stamped_and_repaired_without_backup(tmp_path):
    path = tmp_path / "old.db"
    _legacy_db(path, skip={("images", "provenance"), ("sessions", "effort")})
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO sessions (id, title, project, target, model, status, workspace, created_at,"
                 " updated_at, context) VALUES ('s1', 'kept', 'p', 't', 'm', 'idle', 'w', 1, 1, '[]')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.get_session("s1")["title"] == "kept"
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(images)")}
    assert "provenance" in cols
    db.close()
    assert _version(path) == BASELINE_VERSION
    assert not (tmp_path / "pre-migration").exists()


def test_synthetic_migrations_apply_in_order_after_a_backup(tmp_path):
    path = tmp_path / "harness.db"
    _seed_session(path)
    db = Database(path, migrations=SYNTHETIC)
    assert db.get_session("s1")["note"] == "title:hello"
    assert "idx_sessions_note" in {r["name"] for r in db.conn.execute("PRAGMA index_list(sessions)")}
    db.close()
    assert _version(path) == 48
    backups = list((tmp_path / "pre-migration").glob("harness-v45-*.sqlite3"))
    assert len(backups) == 1
    # The snapshot was taken before the first step.
    assert _version(backups[0]) == 45
    with closing(sqlite3.connect(str(backups[0]))) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "note" not in cols
    # Reopening with nothing pending takes no further backup. Count backups only: opening one may leave
    # -wal/-shm sidecars until its connection is finalized, which coverage tracing delays.
    Database(path, migrations=SYNTHETIC).close()
    assert len(list((tmp_path / "pre-migration").glob("harness-v*.sqlite3"))) == 1


def test_fresh_database_runs_migrations_without_backup(tmp_path):
    path = tmp_path / "harness.db"
    Database(path, migrations=SYNTHETIC).close()
    assert _version(path) == 48
    assert not (tmp_path / "pre-migration").exists()


def test_version_zero_database_with_data_is_backed_up_before_numbered_steps(tmp_path):
    # An install from before #256 that upgrades straight to a release with 0046+ steps.
    path = tmp_path / "old.db"
    _legacy_db(path)
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("INSERT INTO sessions (id, title, project, target, model, status, workspace, created_at,"
                     " updated_at, context) VALUES ('s1', 'kept', 'p', 't', 'm', 'idle', 'w', 1, 1, '[]')")
        conn.commit()
    db = Database(path, migrations=SYNTHETIC)
    assert db.get_session("s1")["note"] == "title:kept"
    db.close()
    backups = list((tmp_path / "pre-migration").glob("harness-v45-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(str(backups[0]))) as conn:
        assert conn.execute("SELECT title FROM sessions WHERE id = 's1'").fetchone()[0] == "kept"
        assert "note" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}


def test_failing_step_rolls_back_only_itself(tmp_path):
    path = tmp_path / "harness.db"
    _seed_session(path)

    def half_then_fail(conn):
        conn.execute("UPDATE sessions SET note = 'partial'")
        raise ValueError("boom")

    with pytest.raises(migrations.MigrationError) as err:
        Database(path, migrations=[(46, add_note_column), (47, half_then_fail), (48, index_note)])
    message = str(err.value)
    backups = list((tmp_path / "pre-migration").glob("*.sqlite3"))
    assert len(backups) == 1 and str(backups[0]) in message
    assert "0047" in message and "boom" in message
    assert _version(path) == 46
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT note FROM sessions WHERE id = 's1'").fetchone()[0] == ""
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_sessions_note'").fetchone()
    conn.close()
    # The fixed code resumes from the failed step.
    Database(path, migrations=SYNTHETIC).close()
    assert _version(path) == 48


def test_too_new_database_is_refused_untouched_and_doctor_reports(tmp_path, capsys):
    path = tmp_path / "harness.db"
    Database(path).close()
    cfg = SimpleNamespace(db_path=path)

    r = doctor.Report()
    doctor.check_schema_version(r, cfg)
    assert (r.failed, r.warned) == (0, 0) and "v45" in capsys.readouterr().out

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    before = _digest(path)
    with pytest.raises(migrations.SchemaTooNewError) as err:
        Database(path)
    expected = migrations.too_new_message(99, BASELINE_VERSION)
    assert str(err.value) == expected
    assert _digest(path) == before

    r = doctor.Report()
    doctor.check_schema_version(r, cfg)
    assert r.failed == 1 and expected in capsys.readouterr().out
    assert _digest(path) == before

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    r = doctor.Report()
    doctor.check_schema_version(r, cfg)
    assert (r.failed, r.warned) == (0, 1) and "will migrate" in capsys.readouterr().out


def test_doctor_reports_a_broken_migration_module_instead_of_crashing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "harness.db"
    Database(path).close()

    def broken():
        raise NameError("name 'oops' is not defined")  # what a typo in a NNNN_*.py raises at import

    monkeypatch.setattr(migrations, "discover", broken)
    r = doctor.Report()
    doctor.check_schema_version(r, SimpleNamespace(db_path=path))
    out = capsys.readouterr().out
    assert r.failed == 1 and "could not load migrations: NameError" in out


def test_doctor_skips_missing_database(tmp_path, capsys):
    r = doctor.Report()
    doctor.check_schema_version(r, SimpleNamespace(db_path=tmp_path / "none.db"))
    assert (r.failed, r.warned) == (0, 0) and "skipped" in capsys.readouterr().out
    assert not (tmp_path / "none.db").exists()


@pytest.mark.parametrize("steps, fragment", [
    ([(46, add_note_column), (46, index_note)], "duplicate"),
    ([(46, add_note_column), (48, index_note)], "gap"),
    ([(47, add_note_column)], "gap"),
    ([(46, None)], "no callable up"),
])
def test_validate_rejects_bad_step_lists(steps, fragment):
    with pytest.raises(migrations.MigrationError, match=fragment):
        migrations.validate(steps)


def _package(root: Path, name: str, files: dict[str, str]) -> str:
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for filename, body in files.items():
        (pkg / filename).write_text(body)
    return name


@pytest.mark.parametrize("files, fragment", [
    ({"0046_a.py": "def up(conn): pass\n", "0046_b.py": "def up(conn): pass\n"}, "duplicate"),
    ({"0046_a.py": "def up(conn): pass\n", "0048_c.py": "def up(conn): pass\n"}, "gap"),
    ({"0046_a.py": "x = 1\n"}, "no up"),
    ({"46_a.py": "def up(conn): pass\n"}, "NNNN_"),
])
def test_discover_rejects_bad_packages(tmp_path, monkeypatch, files, fragment):
    monkeypatch.syspath_prepend(str(tmp_path))
    name = _package(tmp_path, f"mig_bad_{abs(hash(tuple(files)))}", files)
    with pytest.raises(migrations.MigrationError, match=fragment):
        migrations.discover(name)
    sys.modules.pop(name, None)


def test_discover_loads_numbered_modules_in_order(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    name = _package(tmp_path, "mig_good", {
        "0047_second.py": "def up(conn):\n    conn.execute('CREATE TABLE b (x)')\n",
        "0046_first.py": "def up(conn):\n    conn.execute('CREATE TABLE a (x)')\n",
        "helpers.py": "",
    })
    steps = migrations.discover(name)
    assert [n for n, _ in steps] == [46, 47]
    path = tmp_path / "harness.db"
    Database(path, migrations=steps).close()
    assert _version(path) == 47


def test_baseline_plus_migrations_equals_fresh_build(tmp_path):
    migrated, fresh = tmp_path / "a" / "harness.db", tmp_path / "b" / "harness.db"
    _seed_session(migrated)
    Database(migrated, migrations=SYNTHETIC).close()
    Database(fresh, migrations=SYNTHETIC).close()
    assert _snapshot(migrated) == _snapshot(fresh)
    # And with the real (empty) migration set, a baseline DB equals a fresh one.
    legacy, fresh_real = tmp_path / "legacy.db", tmp_path / "fresh_real.db"
    _legacy_db(legacy)
    Database(legacy).close()
    Database(fresh_real).close()
    assert _snapshot(legacy) == _snapshot(fresh_real)


def test_maintenance_backup_delegates_to_backup_sqlite(tmp_path):
    path = tmp_path / "harness.db"
    _seed_session(path)
    maint = Maintenance.__new__(Maintenance)
    maint.cfg = SimpleNamespace(db_path=path)
    copy = tmp_path / "copy.sqlite3"
    maint._backup_db(copy)
    conn = sqlite3.connect(str(copy))
    assert conn.execute("SELECT title FROM sessions").fetchone()[0] == "hello"
    conn.close()
