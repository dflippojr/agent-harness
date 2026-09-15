"""File tools shared by the tower daemon and the MacBook runner.

Stdlib only and Python 3.9 compatible: the runner copies this file to the Mac, whose stock Python is 3.9.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}


class ToolError(Exception):
    pass


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters truncated] ...\n{text[-half:]}"


def resolve_path(p: Path) -> Path:
    r"""Path.resolve() that never returns Windows' extended-length form. Python 3.10 sometimes yields
    `\\?\C:\...` while a directory in the path is being created, which breaks containment checks."""
    resolved = p.resolve()
    text = str(resolved)
    return Path(text[4:]) if text.startswith("\\\\?\\") else resolved


def normalize_path(path: str | None, prefixes: tuple[str, ...] = ("/workspace",)) -> str:
    """Workspace-relative form of a path. Absolute paths under one of `prefixes` (the workspace root as the
    agent sees it) are made relative."""
    path = (path or ".").strip().replace("\\", "/")
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            path = path[len(prefix):]
            break
    return path.lstrip("/") or "."


def dir_size(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


class FileOps:
    def __init__(self, root: Path, context_tokens: int, prefixes: tuple[str, ...] = ("/workspace",)):
        self.root = resolve_path(root)
        self.prefixes = prefixes
        # Size reads to the context window: short pages made Qwen answer from page 1 in Phase 0.
        self.read_lines = 2000
        self.read_chars = max(8000, int(context_tokens * 0.25 * 3.5))

    def resolve(self, path: str | None) -> Path:
        rel = normalize_path(path, self.prefixes)
        candidate = resolve_path(self.root / rel)
        if not self.contains(candidate):
            raise ToolError(f"path escapes the workspace: {path}")
        return candidate

    def contains(self, p: Path) -> bool:
        return p == self.root or p.is_relative_to(self.root)

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def _inside(self, p: Path) -> bool:
        """For entries found while walking: a symlink pointing out of the workspace is skipped."""
        return not p.is_symlink() or self.contains(resolve_path(p))

    def list_files(self, path: str = ".", max_depth: int = 2) -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries: list[str] = []

        def walk(d: Path, depth: int) -> None:
            for child in sorted(d.iterdir()):
                if child.name in SKIP_DIRS or len(entries) >= 500 or not self._inside(child):
                    continue
                entries.append(self.rel(child) + ("/" if child.is_dir() else ""))
                if child.is_dir() and not child.is_symlink() and depth < max_depth:
                    walk(child, depth + 1)

        walk(base, 1)
        suffix = "\n... (listing truncated at 500 entries)" if len(entries) >= 500 else ""
        return "\n".join(entries) + suffix if entries else "(empty directory)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line or len(lines), start + self.read_lines - 1)
        body, shown_end = [], start - 1
        size = 0
        for n in range(start, end + 1):
            line = f"{n}\t{lines[n - 1]}"
            if size + len(line) > self.read_chars and body:
                break
            body.append(line)
            size += len(line) + 1
            shown_end = n
        text = "\n".join(body)
        if shown_end < len(lines):
            text += f"\n... ({len(lines)} lines total; continue with start_line={shown_end + 1})"
        return text or "(empty file)"

    def _walk_files(self, base: Path) -> list[Path]:
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(base):  # doesn't follow directory symlinks
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if self._inside(p):
                    found.append(p)
        return found

    def search(self, pattern: str, path: str = ".") -> str:
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        base = self.resolve(path)
        files = [base] if base.is_file() else self._walk_files(base)
        matches: list[str] = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{self.rel(f)}:{n}: {line[:300]}")
                    if len(matches) >= 200:
                        return "\n".join(matches) + "\n... (stopped at 200 matches)"
        return "\n".join(matches) or "no matches"

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return f"wrote {len(content)} characters to {self.rel(p)}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ToolError(f"old_text must appear exactly once, found {count} occurrences")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old_text, new_text))
        return f"edited {self.rel(p)}"

    def preview_diff(self, name: str, args: dict) -> str:
        """Unified diff of what a write/edit would change, for approval requests."""
        try:
            p = self.resolve(args.get("path"))
            old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        except ToolError:
            return ""
        if name == "write_file":
            new = args.get("content", "")
        elif name == "edit_file" and old.count(args.get("old_text", "")) == 1:
            new = old.replace(args["old_text"], args.get("new_text", ""))
        else:
            return ""
        rel = normalize_path(args.get("path"), self.prefixes)
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="")
        return truncate_middle("\n".join(diff), 12000)


FILE_TOOLS = ("list_files", "read_file", "search", "write_file", "edit_file")
