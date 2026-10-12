"""The repository's own guidance file for native sessions (#642): AGENTS.md, else CLAUDE.md, at the workspace root.

Hosted Claude sessions get CLAUDE.md through `cli_backends.project_memory`; both read files here. The text goes into
the system prompt at the session's first run and is refreshed only at a round reset, like the repo map, so the prompt
prefix stays cache-stable within a round. The file is trusted like the rest of the workspace: it is not a taint
source, the same as the repo map.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import repomap
from .storage import is_reparse_point

GUIDANCE_FILES = ("AGENTS.md", "CLAUDE.md")  # the first one present wins
GUIDANCE_LIMIT = 16_000  # characters, the cap project_memory uses for hosted Claude
SECTION_OPEN = "<workspace-guidance>"
SECTION_CLOSE = "</workspace-guidance>"
_SECTION_RE = re.compile(r"\n\n" + re.escape(SECTION_OPEN) + r".*?" + re.escape(SECTION_CLOSE), re.DOTALL)


def read_workspace_file(workspace: Path, name: str) -> str:
    """The stripped text of `name` (a posix path under `workspace`), or "" when it is missing, unreadable or reached
    through a link: it is read on the host, where a link could point anywhere."""
    parts = name.split("/")
    path = workspace.joinpath(*parts)
    try:
        if any(is_reparse_point(workspace.joinpath(*parts[:i + 1])) for i in range(len(parts))) \
                or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def cap(text: str, limit: int, note: str) -> str:
    return text[:limit] + f"\n\n[truncated: {note}]" if len(text) > limit else text


def discover(workspace: Path) -> tuple[str, str] | None:
    """(file name, capped text) of the first non-empty guidance file at the workspace root, or None."""
    for name in GUIDANCE_FILES:
        text = read_workspace_file(workspace, name)
        if text:
            return name, cap(text, GUIDANCE_LIMIT, f"read {name} in the workspace for the rest")
    return None


def strip_section(prompt: str) -> str:
    return _SECTION_RE.sub("", prompt)


def apply_to_prompt(prompt: str, workspace: Path) -> str:
    """`prompt` with exactly one guidance section for `workspace`, appended at the end, or none when neither file is
    there. Calling it again replaces the section."""
    base = strip_section(prompt)
    found = discover(Path(workspace))
    if found is None:
        return base
    name, text = found
    for marker in (SECTION_OPEN, SECTION_CLOSE, repomap.SECTION_OPEN, repomap.SECTION_CLOSE):
        text = text.replace(marker, marker[:-1] + "​>")  # the file can't open or close a prompt section
    return f"{base}\n\n{SECTION_OPEN}\nContents of {name} at the workspace root (the repository's guidance):\n\n{text}\n{SECTION_CLOSE}"
