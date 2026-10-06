"""The skills switch keeps its existing YAML and registry key."""
from harness.modules import setting_bool

def check_skills(cfg):
    return []

def _get(cfg):
    return cfg.skills.enabled

def _set(cfg, value):
    cfg.skills.enabled = bool(value)

def specs():
    return [setting_bool("skills.enabled", "Instruction skills",
        "Runtime enable for owner-approved instruction skills. Does not install the skills module.",
        "Features", False, _get, _set, ("skills", "enabled"),
        apply_mode="daemon_restart", modules=("skills",), enable_check=check_skills)]
