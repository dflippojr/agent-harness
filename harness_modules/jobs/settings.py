"""Jobs registry keys; original YAML paths and live polling behavior are preserved."""
from harness.modules import setting_bool, setting_float


def _get_enabled(cfg):
    return bool(cfg.jobs.enabled)


def _set_enabled(cfg, value):
    cfg.jobs.enabled = bool(value)


def check_jobs(cfg) -> list[str]:
    return []


def apply_jobs_poll(manager, old, new) -> None:
    if manager.jobs is not None:
        manager.jobs.poll_seconds = manager.cfg.jobs.poll_seconds


def _get_jobs_poll(cfg):
    return cfg.jobs.poll_seconds

def _set_jobs_poll(cfg, value):
    cfg.jobs.poll_seconds = float(value)


def specs():
    return [
        setting_float("jobs.poll_seconds", "Job polling interval (seconds)",
                      "How often scheduled jobs are checked.",
                      "Jobs", 30, _get_jobs_poll, _set_jobs_poll, 5, 300, ("jobs", "poll_seconds"),
                      modules=("jobs",), live_apply=apply_jobs_poll),
        setting_bool("jobs.enabled", "Scheduled jobs",
                     "Runtime enable for scheduled jobs. Does not install the jobs module.",
                     "Features", False, _get_enabled, _set_enabled, ("jobs", "enabled"),
                     apply_mode="daemon_restart", modules=("jobs",), enable_check=check_jobs),
    ]
