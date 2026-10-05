"""Erase one provider-CLI conversation from a CLI state directory (#371).

The daemon never mounts a CLI volume on the host, so this file runs inside a throwaway container
(`cli_domains.erase_history`) as ``python3 - <conversation id>``, with this source on stdin and the domain's state
volume at `STATE_DIR`. It has no imports from the harness for that reason. Tests call `erase` directly on a temporary
directory.

What goes, for the conversation id (Claude's session id, Codex's thread id, Cursor's chat id):
- every file or directory whose name contains the id, at any depth (Claude `projects/*/<id>.jsonl` and `<id>/`,
  `file-history/<id>`, `todos/<id>-*`, `session-env/<id>`; Codex `sessions/**/rollout-*-<id>.jsonl` and
  `archived_sessions`; Cursor `config/chats/<hash>/<id>/`);
- the lines naming it in the top-level prompt indexes (`history.jsonl`, `session_index.jsonl`);
- its rows in the top-level SQLite stores (Codex `state_*.sqlite`, `logs_*`, `goals_*`, `memories_*`, `queue_*`), by
  the thread-keyed tables of Codex 0.154.0 (`ROW_DELETES`; recheck them when the pin moves).
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys

STATE_DIR = "/state"  # where erase_history mounts the state volume; never taken from the command line
INDEXES = ("history.jsonl", "session_index.jsonl")
ROW_DELETES = {
    "threads": "DELETE FROM threads WHERE id = ?",
    "thread_dynamic_tools": "DELETE FROM thread_dynamic_tools WHERE thread_id = ?",
    "thread_artifacts": "DELETE FROM thread_artifacts WHERE thread_id = ?",
    "thread_spawn_edges": "DELETE FROM thread_spawn_edges WHERE parent_thread_id = ?1 OR child_thread_id = ?1",
    "thread_goals": "DELETE FROM thread_goals WHERE thread_id = ?",
    "thread_goal_continuation_deferrals": "DELETE FROM thread_goal_continuation_deferrals WHERE thread_id = ?",
    "logs": "DELETE FROM logs WHERE thread_id = ?",
    "stage1_outputs": "DELETE FROM stage1_outputs WHERE thread_id = ?",
    "queued_items": "DELETE FROM queued_items WHERE thread_id = ?",
    "queued_thread_revisions": "DELETE FROM queued_thread_revisions WHERE thread_id = ?",
}
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
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in sorted(tables & ROW_DELETES.keys()):
            removed += conn.execute(ROW_DELETES[table], (conv_id,)).rowcount
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
    print(erase(STATE_DIR, sys.argv[1]))
