"""The configuration audit trail in a backup (#469): ``data_dir/config-audit.jsonl``.

The trail is append-only JSONL written by the settings service. A backup copies a stable prefix: the byte length is
captured first and only whole lines inside it are kept, so a record appended (or half-written) afterwards belongs to
a later backup. The JSONL and the SQLite/overlay snapshots are taken at different moments; they are not one
transaction. Every kept line must be a UTF-8 JSON object. Errors are stable text and never echo record content.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from harness.modules import storage

AUDIT_FILE = "config-audit.jsonl"
RESTORED_DIR = "restored-audits"


class AuditError(ValueError):
    """The audit file is unusable; the message never holds record content."""


def _check_link(path: Path, what: str) -> None:
    if storage.is_reparse_point(path):
        raise AuditError(f"{what} is a symlink or reparse point")


def validate(data: bytes) -> None:
    """Raise AuditError unless every line of `data` is a UTF-8 JSON object (blank lines are not allowed)."""
    if not data:
        return  # an empty trail has no records
    for number, line in enumerate(data.split(b"\n")[:-1] if data.endswith(b"\n") else data.split(b"\n"), 1):
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AuditError(f"line {number} is not UTF-8 JSON") from None
        if not isinstance(record, dict):
            raise AuditError(f"line {number} is not a JSON object")


def stable_prefix(path: Path) -> bytes | None:
    """The complete-line prefix of `path` as of its size when first opened; None if the file doesn't exist."""
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise AuditError(f"cannot be read: {type(e).__name__}") from None
    _check_link(path, "the config audit trail")
    try:
        with open(path, "rb") as f:
            boundary = os.fstat(f.fileno()).st_size
            data = f.read(boundary)  # never past the captured boundary
    except OSError as e:
        raise AuditError(f"cannot be read: {type(e).__name__}") from None
    data = data[:data.rfind(b"\n") + 1]  # drop an incomplete final line
    validate(data)
    return data


def read_snapshot(path: Path) -> bytes:
    """A backup's snapshot, whole; the whole file must be complete valid lines."""
    _check_link(path, "the audit snapshot")
    real = os.path.realpath(path)
    if os.path.basename(real) != AUDIT_FILE or os.path.dirname(real) != os.path.realpath(path.parent):
        raise AuditError("is not the expected snapshot file")
    try:
        with open(real, "rb") as f:
            data = f.read()
    except OSError as e:
        raise AuditError(f"cannot be read: {type(e).__name__}") from None
    if data and not data.endswith(b"\n"):
        raise AuditError("ends in an incomplete line")
    validate(data)
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
