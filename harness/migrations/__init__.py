"""Ordered, versioned SQLite schema migrations (issue #256).

`PRAGMA user_version` records the last applied step. Version 0 is any pre-versioning database: `Database`
bootstraps it with `SCHEMA` and the frozen `baseline.LEGACY_COLUMNS`, then stamps `BASELINE_VERSION`.
Every later change is a module `NNNN_<name>.py` in this package (4 digits, gap-free from 0046) exposing
`up(conn)`. `up` uses `conn.execute` only, never `executescript` (it commits) and never its own
transaction: each step runs inside `BEGIN IMMEDIATE` ... `COMMIT` together with its `user_version` bump.
See docs/migrations.md.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..sqlite_backup import backup_sqlite
from .baseline import BASELINE_VERSION

Up = Callable[[sqlite3.Connection], None]
Step = tuple[int, Up]

_MODULE_NAME = re.compile(r"^(\d+)_\w+$")
_VALID_NAME = re.compile(r"^\d{4}_[a-z0-9_]+$")


class MigrationError(RuntimeError):
    """A migration step failed; that step was rolled back and earlier steps stay applied."""


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer harness than this code."""


def validate(steps: Iterable[Step]) -> list[Step]:
    """Sort `steps` by number and fail fast on duplicates, gaps, or a missing/non-callable `up`."""
    ordered = sorted(steps, key=lambda s: s[0])
    expected = BASELINE_VERSION + 1
    for number, up in ordered:
        if not isinstance(number, int) or isinstance(number, bool):
            raise MigrationError(f"migration number {number!r} is not an integer")
        if number < expected:
            raise MigrationError(f"duplicate or out-of-range migration number {number:04d}"
                                 f" (next expected {expected:04d})")
        if number > expected:
            raise MigrationError(f"migration numbers have a gap: {expected:04d} is missing before {number:04d}")
        if not callable(up):
            raise MigrationError(f"migration {number:04d} has no callable up(conn)")
        expected += 1
    return ordered


def discover(package: str = __name__) -> list[Step]:
    """Import every `NNNN_<name>` module of `package`, validated and sorted by number."""
    pkg = importlib.import_module(package)
    steps: list[Step] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        match = _MODULE_NAME.match(info.name)
        if not match:
            continue
        if not _VALID_NAME.match(info.name):
            raise MigrationError(f"migration module {info.name!r} must be named NNNN_<lowercase_name>")
        module = importlib.import_module(f"{package}.{info.name}")
        up = getattr(module, "up", None)
        if not callable(up):
            raise MigrationError(f"migration module {info.name!r} has no up(conn) function")
        steps.append((int(match.group(1)), up))
    return validate(steps)


def latest_version(steps: Sequence[Step]) -> int:
    return steps[-1][0] if steps else BASELINE_VERSION


def too_new_message(current: int, latest: int) -> str:
    return (f"database is at schema v{current}, this code knows up to v{latest}: "
            "upgrade the harness or restore a backup")


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def check_not_too_new(current: int, steps: Sequence[Step]) -> None:
    latest = latest_version(steps)
    if current > latest:
        raise SchemaTooNewError(too_new_message(current, latest))


def backup_path(db_path: Path, version: int) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return db_path.parent / "pre-migration" / f"harness-v{version}-{stamp}.sqlite3"


def apply_pending(conn: sqlite3.Connection, db_path: Path, steps: Sequence[Step]) -> Path | None:
    """Apply every step above the current `user_version`, one transaction each.

    `conn` must be in autocommit mode (`isolation_level=None`). Databases already at or past the baseline get
    a snapshot under `<db dir>/pre-migration/` before the first step; a database that was just bootstrapped
    from version 0 holds nothing worth restoring, so it gets none. Returns the backup path, if any.
    """
    current = user_version(conn)
    check_not_too_new(current, steps)
    pending = [(number, up) for number, up in steps if number > current]
    if not pending:
        return None
    backup = None
    if current >= BASELINE_VERSION:
        backup = backup_path(db_path, current)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup_sqlite(db_path, backup)
    for number, up in pending:
        conn.execute("BEGIN IMMEDIATE")
        try:
            up(conn)
            conn.execute(f"PRAGMA user_version = {int(number)}")
            conn.execute("COMMIT")
        except BaseException as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if not isinstance(e, Exception):
                raise
            where = backup or "none (database was bootstrapped in this start)"
            raise MigrationError(f"schema migration {number:04d} failed ({type(e).__name__}: {e});"
                                 f" database left at v{user_version(conn)}; pre-migration backup: {where}") from e
    return backup
