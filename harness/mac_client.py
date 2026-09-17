"""Build the version-matched Mac client/runner bundle served by the daemon."""

from __future__ import annotations

import gzip
import io
import tarfile
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "macrunner/harness_runner.py": "app/harness_runner.py",
    "macrunner/sandbox.sb": "app/sandbox.sb",
    "macrunner/dev.agent-harness.runner.plist": "dev.agent-harness.runner.plist",
    "harness/fileops.py": "app/harness/fileops.py",
    "harness/projects.py": "app/harness/projects.py",
    "harness/changes.py": "app/harness/changes.py",
    "harness/cli.py": "client/harness_cli.py",
    "sdk/harness_client.py": "client/harness_client.py",
}


@lru_cache(maxsize=1)
def package_bytes() -> bytes:
    """Return a deterministic gzip tarball containing only the native client runtime."""
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for source, destination in sorted(FILES.items()):
                data = (ROOT / source).read_bytes()
                info = tarfile.TarInfo(destination)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return compressed.getvalue()
