"""The nightly backup, and verifying and restoring one, as an add-on module (#334; docs/modules.md).

Contributes the scheduled backup (``backup.enabled``, ``backup.at``, ``backup.keep_days``, ``backup.dir``),
POST /maintenance/backup, the ``harness maintenance backup`` CLI row, the ``backup`` entry in GET /maintenance, the
backup metrics and the doctor check. ``python -m harness_modules.backup.restore verify|restore`` (#374) lives here
too. This file stays light: the CLI imports it to list commands.
"""

from __future__ import annotations

from harness.modules import Module, enabled_flag, lazy


MODULE = Module(
    name="backup",
    switches=("backup",),
    title="Nightly backup",
    docs=("docs/modules.md", "docs/INSTALL.md"),
    runtime_enabled=enabled_flag("backup"),
    runtime=lazy(".runtime", "BackupRuntime", call=True),
    owner_routes=lazy(".routes", "owner_routes"),
    admin_paths=frozenset({"/maintenance/backup"}),
    settings=lazy(".settings", "specs", call=True),
    doctor=lazy(".runtime", "doctor", call=True),
    cli=(("maintenance backup", "POST", "/maintenance/backup", "back up the databases", ()),),
)
