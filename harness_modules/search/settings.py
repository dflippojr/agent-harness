"""The settings registry key session search owns (Module.settings). Present only while the module is."""

from __future__ import annotations

from harness.modules import cfg_field, setting_bool


def check_search(cfg) -> list[str]:
    return []


def specs() -> list:
    return [
        setting_bool("search.enabled", "Session search",
                     "Runtime enable for session search. Does not install the search module.",
                     "Features", False, *cfg_field("search", "enabled", cast=bool), ("search", "enabled"),
                     apply_mode="daemon_restart", modules=("search",), enable_check=check_search),
    ]
