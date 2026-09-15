"""Read-only access to the user's memory library, so agents have personal context.

The daemon keeps its own clone (`memory_library.clone_dir`) and refreshes it with `git pull --ff-only` at most every
`refresh_minutes`; it never writes to the library. Only files under `categories/<name>/` for the allowlisted
categories exist as far as agents can tell: the top-level index, inbox, source archives, cross-category capsules, and
every other category (health, emotions, finance, relationships, ...) are neither listed, searched, nor readable.
Anything the tool returns is data, not instructions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from .config import MemoryLibraryConfig
from .fileops import ToolError
from .sandbox import run_cmd

log = logging.getLogger("harness.memory_library")

TOOLS = ("memory_index", "memory_search", "memory_read")
TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".csv"}
MAX_READ_CHARS = 60_000


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


def schemas(cfg: MemoryLibraryConfig) -> list[dict]:
    cats = ", ".join(cfg.categories)
    return [
        _fn("memory_index", "List the files in the user's personal memory library that you may read (categories: "
                            f"{cats}), with each file's title. Use it for context about the user's projects, work, "
                            "home, and tastes. Read-only.", {}),
        _fn("memory_search", "Search the readable memory library for a regular expression (case-insensitive). "
                             "Returns up to 100 'path:line: text' matches. Newer dated entries win over older ones.", {
            "pattern": {"type": "string"},
        }, ["pattern"]),
        _fn("memory_read", "Read a memory library file by the path memory_index or memory_search showed.", {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based first line. Default 1."},
            "end_line": {"type": "integer", "description": "1-based last line, inclusive."},
        }, ["path"]),
    ]


class MemoryLibrary:
    tool_names = TOOLS

    def __init__(self, cfg: MemoryLibraryConfig):
        self.cfg = cfg
        self.root = Path(cfg.clone_dir)
        self._refreshed = 0.0
        self._lock = asyncio.Lock()
        self.refresh_error = ""

    def schemas(self) -> list[dict]:
        return schemas(self.cfg)

    # sync
    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            if not force and time.monotonic() - self._refreshed < self.cfg.refresh_minutes * 60 and self.root.is_dir():
                return
            if not (self.root / ".git").is_dir():
                if not self.cfg.repo:
                    raise ToolError("the memory library isn't configured (memory_library.repo)")
                self.root.parent.mkdir(parents=True, exist_ok=True)
                code, out, err = await run_cmd(["git", "clone", "-q", "--", self.cfg.repo, str(self.root)], timeout=300)
            else:
                code, out, err = await run_cmd(["git", "-C", str(self.root), "pull", "-q", "--ff-only"], timeout=120)
            self._refreshed = time.monotonic()
            self.refresh_error = "" if code == 0 else (err or out).strip()[:300]
            if code != 0:
                log.warning("memory library refresh failed: %s", self.refresh_error)
                if not self.root.is_dir():
                    raise ToolError(f"the memory library couldn't be cloned: {self.refresh_error}")

    # access control
    def allowed_files(self) -> list[Path]:
        files = []
        for cat in self.cfg.categories:
            base = self.root / "categories" / cat
            if base.is_dir():
                files += [f for f in base.rglob("*") if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES
                          and self._allowed(f)]
        return sorted(files)

    def _allowed(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root.resolve()).parts
        except ValueError:
            return False
        return (len(rel) >= 3 and rel[0] == "categories" and rel[1] in self.cfg.categories
                and not any(p.startswith(".") for p in rel) and path.suffix.lower() in TEXT_SUFFIXES)

    def _resolve(self, path: str) -> Path:
        rel = path.strip().replace("\\", "/").lstrip("/")
        target = (self.root / rel).resolve()
        if not target.is_file() or not self._allowed(target):
            raise ToolError(f"{path} isn't a readable memory library file; use memory_index to see what is")
        return target

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    # tools
    def memory_index(self) -> str:
        lines = []
        for f in self.allowed_files():
            title = ""
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                    if line.startswith("#"):
                        title = line.lstrip("#").strip()
                        break
            except OSError:
                pass
            lines.append(f"- {self._rel(f)}" + (f" — {title}" if title else ""))
        note = f"\n(note: the library couldn't be refreshed, so it may be stale: {self.refresh_error})" \
            if self.refresh_error else ""
        return ("\n".join(lines) or "No readable files.") + note

    def memory_search(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ToolError(f"bad regular expression: {e}")
        hits = []
        for f in self.allowed_files():
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self._rel(f)}:{n}: {line[:300]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n... (first 100 matches; narrow the pattern)"
        return "\n".join(hits) or "no matches"

    def memory_read(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        f = self._resolve(path)
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else len(lines))
        out, size = [], 0
        for n in range(start, end + 1):
            text = f"{n}: {lines[n - 1]}"
            if size + len(text) > MAX_READ_CHARS:
                out.append(f"... [stopped at line {n - 1} of {len(lines)}; read on with start_line={n}]")
                break
            out.append(text)
            size += len(text) + 1
        if end < len(lines) and not out[-1].startswith("..."):
            out.append(f"... [{len(lines) - end} more lines]")
        return "\n".join(out)

    async def call(self, name: str, args: dict) -> str:
        await self.refresh()
        return await asyncio.to_thread(getattr(self, name), **args)
