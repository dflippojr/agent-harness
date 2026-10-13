"""Where the daemon finds the bundled Stables shell (issue #334 stage (d)).

Stables, the browser client, is a separate static site in `stables/public`. It talks to the daemon only through the
documented API, and nothing in the daemon depends on it: the daemon serves the shell at `/` and `/static` when it is
there, and runs the API alone when it is not. This is the only place the server names the client's files.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT

ENV = "HARNESS_STABLES_DIR"
DEFAULT = ROOT / "stables" / "public"


def locate() -> Path | None:
    """The shell directory to serve: `HARNESS_STABLES_DIR` when set, else the checkout's copy; None without one."""
    override = os.environ.get(ENV, "").strip()
    path = Path(override).expanduser() if override else DEFAULT
    return path.resolve() if (path / "index.html").is_file() else None


def top_files(shell: Path | None) -> frozenset[str]:
    """The `/name` paths of the shell's top-level files, which load before sign-in."""
    if shell is None:
        return frozenset()
    return frozenset("/" + p.name for p in shell.iterdir() if p.is_file())
