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

from harness.modules import check_member_key_dir

KEY_FILE = "member-keys.key"
WARNING = ("The member key and a database backup together decrypt every member's stored API key; "
           "keep the key copy somewhere other than where the backups are kept off-site.")


def key_dir(cfg, root: Path) -> Path:
    """The member-key directory, resolved once: root/member-keys, or backup.member_key_dir checked as at config load."""
    base = root.resolve()
    if cfg.backup.member_key_dir:
        resolved = check_member_key_dir(cfg.backup.member_key_dir)
    else:
        resolved = (base / "member-keys").resolve()
        if resolved.parent != base:  # a member-keys symlink or junction pointing elsewhere
            raise ValueError("the default member-key directory must stay inside backup.dir")
    relative = resolved.relative_to(base) if resolved.is_relative_to(base) else None
    if relative is not None and (not relative.parts or re.match(r"^\d{4}-\d{2}-\d{2}", relative.parts[0])):
        raise ValueError("backup.member_key_dir must be separate from dated database backup folders")
    return resolved


def fingerprint(content: bytes) -> str:
    key = base64.b64decode(content.strip(), validate=True)
    if len(key) != 32:
        raise ValueError("member key must contain a 32-byte master key")
    return hashlib.sha256(key).hexdigest()


def restrict(path: Path) -> None:
    """Enforce owner-only access, including a protected Windows DACL."""
    if os.name == "nt":
        # Reset explicit temp-file grants too (Python/Windows can add SYSTEM, Administrators and Owner Rights).
        # The file is still empty; protect it before writing any key bytes.
        identity = _windows_command(["whoami", "/user", "/fo", "csv", "/nh"])
        sid = next(csv.reader(identity.splitlines()))[-1]
        if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
            raise OSError("could not determine the daemon account's Windows SID")
        _windows_command(["icacls", str(path.resolve()), "/reset"])
        _windows_command(["icacls", str(path.resolve()), "/inheritance:r", "/grant:r", f"*{sid}:F"])
    else:
        path.chmod(0o600)


def _windows_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"Windows key permissions failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def write_private(directory: Path, name: str, content: bytes) -> Path:
    """Atomically write an owner-only file called `name` directly inside `directory`; any other target is refused
    before the filesystem is touched (#728)."""
    base = directory.resolve()
    target = (base / name).resolve()
    if Path(name).name != name or target.parent != base or not target.is_relative_to(base):
        raise ValueError(f"refusing to write {name!r} outside {base}")
    base.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".member-key-", dir=base)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            restrict(tmp)  # protect the file before any secret bytes are written
            f.write(content)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def snapshot_key(cfg, root: Path, database: Path, warnings: list[str]) -> Path | None:
    source = Path(cfg.data_dir) / KEY_FILE
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("DROP TABLE IF EXISTS backup_member_key")
        conn.commit()
    directory = key_dir(cfg, root)
    if not source.exists():
        return None
    try:
        content = source.read_bytes()
        digest = fingerprint(content)
    except (OSError, ValueError):
        warnings.append("Member key backup skipped: the source key is unreadable or invalid; "
                        "members may need to re-add their API keys after restore.")
        return None
    dest = write_private(directory, f"{digest}.key", content)
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
