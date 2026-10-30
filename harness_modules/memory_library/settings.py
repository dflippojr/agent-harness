"""Runtime switch owned by the memory library module."""
from harness.modules import cfg_field, setting_bool


def check_memory_library(cfg):
    # A pre-existing local clone works without a configured remote, as before.
    return []


def specs():
    return [setting_bool(
        "memory_library.enabled", "Memory library",
        "Runtime enable for personal memory tools. Does not install the memory library module.",
        "Features", False, *cfg_field("memory_library", "enabled", cast=bool), ("memory_library", "enabled"),
        apply_mode="daemon_restart", modules=("memory_library",), enable_check=check_memory_library,
    )]
