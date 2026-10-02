"""Consistent SQLite file copies, shared by nightly backups and pre-migration snapshots."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def backup_sqlite(source: Path, dest: Path) -> None:
    """Copy `source` to `dest` with the online backup API and verify the copy's integrity."""
    src = sqlite3.connect(str(source))
    target = sqlite3.connect(str(dest))
    try:
        src.backup(target)  # consistent snapshot while the daemon keeps writing (WAL)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        target.close()
        src.close()
    if check != "ok":
        raise RuntimeError(f"backup copy failed its integrity check: {check}")
