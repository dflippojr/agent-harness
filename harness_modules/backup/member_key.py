"""Separate, fingerprint-addressed master-key copies for database snapshots (#414)."""

import base64
from contextlib import closing
import csv
import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

KEY_FILE = "member-keys.key"
WARNING = ("The member key and a database backup together decrypt every member's stored API key; "
           "keep the key copy somewhere other than where the backups are kept off-site.")


def key_dir(cfg, root: Path) -> Path:
    path = Path(cfg.backup.member_key_dir).expanduser() if cfg.backup.member_key_dir else root / "member-keys"
    resolved = path.resolve()
    relative = resolved.relative_to(root.resolve()) if resolved.is_relative_to(root.resolve()) else None
    if relative is not None and (not relative.parts or re.match(r"^\d{4}-\d{2}-\d{2}", relative.parts[0])):
        raise ValueError("backup.member_key_dir must be separate from dated database backup folders")
    return path


def fingerprint(content: bytes) -> str:
    key = base64.b64decode(content.strip(), validate=True)
    if len(key) != 32:
        raise ValueError("member key must contain a 32-byte master key")
    return hashlib.sha256(key).hexdigest()


def restrict(path: Path) -> None:
    """Enforce owner-only access, including a protected Windows DACL."""
    if os.name == "nt":
        # Use the native ACL tool to remove inherited access and grant only the daemon account.
        # This is a freshly created empty file: it has no explicit access rules to preserve.
        identity = _windows_command(["whoami", "/user", "/fo", "csv", "/nh"])
        sid = next(csv.reader(identity.splitlines()))[-1]
        if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
            raise OSError("could not determine the daemon account's Windows SID")
        _windows_command(["icacls", str(path.resolve()), "/inheritance:r", "/grant:r", f"*{sid}:F"])
    else:
        path.chmod(0o600)


def _windows_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"Windows key permissions failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".member-key-", dir=path.parent)
    tmp = Path(name)
    try:
        os.close(fd)
        restrict(tmp)  # protect the file before any secret bytes are written
        tmp.write_bytes(content)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def snapshot_key(cfg, root: Path, database: Path) -> Path | None:
    directory = key_dir(cfg, root)
    source = Path(cfg.data_dir) / KEY_FILE
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("DROP TABLE IF EXISTS backup_member_key")
        conn.commit()
    if not source.exists():
        return None
    content = source.read_bytes()
    digest = fingerprint(content)
    dest = directory / f"{digest}.key"
    write_private(dest, content)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS backup_member_key (fingerprint TEXT NOT NULL)")
        conn.execute("DELETE FROM backup_member_key")
        conn.execute("INSERT INTO backup_member_key VALUES (?)", (digest,))
        conn.commit()
    return dest


def expected_fingerprint(database: Path) -> str:
    conn = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='backup_member_key'").fetchone():
            return ""
        row = conn.execute("SELECT fingerprint FROM backup_member_key").fetchone()
        return row[0] if row else ""
    finally:
        conn.close()
