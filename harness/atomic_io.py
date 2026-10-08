"""One atomic small-file write: a new temp file beside the target, then a replace."""
from __future__ import annotations

import os
from pathlib import Path


def write_atomic(path: Path, text: str, *, private: bool = False) -> None:
    """Replace `path` with `text` (UTF-8) via a new, uniquely named temp file in the same directory.

    The temp file is created with O_EXCL, so nothing already at its name is written through; a link at the target
    is replaced, not followed. `private` creates it owner-only (0o600) before any text goes in. A failed write
    removes the temp file and leaves the old file whole. The parent directory must exist.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{os.urandom(4).hex()}.tmp")
    if private:
        tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
