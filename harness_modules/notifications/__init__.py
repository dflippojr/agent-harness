"""Phone notifications through a self-hosted ntfy server, as an add-on module (#334; docs/modules.md).

Everything notifications contributes to the daemon is registered here, in ``MODULE``. This file stays light: the
CLI imports it to list commands, so the service, routes and settings load only when the core asks for them.
"""

from __future__ import annotations

from harness.modules import Module


def _runtime(manager, module):
    from .runtime import NotificationsRuntime
    return NotificationsRuntime(manager, module)


def _owner_routes():
    from .routes import owner_routes
    return owner_routes


def _settings():
    from .settings import specs
    return specs()


def _runtime_enabled(cfg, switch: str) -> bool:
    return bool(cfg.notify.enabled)


MODULE = Module(
    name="notifications",
    switches=("notifications",),
    title="Phone notifications",
    docs=("docs/modules.md",),
    runtime_enabled=_runtime_enabled,
    runtime=_runtime,
    owner_routes=_owner_routes,
    admin_paths=frozenset({"/notify/test"}),
    settings=_settings,
    cli=(("notify test", "POST", "/notify/test", "send a test notification", ()),),
    cli_groups={"notify": "notifications"},
)
