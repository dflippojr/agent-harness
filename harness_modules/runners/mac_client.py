"""Build the version-matched Agent Harness for Mac bundle served by the Server."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from functools import lru_cache
from pathlib import Path

import harness.modules as interface
from harness.modules import repo_file

# The running core's own package: the bundle's harness/ files are the Server's, from a checkout or from the wheel.
CORE = Path(interface.__file__).resolve().parent
FILES = (
    ("macrunner/harness_runner.py", "app/harness_runner.py"),
    ("macrunner/sandbox.sb", "app/sandbox.sb"),
    ("macrunner/dev.agent-harness.runner.plist", "dev.agent-harness.runner.plist"),
    ("harness/fileops.py", "app/harness/fileops.py"),
    ("harness/projects.py", "app/harness/projects.py"),
    ("harness/changes.py", "app/harness/changes.py"),
    ("harness/review_comments.py", "app/harness/review_comments.py"),
    ("harness/atomic_io.py", "app/harness/atomic_io.py"),
    ("harness/compat.py", "app/harness/compat.py"),
    ("harness/updater.py", "app/harness/updater.py"),
    ("harness/cli.py", "client/harness_cli.py"),
    ("harness/atomic_io.py", "client/harness_atomic_io.py"),
    ("harness/compat.py", "client/harness_compat.py"),
    ("harness/updater.py", "client/harness_update.py"),
    ("sdk/harness_client.py", "client/harness_client.py"),
)


def source(relative: str) -> Path:
    """Where a bundle file comes from. harness/ files are the running core's. The rest (macrunner/, the SDK) are
    repository files: the checkout's, or the copies the runners wheel carries when the Server is installed (#334)."""
    if relative.startswith("harness/"):
        return CORE / relative.removeprefix("harness/")
    return repo_file(__package__, relative)


@lru_cache(maxsize=1)
def package_bytes() -> bytes:
    """Return a deterministic gzip tarball containing only the native client runtime."""
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for relative, destination in sorted(FILES):
                data = source(relative).read_bytes()
                info = tarfile.TarInfo(destination)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
            # The client CLI can't import add-on modules: pack their command rows next to it (cli.module_commands).
            data = _module_commands()
            info = tarfile.TarInfo("client/harness_module_commands.json")
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return compressed.getvalue()


def _module_commands() -> bytes:
    from harness.modules import cli_groups, cli_rows
    return json.dumps({"rows": cli_rows(), "groups": cli_groups()}, sort_keys=True).encode("utf-8")


def package_manifest() -> dict:
    from harness.modules import BUILD_ID, CLIENT_PROTOCOLS, MAC_CLIENT_VERSION
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
