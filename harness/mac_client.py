"""Build the version-matched Agent Harness for Mac bundle served by the Server."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    ("macrunner/harness_runner.py", "app/harness_runner.py"),
    ("macrunner/sandbox.sb", "app/sandbox.sb"),
    ("macrunner/dev.agent-harness.runner.plist", "dev.agent-harness.runner.plist"),
    ("harness/fileops.py", "app/harness/fileops.py"),
    ("harness/projects.py", "app/harness/projects.py"),
    ("harness/changes.py", "app/harness/changes.py"),
    ("harness/compat.py", "app/harness/compat.py"),
    ("harness/updater.py", "app/harness/updater.py"),
    ("harness/cli.py", "client/harness_cli.py"),
    ("harness/compat.py", "client/harness_compat.py"),
    ("harness/updater.py", "client/harness_update.py"),
    ("sdk/harness_client.py", "client/harness_client.py"),
)


@lru_cache(maxsize=1)
def package_bytes() -> bytes:
    """Return a deterministic gzip tarball containing only the native client runtime."""
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for source, destination in sorted(FILES):
                data = (ROOT / source).read_bytes()
                info = tarfile.TarInfo(destination)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return compressed.getvalue()


def package_manifest() -> dict:
    from .compat import BUILD_ID, CLIENT_PROTOCOLS, MAC_CLIENT_VERSION
    package = package_bytes()
    return {
        "schema": 1,
        "version": MAC_CLIENT_VERSION,
        "build_id": BUILD_ID,
        "admin_protocol": CLIENT_PROTOCOLS["cli"],
        "runner_protocol": CLIENT_PROTOCOLS["runner"],
        "bytes": len(package),
        "sha256": hashlib.sha256(package).hexdigest(),
        "package_url": "/mac-client/package.tar.gz",
    }
