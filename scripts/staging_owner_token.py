"""Mint the staging slot's own owner token, revoking any earlier staging token (issue #129).

The staging slot never receives production credentials: no copied SQLite database, no production `ho-` token, and no
app keys. This creates a fresh owner-scoped key in the staging database only, writes it to a file the owner reads on
the tower, and prints nothing but its prefix, so the secret never lands in an Actions log.

    python scripts/staging_owner_token.py --data-dir D:\\Agents\\harness-staging \\
        --token-file D:\\Agents\\harness-staging\\owner-token.txt --harness-root D:\\Projects\\agent-harness-staging

`--harness-root` is the staging checkout: the candidate's own `harness.db` writes the candidate's own schema.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PRODUCTION_DATA_ROOT = Path("D:/Agents/harness")
PRODUCTION_CHECKOUT = Path("D:/Projects/agent-harness")
TOKEN_BASENAME = "owner-token.txt"
# Absolute paths with no '..', spaces, or other separators. matched.group(0) is the write sink.
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/)(?:[A-Za-z0-9._-]+[\\/])*[A-Za-z0-9._-]+$")


def _collapsed(path: Path) -> Path:
    return Path(os.path.normpath(os.path.expanduser(str(path))))


def _refuse_unsafe_path(path: Path, label: str) -> None:
    raw = str(path)
    if raw.startswith("-") or any(
            part.startswith("-") for part in Path(raw.replace("\\", "/")).parts
            if part not in ("/", ".") and not (len(part) == 2 and part.endswith(":"))):
        raise SystemExit(f"refusing {label} that starts with '-': {path}")
    if ".." in raw:
        raise SystemExit(f"refusing {label} that contains '..': {path}")


def _refuse_production(path: Path) -> None:
    resolved = path.resolve() if path.exists() else path
    for candidate in (resolved, _collapsed(path), path):
        candidate = Path(candidate)
        if candidate == PRODUCTION_DATA_ROOT or PRODUCTION_DATA_ROOT in candidate.parents:
            raise SystemExit(f"refusing to mint a token in the production data root: {candidate}")
        if candidate == PRODUCTION_CHECKOUT or PRODUCTION_CHECKOUT in candidate.parents:
            raise SystemExit(f"refusing to mint a token from the production checkout: {candidate}")


def _require_token_file(token_file: Path, data_dir: Path) -> None:
    expected = _collapsed(data_dir) / TOKEN_BASENAME
    if _collapsed(token_file) != expected:
        raise SystemExit(f"refusing to write a token outside the staging data dir: {token_file}")


def _sanitize_fs_path(path: Path, label: str) -> str:
    raw = os.path.normpath(os.path.expanduser(str(path)))
    matched = _ABS_PATH.fullmatch(raw)
    if matched is None:
        raise SystemExit(f"refusing {label} that is not a safe absolute path: {path}")
    return matched.group(0)


def mint(data_dir: Path, token_file: Path, harness_root: Path | None = None) -> str:
    """Revoke every existing key in the staging database, then return one fresh owner token."""
    data_dir, token_file = Path(data_dir), Path(token_file)
    _refuse_unsafe_path(data_dir, "data dir")
    _refuse_unsafe_path(token_file, "token file")
    if harness_root is not None:
        harness_root = Path(harness_root)
        _refuse_unsafe_path(harness_root, "harness root")
        _refuse_production(harness_root)
    _refuse_production(data_dir)
    _refuse_production(token_file)
    _require_token_file(token_file, data_dir)
    data_dir_raw = _sanitize_fs_path(data_dir, "data dir")
    destination = os.path.join(data_dir_raw, TOKEN_BASENAME)
    if harness_root is not None:
        sys.path.insert(0, _sanitize_fs_path(harness_root, "harness root"))
    from harness.db import Database

    db = Database(Path(data_dir_raw) / "harness.sqlite3")
    try:
        for key in db.list_api_keys():
            if not key.get("revoked_at"):
                db.revoke_api_key(key["id"])
        row, secret = db.create_api_key("staging owner", scopes="admin", kind="owner")
    finally:
        db.close()

    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(secret + "\n")
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return row["prefix"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--harness-root", default="")
    args = parser.parse_args(argv)

    prefix = mint(Path(args.data_dir), Path(args.token_file),
                  Path(args.harness_root) if args.harness_root else None)
    print(f"staging owner token rotated (prefix {prefix}); read it on the tower with:")
    print(f"  Get-Content {args.token_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
