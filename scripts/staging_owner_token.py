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
import sys
from pathlib import Path

PRODUCTION_DATA_ROOT = Path("D:/Agents/harness")


def _refuse_production(data_dir: Path) -> None:
    resolved = data_dir.resolve() if data_dir.exists() else data_dir
    if resolved == PRODUCTION_DATA_ROOT or PRODUCTION_DATA_ROOT in resolved.parents:
        raise SystemExit(f"refusing to mint a token in the production data root: {resolved}")


def mint(data_dir: Path, token_file: Path, harness_root: Path | None = None) -> str:
    """Revoke every existing key in the staging database, then return one fresh owner token."""
    _refuse_production(data_dir)
    if harness_root is not None:
        sys.path.insert(0, str(harness_root))
    from harness.db import Database

    db = Database(data_dir / "harness.sqlite3")
    try:
        for key in db.list_api_keys():
            if not key.get("revoked_at"):
                db.revoke_api_key(key["id"])
        row, secret = db.create_api_key("staging owner", scopes="admin", kind="owner")
    finally:
        db.close()

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(secret + "\n", encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
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
