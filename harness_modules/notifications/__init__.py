"""Phone notifications through a self-hosted ntfy server, as an add-on module (#334; docs/modules.md).

Everything notifications contributes to the daemon is registered here, in ``MODULE``. This file stays light: the
CLI imports it to list commands, so the service, routes and settings load only when the core asks for them.
"""

from __future__ import annotations

from harness.modules import Module, enabled_flag, lazy


MODULE = Module(
    name="notifications",
    switches=("notifications",),
    title="Phone notifications",
    docs=("docs/modules.md",),
    runtime_enabled=enabled_flag("notify"),
    runtime=lazy(".runtime", "NotificationsRuntime", call=True),
    owner_routes=lazy(".routes", "owner_routes"),
    admin_paths=frozenset({"/notify/test"}),
    settings=lazy(".settings", "specs", call=True),
    cli=(("notify test", "POST", "/notify/test", "send a test notification", ()),),
    cli_groups={"notify": "notifications"},
)
