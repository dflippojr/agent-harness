"""Runtime switch owned by the memory library module."""
from harness.modules import setting_bool


def check_memory_library(cfg):
    # A pre-existing local clone works without a configured remote, as before.
    return []


def _get_enabled(cfg):
    return cfg.memory_library.enabled


def _set_enabled(cfg, value):
    cfg.memory_library.enabled = bool(value)


def specs():
    return [setting_bool(
        "memory_library.enabled", "Memory library",
        "Runtime enable for personal memory tools. Does not install the memory library module.",
        "Features", False, _get_enabled, _set_enabled, ("memory_library", "enabled"),
        apply_mode="daemon_restart", modules=("memory_library",), enable_check=check_memory_library,
    )]
