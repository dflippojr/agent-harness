"""The Server image's entry point (#334 stage e, the repository's Dockerfile).

    python -m harness.container            first start: write a config; then run the daemon
    python -m harness.container --health   exit 0 when the daemon's /health answers (the image's HEALTHCHECK)

The image keeps its configuration and data in one volume, named by HARNESS_CONFIG_DIR and HARNESS_DATA_DIR (the
Dockerfile sets both). On the first start the configuration directory is empty, so this writes a service-profile
config there with harness.setup_config, which never overwrites one; later starts keep whatever the owner changed.
HARNESS_PORT sets that first config's port (default 8100) and HARNESS_ENABLE_MODULES, a comma-separated list, its
modules. Like every generated config it listens on 127.0.0.1 only: the daemon is reachable from outside the container
only when the owner sets listen.host in harness.local.yaml (docs/INSTALL.md, "Run the Server in Docker"). A first
start cut short is finished by the next one.
"""

from __future__ import annotations

import http.client
import os
import sys
from pathlib import Path

DEFAULT_PORT = "8100"
PAUSE_FLAG = "llama-server.paused"  # setup_config requires one; the service profile has no local model to pause
# What harness.setup_config writes, in this order: the volume is set up only once the last of them exists.
FIRST_START_FILES = ("harness.yaml", "projects.yaml", "profile.yaml")


def _volume(name: str, environ=os.environ) -> Path:
    value = environ.get(name, "").strip()
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
    """Write the first config when the volume has none, or finish one that an interrupted first start left half
    written (setup_config keeps an existing harness.yaml and projects.yaml, and writes profile.yaml afresh). True
    when this start wrote anything."""
    config_dir, data_dir = _volume("HARNESS_CONFIG_DIR", environ), _volume("HARNESS_DATA_DIR", environ)
    if all((config_dir / name).exists() for name in FIRST_START_FILES):
        return False
    from . import setup_config
    code = setup_config.main(first_start_args(config_dir, data_dir, environ))
    if code:
        raise SystemExit(code)
    return True


def probe_address(host: str) -> str:
    """Where a daemon listening on `host` is reached from inside its own container."""
    host = host.strip().strip("[]")
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def healthy(timeout: float = 5.0) -> bool:
    """Whether the daemon this container runs answers /health where its configuration says it listens."""
    from .config import resolve_listen
    try:
        host, port = resolve_listen()
    except Exception:  # noqa: BLE001 - an unreadable configuration means no healthy daemon, never a crashed probe
        return False
    connection = http.client.HTTPConnection(probe_address(host), port, timeout=timeout)
    try:
        # The daemon answers only a loopback Host (harness/api.py, _host_allowed), whatever address it listens on.
        connection.request("GET", "/health", headers={"Host": f"127.0.0.1:{port}"})
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


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
