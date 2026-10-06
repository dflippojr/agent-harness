"""The settings registry key session search owns (Module.settings). Present only while the module is."""

from __future__ import annotations

from harness.modules import setting_bool


def check_search(cfg) -> list[str]:
    return []


def _get_enabled(cfg):
    return cfg.search.enabled


def _set_enabled(cfg, value):
    cfg.search.enabled = bool(value)


def specs() -> list:
    return [
        setting_bool("search.enabled", "Session search",
                     "Runtime enable for session search. Does not install the search module.",
                     "Features", False, _get_enabled, _set_enabled, ("search", "enabled"),
                     apply_mode="daemon_restart", modules=("search",), enable_check=check_search),
    ]
