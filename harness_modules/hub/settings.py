"""The Hub's optional runtime switch."""
from harness.modules import setting_bool


def _get(cfg):
    return cfg.hub.enabled


def _set(cfg, value):
    cfg.hub.enabled = bool(value)


def specs():
    return [setting_bool("hub.enabled", "Hub inventory", "Read-only inventory for the standalone Hub app.",
                         "Features", True, _get, _set, ("hub", "enabled"), modules=("hub",))]
