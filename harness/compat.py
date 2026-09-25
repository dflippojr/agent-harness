"""First-party client/server compatibility contract (issue #69).

Protocol versions are deliberately independent of package and Git versions.  The
server accepts the current protocol and its immediate predecessor.  Release and
build identifiers are only used to discover an available update.
"""

from __future__ import annotations

import os
import re


RELEASE = "0.9.0"
BUILD_ID = os.environ.get("HARNESS_BUILD_ID", "2026.09.17.1")
WEB_BUILD_ID = "2026.09.17.1"
MAC_CLIENT_VERSION = "4.2"

PROTOCOLS = {
    "app": {"min": 1, "max": 2},
    "admin": {"min": 1, "max": 2},
    "runner": {"min": 1, "max": 2},
}
CLIENT_PROTOCOLS = {"web": 2, "cli": 2, "runner": 2}
MINIMUM_CLIENTS = {"web": "2026.09.16", "cli": "4.1", "runner": "4.1"}

CLIENT_HEADER = "X-Agent-Harness-Client"
_HEADER_RE = re.compile(r"^(web|cli|runner)/(\d+)$")


def metadata(capabilities: dict) -> dict:
    """Public, secret-free compatibility and update discovery metadata."""
    return {
        "release": RELEASE,
        "build_id": BUILD_ID,
        "protocols": PROTOCOLS,
        "minimum_clients": MINIMUM_CLIENTS,
        "capabilities": capabilities,
        "update_hint": {
            "web": {
                "build_id": WEB_BUILD_ID,
                "protocol": CLIENT_PROTOCOLS["web"],
                "action": "reload",
            },
            "mac": {
                "version": MAC_CLIENT_VERSION,
                "admin_protocol": CLIENT_PROTOCOLS["cli"],
                "runner_protocol": CLIENT_PROTOCOLS["runner"],
                "action": "harness update",
                "manifest_url": "/mac-client/manifest.json",
            },
        },
    }


def surface_for_path(path: str) -> str | None:
    if path == "/api/v1" or path.startswith("/api/v1/"):
        return "app"
    if path == "/api/admin/v1" or path.startswith("/api/admin/v1/"):
        return "admin"
    if path.startswith("/runners/"):
        return "runner"
    return None


def check_client(header: str, surface: str) -> dict:
    """Return compatibility state for a request header and protocol surface."""
    if not header:
        return {"state": "transition", "surface": surface, "notice": "missing_client_version"}
    match = _HEADER_RE.fullmatch(header.strip())
    if not match:
        return {"state": "invalid", "surface": surface, "detected": header}
    kind, raw_version = match.groups()
    # Native pairing is intentionally on the app surface, so the CLI may identify
    # itself there even though its ordinary operations use the admin surface.
    allowed = {"app": {"web", "cli"}, "admin": {"web", "cli"}, "runner": {"runner"}}[surface]
    version = int(raw_version)
    if kind not in allowed:
        return {"state": "invalid", "surface": surface, "kind": kind, "detected": version}
    supported = PROTOCOLS[surface]
    state = "compatible"
    if version < supported["min"]:
        state = "client_update_required"
    elif version > supported["max"]:
        state = "daemon_update_required"
    return {"state": state, "surface": surface, "kind": kind, "detected": version,
            "supported": dict(supported), "update": update_action(kind)}


def update_action(kind: str) -> dict:
    if kind == "web":
        return {"action": "reload_and_update", "build_id": WEB_BUILD_ID}
    if kind in {"cli", "runner"}:
        return {"action": "harness update", "version": MAC_CLIENT_VERSION,
                "manifest_url": "/mac-client/manifest.json"}
    return {"action": "update_client"}


def runner_compatibility(info: dict) -> dict:
    raw = info.get("protocol")
    if raw is None:
        return {"state": "transition", "notice": "missing_client_version",
                "supported": dict(PROTOCOLS["runner"]), "update": update_action("runner")}
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return {"state": "invalid", "detected": raw, "supported": dict(PROTOCOLS["runner"]),
                "update": update_action("runner")}
    supported = PROTOCOLS["runner"]
    if version < supported["min"]:
        state = "client_update_required"
    elif version > supported["max"]:
        state = "daemon_update_required"
    else:
        state = "compatible"
    return {"state": state, "detected": version, "supported": dict(supported),
            "update": update_action("runner")}
