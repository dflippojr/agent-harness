"""Structured per-round agent state: schema, validation, and workspace file lists."""

from __future__ import annotations

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
    try:
        if not (root / ".git").exists():
            return None
        from .projects import git
        result = git(root, "status", "--porcelain=v1", "--untracked-files=all", check=False, timeout=30)
    except (OSError, TypeError, ValueError):
        return None
    if result.code != 0:
        return None
    return [line.replace("\\", "/") for line in result.out.splitlines() if line.strip()]


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
