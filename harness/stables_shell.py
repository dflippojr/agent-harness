"""Where the daemon finds the Stables shell (issue #334).

Stables, the browser client, is a separate static site in its own repository, dflippojr/stables. It talks to the
daemon only through the documented API, and nothing in the daemon depends on it: the daemon serves a copy at `/` and
`/static` when it finds one, and runs the API alone when it does not. The tower's deploy points `HARNESS_STABLES_DIR`
at the pinned release (tools/stables/pin.json); a developer can clone the repository into the checkout as `stables/`,
which git ignores. This is the only place the server names the client's files.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT

ENV = "HARNESS_STABLES_DIR"
DEFAULT = ROOT / "stables" / "public"


def locate() -> Path | None:
    """The shell directory to serve: `HARNESS_STABLES_DIR` when set, else a clone in the checkout; None without one."""
    override = os.environ.get(ENV, "").strip()
    path = Path(override).expanduser() if override else DEFAULT
    return path.resolve() if (path / "index.html").is_file() else None


def top_files(shell: Path | None) -> frozenset[str]:
    """The `/name` paths of the shell's top-level files, which load before sign-in."""
    if shell is None:
        return frozenset()
    return frozenset("/" + p.name for p in shell.iterdir() if p.is_file())
