"""The settings registry keys the backup owns (Module.settings). Present only while the module is."""

from __future__ import annotations

import re
from pathlib import Path

from harness.modules import Bounds, SettingSpec, setting_bool, setting_int

from .member_key import key_dir

TIME_OF_DAY = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def check_backup(cfg) -> list[str]:
    if not cfg.backup.dir.strip():
        return ["backup.dir is not configured"]
    parent = Path(cfg.backup.dir).expanduser()
    if not parent.parent.exists():
        return ["backup.dir parent is missing"]
    try:
        key_dir(cfg, parent)
    except ValueError as e:
        return [str(e)]
    return []


def apply_backup_schedule(manager, old, new) -> None:
    runtime = manager.modules.get("backup")
    if runtime is not None:
        runtime.service.reschedule_backup()


def _get_enabled(cfg):
    return cfg.backup.enabled


def _set_enabled(cfg, value):
    cfg.backup.enabled = bool(value)


def _get_at(cfg):
    return cfg.backup.at


def _set_at(cfg, value):
    text = str(value)
    if not TIME_OF_DAY.fullmatch(text):
        raise ValueError("backup.at must be HH:MM in 24-hour local time")
    cfg.backup.at = text


def _get_keep(cfg):
    return cfg.backup.keep_days


def _set_keep(cfg, value):
    cfg.backup.keep_days = int(value)


def _dir_get(cfg):
    return None


def _dir_set(cfg, value):
    raise ValueError("this setting is managed in local configuration")


def specs() -> list:
    return [
        SettingSpec(
            key="backup.member_key_dir", label="Member key backup directory",
            help="Separate key copies; keep apart from database backups off-site. Unset uses backup.dir/member-keys.",
            category="Backup", value_type="string", default=None, scope="admin", apply_mode="installer_only",
            getter=_dir_get, setter=_dir_set, sensitivity="hidden", readable=False, writable=False,
            yaml_path=("backup", "member_key_dir"), modules=("backup",),
        ),
        SettingSpec(
            key="backup.at", label="Backup time", help="Local time (HH:MM) for the nightly backup.",
            category="Backup", value_type="string", default="03:30", scope="admin", apply_mode="live",
            getter=_get_at, setter=_set_at, bounds=Bounds(pattern=TIME_OF_DAY.pattern),
            yaml_path=("backup", "at"), modules=("backup",),
            live_apply=apply_backup_schedule, live_undo=apply_backup_schedule,
        ),
        setting_int("backup.keep_days", "Backup retention (days)",
                    "Delete dated backup folders older than this.",
                    "Backup", 14, _get_keep, _set_keep, 1, 365, ("backup", "keep_days"), modules=("backup",)),
        setting_bool("backup.enabled", "Backups",
                     "Runtime enable for the nightly backup. Does not change the backup directory.",
                     "Features", False, _get_enabled, _set_enabled, ("backup", "enabled"),
                     apply_mode="daemon_restart", modules=("backup",), enable_check=check_backup),
        SettingSpec(
            key="backup.dir", label="Backup directory", help="Where nightly backups are written.",
            category="Backup", value_type="string", default=None, scope="admin", apply_mode="installer_only",
            getter=_dir_get, setter=_dir_set, sensitivity="hidden", readable=False, writable=False,
            yaml_path=("backup", "dir"), modules=("backup",),
        ),
    ]
