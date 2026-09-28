"""Structured per-round agent state: schema, validation, and workspace file lists."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .tools import ToolError

STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["goal"],
    "properties": {
        "goal": {"type": "string"},
        "plan": {"type": "string"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["command", "args", "message"],
                "properties": {
                    "command": {"type": "string"},
                    "args": {},
                    "message": {"type": "string"},
                },
            },
        },
        "next_step": {"type": "string"},
        "notes": {"type": "string"},
    },
}

_VALIDATOR = Draft202012Validator(STATE_SCHEMA)
EMPTY_STATE = {"goal": "", "plan": "", "errors": [], "next_step": "", "notes": ""}
FILES_MODIFIED_MAX = 200
_STATE_KEYS = ("goal", "plan", "errors", "next_step", "notes")


def _schema_error(err: ValidationError) -> str:
    path = "/".join(str(part) for part in err.absolute_path)
    where = f" ({path})" if path else ""
    return f"invalid state{where}: {err.message}"


def dump_state(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def normalize_state(payload: dict) -> dict:
    """Full replacement: omitted optional fields become empty and do not persist."""
    return {
        "goal": payload["goal"],
        "plan": payload["plan"] if "plan" in payload else "",
        "errors": list(payload["errors"]) if "errors" in payload else [],
        "next_step": payload["next_step"] if "next_step" in payload else "",
        "notes": payload["notes"] if "notes" in payload else "",
    }


def validate_state(payload: dict, max_chars: int) -> dict:
    """Raise ToolError without returning a value when the object is not schema-valid or is too large."""
    if not isinstance(payload, dict):
        raise ToolError("state must be a JSON object")
    errors = sorted(_VALIDATOR.iter_errors(payload), key=lambda err: list(err.path))
    if errors:
        raise ToolError(_schema_error(errors[0]))
    normalized = normalize_state(payload)
    encoded = dump_state(normalized)
    if len(encoded) > max_chars:
        raise ToolError(f"state is {len(encoded)} characters; max is {max_chars}")
    return normalized


def is_valid_state(saved) -> bool:
    if not isinstance(saved, dict):
        return False
    try:
        validate_state(saved, max_chars=10**9)
        return True
    except ToolError:
        return False


def is_dead_end_retry(saved: dict | None, command: str, args) -> bool:
    """Same tool name and identical arguments as a recorded error (#159)."""
    if not isinstance(saved, dict):
        return False
    for item in saved.get("errors") or []:
        if not isinstance(item, dict):
            continue
        if item.get("command") == command and item.get("args") == args:
            return True
    return False


def porcelain_paths(line: str) -> list[str]:
    if len(line) < 4:
        return []
    path = line[3:]
    if " -> " in path:
        old, new = path.split(" -> ", 1)
        return [_posix(old), _posix(new)]
    return [_posix(path)]


def name_status_paths(line: str) -> list[str]:
    parts = line.strip().split("\t")
    if len(parts) < 2:
        return []
    return [_posix(part) for part in parts[1:] if part]


def _posix(path: str) -> str:
    return path.replace("\\", "/").strip().strip('"')


def paths_since(baseline: list[str] | None, current: list[str]) -> list[str]:
    changed = [line for line in current if line not in set(baseline or [])]
    out: list[str] = []
    seen: set[str] = set()
    for line in changed:
        for path in porcelain_paths(line):
            if path and path not in seen:
                seen.add(path)
                out.append(path)
            if len(out) >= FILES_MODIFIED_MAX:
                return out
    return out


def git_porcelain(root: Path) -> list[str] | None:
    """`git status --porcelain` lines at the workspace root, or None if this is not a usable git repo."""
    return _git_lines(root, "status", "--porcelain=v1", "--untracked-files=all")


def _git_lines(root: Path, *args: str) -> list[str] | None:
    try:
        if not (root / ".git").exists():
            return None
        from .projects import git
        result = git(root, *args, check=False, timeout=30)
    except (OSError, TypeError, ValueError):
        return None
    if result.code != 0:
        return None
    return [line.replace("\\", "/") for line in result.out.strip("\n").splitlines() if line.strip()]


def _rev_parse_head(root: Path) -> str | None:
    lines = _git_lines(root, "rev-parse", "HEAD")
    if not lines:
        return None
    sha = lines[0].strip()
    return sha or None


def _file_digest(root: Path, rel: str) -> str | None:
    path = root / rel
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def snapshot_git_baseline(root: Path) -> dict | None:
    """HEAD at run start plus content hashes of paths already dirty in the worktree."""
    porcelain = git_porcelain(root)
    if porcelain is None:
        return None
    dirty: dict[str, str | None] = {}
    for line in porcelain:
        for path in porcelain_paths(line):
            if path and path not in dirty:
                dirty[path] = _file_digest(root, path)
    return {"head": _rev_parse_head(root), "dirty": dirty}


def _committed_name_status(root: Path, head: str | None) -> list[str] | None:
    if head:
        return _git_lines(root, "diff", "--name-status", "-M", f"{head}..HEAD")
    if _rev_parse_head(root) is None:
        return []
    try:
        from .projects import git
        empty = git(root, "hash-object", "-t", "tree", "--stdin", check=False, timeout=30, input_="")
    except (OSError, TypeError, ValueError):
        return None
    if empty.code != 0 or not empty.out.strip():
        return None
    return _git_lines(root, "diff", "--name-status", "-M", f"{empty.out.strip()}..HEAD")


def files_modified_since(root: Path, baseline: dict | None) -> list[str] | None:
    """Committed, staged, unstaged, and untracked paths since the run-start baseline, or None if git fails."""
    porcelain = git_porcelain(root)
    if porcelain is None:
        return None
    committed = _committed_name_status(root, (baseline or {}).get("head"))
    if committed is None:
        return None
    dirty = dict((baseline or {}).get("dirty") or {})
    out: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> bool:
        path = _posix(path)
        if not path or path in seen:
            return len(out) >= FILES_MODIFIED_MAX
        if path in dirty and _file_digest(root, path) == dirty[path]:
            return len(out) >= FILES_MODIFIED_MAX
        seen.add(path)
        out.append(path)
        return len(out) >= FILES_MODIFIED_MAX

    for line in committed:
        for path in name_status_paths(line):
            if add(path):
                return out
    for line in porcelain:
        for path in porcelain_paths(line):
            if add(path):
                return out
    return out


def inject_payload(saved: dict | None, files_modified: list[str], max_chars: int) -> dict:
    """State plus derived files_modified, shrunk to fit the serialized-size cap."""
    base = {**EMPTY_STATE, **{k: v for k, v in (saved or {}).items() if k in _STATE_KEYS}}
    payload = {**base, "files_modified": list(files_modified)[:FILES_MODIFIED_MAX]}
    while len(dump_state(payload)) > max_chars and payload["files_modified"]:
        payload["files_modified"].pop()
    if len(dump_state(payload)) > max_chars:
        for key in ("notes", "plan", "next_step"):
            payload[key] = ""
            if len(dump_state(payload)) <= max_chars:
                break
    encoded = dump_state(payload)
    if len(encoded) > max_chars and isinstance(payload["goal"], str):
        overflow = len(encoded) - max_chars
        payload["goal"] = payload["goal"][: max(0, len(payload["goal"]) - overflow)]
    return payload
