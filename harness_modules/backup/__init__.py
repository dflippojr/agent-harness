"""The nightly backup, and verifying and restoring one, as an add-on module (#334; docs/modules.md).

Contributes the scheduled backup (``backup.enabled``, ``backup.at``, ``backup.keep_days``, ``backup.dir``),
POST /maintenance/backup, the ``harness maintenance backup`` CLI row, the ``backup`` entry in GET /maintenance, the
backup metrics and the doctor check. ``python -m harness_modules.backup.restore verify|restore`` (#374) lives here
too. This file stays light: the CLI imports it to list commands.
"""

from __future__ import annotations

from harness.modules import Module


def _runtime(manager, module):
    from .runtime import BackupRuntime
    return BackupRuntime(manager, module)


def _owner_routes():
    from .routes import owner_routes
    return owner_routes


def _settings():
    from .settings import specs
    return specs()


def _doctor(report, cfg) -> None:
    from .runtime import doctor
    doctor(report, cfg)


def _runtime_enabled(cfg, switch: str) -> bool:
    return bool(cfg.backup.enabled)


MODULE = Module(
    name="backup",
    switches=("backup",),
    title="Nightly backup",
    docs=("docs/modules.md", "docs/INSTALL.md"),
    runtime_enabled=_runtime_enabled,
    runtime=_runtime,
    owner_routes=_owner_routes,
    admin_paths=frozenset({"/maintenance/backup"}),
    settings=_settings,
    doctor=_doctor,
    cli=(("maintenance backup", "POST", "/maintenance/backup", "back up the databases", ()),),
)
