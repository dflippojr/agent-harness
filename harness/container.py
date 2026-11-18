"""The Server image's entry point (#334 stage e, the repository's Dockerfile).

    python -m harness.container            first start: write a config; then run the daemon
    python -m harness.container --health   exit 0 when the daemon's /health answers (the image's HEALTHCHECK)

The image keeps its configuration and data in one volume, named by HARNESS_CONFIG_DIR and HARNESS_DATA_DIR (the
Dockerfile sets both). On the first start the configuration directory is empty, so this writes a service-profile
config there with harness.setup_config, which never overwrites one; later starts keep whatever the owner changed.
HARNESS_PORT sets that first config's port (default 8100) and HARNESS_ENABLE_MODULES, a comma-separated list, its
modules. Like every generated config it listens on 127.0.0.1 only: the daemon is reachable from outside the container
only when the owner sets listen.host in harness.local.yaml (docs/INSTALL.md, "Run the Server in Docker").
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = "8100"
PAUSE_FLAG = "llama-server.paused"  # setup_config requires one; the service profile has no local model to pause


def _volume(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} must name the image's volume directory (the Dockerfile sets it)")
    return Path(value)


def first_start_args(config_dir: Path, data_dir: Path, environ=os.environ) -> list[str]:
    """harness.setup_config's arguments for the first config: the service profile in the volume."""
    port = environ.get("HARNESS_PORT", "").strip() or DEFAULT_PORT
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise SystemExit(f"HARNESS_PORT must be a port number, not {port!r}")
    args = ["--profile", "service", "--config-dir", str(config_dir), "--data-dir", str(data_dir), "--port", port,
            "--pause-flag", str(data_dir / PAUSE_FLAG)]
    for module in environ.get("HARNESS_ENABLE_MODULES", "").split(","):
        if module.strip():
            args += ["--enable-module", module.strip()]
    return args


def prepare(environ=os.environ) -> bool:
    """Write the first config when the volume has none. True when this start wrote it."""
    config_dir, data_dir = _volume("HARNESS_CONFIG_DIR"), _volume("HARNESS_DATA_DIR")
    if (config_dir / "harness.yaml").exists():
        return False
    from . import setup_config
    code = setup_config.main(first_start_args(config_dir, data_dir, environ))
    if code:
        raise SystemExit(code)
    return True


def healthy(timeout: float = 5.0) -> bool:
    """Whether the daemon this container runs answers /health on its configured loopback port."""
    from .config import resolve_port
    try:
        port = resolve_port()
    except (OSError, ValueError):
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, never through a proxy
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--health"]:
        return 0 if healthy() else 1
    if argv:
        raise SystemExit("usage: python -m harness.container [--health]")
    prepare()
    from .__main__ import main as serve
    sys.argv = [sys.argv[0]]  # the daemon reads its configuration from HARNESS_CONFIG_DIR and HARNESS_DATA_DIR
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
