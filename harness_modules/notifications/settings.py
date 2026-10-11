"""The settings registry keys notifications owns (Module.settings). Present only while the module is."""

from __future__ import annotations

from harness.modules import require_readable_file, require_url, setting_bool, setting_hidden

NOTIFICATIONS = "Notifications"


def check_notifications(cfg) -> list[str]:
    errors = require_url(cfg.notify.server, "notify.server")
    if not cfg.notify.topic.strip():
        errors.append("notify.topic is not configured")
    if cfg.notify.token_file:
        errors.extend(require_readable_file(cfg.notify.token_file, "notify.token_file"))
    return errors


def _get_enabled(cfg):
    return cfg.notify.enabled


def _set_enabled(cfg, value):
    cfg.notify.enabled = bool(value)


def specs() -> list:
    return [
        setting_bool("notifications.enabled", "Notifications",
                     "Runtime enable for ntfy notifications. Does not configure a server or topic.",
                     "Features", False, _get_enabled, _set_enabled, ("notify", "enabled"),
                     apply_mode="daemon_restart", modules=("notifications",), enable_check=check_notifications),
        setting_hidden("notify.server", "Notification server", "ntfy server URL.",
                       NOTIFICATIONS, ("notify", "server"), modules=("notifications",)),
        setting_hidden("notify.topic", "Notification topic", "ntfy topic name.",
                       NOTIFICATIONS, ("notify", "topic"), modules=("notifications",)),
        setting_hidden("notify.token_file", "Notification token file", "File holding the ntfy write token.",
                       NOTIFICATIONS, ("notify", "token_file"), modules=("notifications",)),
    ]
