"""Erase one provider-CLI conversation from a CLI state directory (#371).

The daemon never mounts a CLI volume on the host, so this file runs inside a throwaway container
(`cli_domains.erase_history`) as ``python3 -c <this source> <state dir> <conversation id>``. It has no imports from the
harness for that reason. Tests call `erase` directly on a temporary directory.

What goes, for the conversation id (Claude's session id, Codex's thread id, Cursor's chat id):
- every file or directory whose name contains the id, at any depth (Claude `projects/*/<id>.jsonl` and `<id>/`,
  `file-history/<id>`, `todos/<id>-*`, `session-env/<id>`; Codex `sessions/**/rollout-*-<id>.jsonl` and
  `archived_sessions`; Cursor `config/chats/<hash>/<id>/`);
- the lines naming it in the top-level prompt indexes (`history.jsonl`, `session_index.jsonl`);
- its rows in the top-level SQLite stores (Codex `state_*.sqlite` and friends): `threads.id` and every `thread_id`.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys

INDEXES = ("history.jsonl", "session_index.jsonl")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}")


def _remove_named(root: str, conv_id: str) -> int:
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [d for d in dirnames if conv_id in d]:
            shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
            dirnames.remove(name)
            removed += 1
        for name in filenames:
            if conv_id in name:
                os.remove(os.path.join(dirpath, name))
                removed += 1
    return removed


def _filter_index(path: str, conv_id: str) -> int:
    with open(path, "rb") as f:
        lines = f.readlines()
    kept = [line for line in lines if conv_id.encode() not in line]
    if len(kept) == len(lines):
        return 0
    with open(path, "r+b") as f:  # in place, so the file keeps its owner and mode
        f.writelines(kept)
        f.truncate()
    return len(lines) - len(kept)


def _delete_rows(path: str, conv_id: str) -> int:
    removed = 0
    conn = sqlite3.connect(path, timeout=10)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            keys = ["thread_id"] if "thread_id" in columns else []
            if table == "threads" and "id" in columns:
                keys.append("id")
            for key in keys:
                removed += conn.execute(f'DELETE FROM "{table}" WHERE "{key}" = ?', (conv_id,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return removed


def erase(root: str, conv_id: str) -> int:
    """Remove conversation `conv_id` from the CLI state under `root`; returns how many files, lines and rows went."""
    if not SAFE_ID.fullmatch(conv_id):
        raise ValueError(f"refusing to erase by an unsafe conversation id {conv_id!r}")
    removed = _remove_named(root, conv_id)
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        if name in INDEXES:
            removed += _filter_index(path, conv_id)
        elif name.endswith((".sqlite", ".db")):
            removed += _delete_rows(path, conv_id)
    return removed


if __name__ == "__main__":
    print(erase(sys.argv[1], sys.argv[2]))
