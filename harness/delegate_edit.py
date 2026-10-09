"""Delegated edits (#157): a fresh, single-shot model call proposes search/replace edits; the worker applies them.

The worker calls `delegate_edit(paths, instruction)`. The daemon reads the listed files itself, asks the session's
model for a JSON list of `{path, old_text, new_text}` edits in a fresh bounded context, validates them all against
the text it read, and stores the proposal on the run. The worker gets back only `{patch_id, summary, status, error}`:
no file contents and no snippet text. `apply_delegated_edit(patch_id)` then rechecks each file's hash and writes the
whole proposal, or nothing, through the same preview and approval path as `edit_file`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import posixpath
import uuid
from pathlib import Path

from .fileops import FileOps, ToolError, normalize_path, truncate_middle

PROPOSE = "delegate_edit"
APPLY = "apply_delegated_edit"
TOOL_NAMES = (PROPOSE, APPLY)
MAX_ATTEMPTS = 3            # 1 initial delegation plus at most 2 re-delegations per edit
MAX_PATHS = 20
DELEGATE_MAX_TOKENS = 4096  # cap on the delegate's completion
INPUT_SHARE = 0.5           # the delegate's input is at most this share of the model's context window

PROPOSE_SCHEMA = {"type": "function", "function": {
    "name": PROPOSE,
    "description": (
        "Delegate a code edit to a fresh, single-shot model call that reads the listed files and proposes "
        "search/replace edits. You get back a patch_id and a short summary, never the file contents or the edit "
        "text, so your context stays small. Review the summary, then call apply_delegated_edit(patch_id) to write "
        "it (or delegate again with a clearer instruction). Only existing files can be edited: create new files "
        "with write_file. At most 3 delegations per set of files until one is applied. Not available on runner "
        "targets or in chat."),
    "parameters": {"type": "object", "properties": {
        "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                  "description": "Workspace-relative paths of the existing files to edit."},
        "instruction": {"type": "string",
                        "description": "What to change, precisely enough for someone who sees only these files."},
        "task_id": {"type": "string",
                    "description": "Optional: names this edit for the retry limit; defaults to the file list."},
    }, "required": ["paths", "instruction"]},
}}

APPLY_SCHEMA = {"type": "function", "function": {
    "name": APPLY,
    "description": (
        "Apply a proposal from delegate_edit by its patch_id. All of its edits are written or none are; if a file "
        "changed since the proposal was made, nothing is written and you should delegate again."),
    "parameters": {"type": "object", "properties": {
        "patch_id": {"type": "string", "description": "The patch_id delegate_edit returned."},
    }, "required": ["patch_id"]},
}}

SYSTEM_PROMPT = (
    "You edit source files. You are given an instruction and the full text of some files. Reply with only a JSON "
    "object, no prose and no code fences:\n"
    '{"summary": "<one or two sentences on what you changed>", '
    '"edits": [{"path": "<one of the given paths>", "old_text": "<exact text copied from the file>", '
    '"new_text": "<its replacement>"}]}\n'
    "Each old_text must appear exactly once in its file, copied character for character including indentation, "
    "and edits in the same file must not overlap. Keep each old_text as short as it can be while still unique. "
    "If the instruction cannot be done with these files, reply with "
    '{"summary": "<why>", "edits": []}.')


def text_digest(text: str) -> str:
    """sha256 of a file's decoded UTF-8 text, the convention #155 receipts use."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def edit_key(paths: list[str], task_id: str | None = None) -> str:
    """What the retry limit counts against: an explicit task id, else the sorted file list."""
    if task_id and task_id.strip():
        return "task:" + task_id.strip()
    return "paths:" + "\n".join(sorted(set(paths)))


def _read_strict(files: FileOps, path: str) -> tuple[str, Path, str]:
    """(workspace-relative path, resolved path, text) of an existing UTF-8 text file in the workspace."""
    p = files.resolve(path)  # follows symlinks, then refuses anything outside the workspace
    if not p.is_file():
        raise ToolError(f"no such file: {path} (delegate_edit only edits existing files; use write_file to create)")
    try:
        text = files.read_checked(p, path)  # universal newlines, as edit_file reads
    except UnicodeDecodeError:
        raise ToolError(f"binary file: {path} is not UTF-8 text") from None
    if "\x00" in text:
        raise ToolError(f"binary file: {path} contains a NUL byte")
    return files.rel(p), p, text


def read_inputs(files: FileOps, paths: list[str], total_chars: int) -> dict[str, str]:
    """The listed files' text keyed by workspace-relative path. Each file is at most `files.read_chars` and all of
    them at most `total_chars`; anything over is an error, never a truncation."""
    if not paths:
        raise ToolError("paths must list at least one file")
    if len(paths) > MAX_PATHS:
        raise ToolError(f"at most {MAX_PATHS} files per delegation")
    texts: dict[str, str] = {}
    for path in paths:
        rel, _, text = _read_strict(files, path)
        if len(text) > files.read_chars:
            raise ToolError(f"{rel} is {len(text)} characters; a delegated file may be at most {files.read_chars}")
        texts[rel] = text
    total = sum(len(t) for t in texts.values())
    if total > total_chars:
        raise ToolError(f"the listed files total {total} characters; a delegation may read at most {total_chars}. "
                        "Delegate fewer files at a time.")
    return texts


def request(instruction: str, texts: dict[str, str]) -> list[dict]:
    """The delegate's whole context: the instruction and the files, nothing from the worker's conversation."""
    parts = [f"Instruction:\n{instruction}\n"]
    for rel, text in texts.items():
        parts.append(f'<file path="{rel}">\n{text}\n</file>')
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n\n".join(parts)}]


def _first_json_object(content: str):
    """The first JSON value that decodes from `content`, skipping prose or code fences around it."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(content):
        if ch not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(content, i)
        except ValueError:
            continue
        return value
    raise ToolError("the delegate's reply was not JSON")


def _require_utf8(n: int, *texts: str) -> None:
    """Reject text that cannot be written as UTF-8 (e.g. a lone surrogate from a JSON \ud800 escape)."""
    for t in texts:
        try:
            t.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolError(f"edit {n} contains text that cannot be encoded as UTF-8") from None


def parse_reply(content: str, finish_reason: str, texts: dict[str, str]) -> tuple[str, list[dict]]:
    """(summary, edits) from the delegate's reply. Unparsable, cut-off, empty or partial output is an error."""
    if finish_reason == "length":
        raise ToolError("the delegate's reply was cut off at its token limit; delegate fewer or smaller changes")
    if not content.strip():
        raise ToolError("the delegate returned nothing")
    value = _first_json_object(content)
    summary = ""
    if isinstance(value, dict):
        summary = str(value.get("summary") or "").strip()
        value = value.get("edits")
    if not isinstance(value, list):
        raise ToolError("the delegate's reply had no list of edits")
    if not value:
        raise ToolError("the delegate proposed no edits" + (f": {summary}" if summary else ""))
    edits = []
    for n, e in enumerate(value, 1):
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("path", "old_text", "new_text")):
            raise ToolError(f"edit {n} is not a {{path, old_text, new_text}} object of strings")
        path = posixpath.normpath(normalize_path(e["path"]))
        if path not in texts:
            raise ToolError(f"edit {n} targets {e['path']}, which was not one of the listed files")
        if not e["old_text"]:
            raise ToolError(f"edit {n} has an empty old_text")
        _require_utf8(n, e["old_text"], e["new_text"])
        edits.append({"path": path, "old_text": e["old_text"], "new_text": e["new_text"]})
    return summary, edits


def apply_to_texts(texts: dict[str, str], edits: list[dict]) -> dict[str, str]:
    """New text for every edited file, validating all edits first: each old_text occurs exactly once in the
    original file and no two edits in a file overlap. Raises on the first problem, having changed nothing."""
    spans: dict[str, list[tuple[int, int, str]]] = {}
    for n, e in enumerate(edits, 1):
        original = texts.get(e["path"])
        if original is None:
            raise ToolError(f"edit {n} targets {e['path']}, which is not part of this proposal")
        count = original.count(e["old_text"])
        if count != 1:
            raise ToolError(f"edit {n} ({e['path']}): old_text must appear exactly once, found {count}")
        start = original.index(e["old_text"])
        spans.setdefault(e["path"], []).append((start, start + len(e["old_text"]), e["new_text"]))
    result = {}
    for path, items in spans.items():
        items.sort()
        for (_, end, _), (start, _, _) in zip(items, items[1:]):
            if start < end:
                raise ToolError(f"two edits to {path} overlap")
        original, out, pos = texts[path], [], 0
        for start, end, new in items:
            out.append(original[pos:start])
            out.append(new)
            pos = end
        out.append(original[pos:])
        result[path] = "".join(out)
        _require_utf8(0, result[path])
    return result


def summarize(summary: str, edits: list[dict], new_texts: dict[str, str], texts: dict[str, str]) -> str:
    """What the worker sees about a proposal: the delegate's summary plus per-file line counts, no snippets."""
    lines = [summary[:500]] if summary else []
    for path, new in new_texts.items():
        n = sum(1 for e in edits if e["path"] == path)
        diff = list(difflib.unified_diff(texts[path].splitlines(), new.splitlines(), lineterm="", n=0))
        added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
        lines.append(f"{path}: {n} edit{'s' if n != 1 else ''}, +{added} -{removed} lines")
    return "\n".join(lines)


def new_proposal(texts: dict[str, str], edits: list[dict], key: str, summary: str) -> tuple[str, dict]:
    patch_id = "p-" + uuid.uuid4().hex[:12]
    used = {e["path"] for e in edits}
    return patch_id, {"key": key, "summary": summary, "edits": edits,
                      "hashes": {p: text_digest(t) for p, t in texts.items() if p in used}}


def _current(files: FileOps, proposal: dict) -> dict[str, tuple[Path, str]]:
    """Each proposal file's resolved path and current text; a changed file is a stale-proposal error."""
    current = {}
    for path, digest in proposal["hashes"].items():
        try:
            _, p, text = _read_strict(files, path)
        except ToolError as e:
            raise ToolError(f"stale proposal: {e}. Nothing was written; delegate again.") from None
        if text_digest(text) != digest:
            raise ToolError(f"stale proposal: {path} changed since the edit was proposed. Nothing was written; "
                            "delegate again.")
        current[path] = (p, text)
    return current


def preview(files: FileOps, proposal: dict) -> str:
    """Unified diff of the whole proposal against the files as they are now, for approval requests."""
    try:
        current = _current(files, proposal)
        new_texts = apply_to_texts({p: t for p, (_, t) in current.items()}, proposal["edits"])
    except ToolError:
        return ""
    out = []
    for path, new in new_texts.items():
        out.extend(difflib.unified_diff(current[path][1].splitlines(), new.splitlines(), f"a/{path}", f"b/{path}",
                                        lineterm=""))
    return truncate_middle("\n".join(out), 12000)


def apply(files: FileOps, proposal: dict) -> str:
    """Write the whole proposal or nothing: hashes and every edit are checked before the first write."""
    current = _current(files, proposal)
    new_texts = apply_to_texts({p: t for p, (_, t) in current.items()}, proposal["edits"])
    done: list[str] = []
    try:
        for path, new in new_texts.items():
            files.write_replacing(current[path][0], path, new.encode("utf-8"))  # as edit_file writes
            done.append(path)  # a failed write replaces nothing, so only finished files need restoring
    except Exception as e:  # any write-time failure, not just OSError, must restore and surface as ToolError
        failed = []
        for path in done:
            try:
                files.write_replacing(current[path][0], path, current[path][1].encode("utf-8"))
            except Exception:
                failed.append(path)
        note = f" Could not restore: {', '.join(failed)}." if failed else " Earlier writes were rolled back."
        raise ToolError(f"write failed: {e}.{note}") from None
    return "applied " + ", ".join(new_texts)
