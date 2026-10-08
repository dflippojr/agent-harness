"""Resource guard registry keys with their original YAML and overlay names."""
from harness.modules import Config, module_installed, setting_float as _float, setting_bool as _bool

GPU_GUARD = "GPU guard"

def check_gpu_guard(cfg: Config) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("gpu_guard requires the local_model module")
    if not cfg.gpu_guard.pause_flag.strip():
        errors.append("gpu_guard.pause_flag is not configured")
    return errors


def _get_gpu_poll(cfg: Config):
    return cfg.gpu_guard.poll_seconds


def _set_gpu_poll(cfg: Config, value):
    cfg.gpu_guard.poll_seconds = float(value)


def _get_gpu_resume(cfg: Config):
    return cfg.gpu_guard.resume_after_seconds


def _set_gpu_resume(cfg: Config, value):
    cfg.gpu_guard.resume_after_seconds = float(value)


def _get_gpu_drain(cfg: Config):
    return cfg.gpu_guard.drain_timeout_seconds


def _set_gpu_drain(cfg: Config, value):
    cfg.gpu_guard.drain_timeout_seconds = float(value)



def _get_enabled(cfg):
    return cfg.gpu_guard.enabled

def _set_enabled(cfg, value):
    cfg.gpu_guard.enabled = bool(value)

def specs():
    return [
        _float("gpu_guard.poll_seconds", "GPU guard poll (seconds)",
               "How often the GPU guard looks for games or Plex transcodes.",
               GPU_GUARD, 10, _get_gpu_poll, _set_gpu_poll, 2, 60, ("gpu_guard", "poll_seconds"),
               modules=("gpu_guard",)),
        _float("gpu_guard.resume_after_seconds", "GPU resume delay (seconds)",
               "The GPU must stay clear this long before the model is reloaded.",
               GPU_GUARD, 180, _get_gpu_resume, _set_gpu_resume, 10, 1800,
               ("gpu_guard", "resume_after_seconds"), modules=("gpu_guard",)),
        _float("gpu_guard.drain_timeout_seconds", "GPU drain timeout (seconds)",
               "Longest wait for the current model turn before the server is stopped.",
               GPU_GUARD, 300, _get_gpu_drain, _set_gpu_drain, 30, 1800,
               ("gpu_guard", "drain_timeout_seconds"), modules=("gpu_guard",)),
        _bool("gpu_guard.enabled", GPU_GUARD,
              "Runtime enable for pausing the model while a game or Plex transcode needs the GPU.",
              "Features", False, _get_enabled, _set_enabled, ("gpu_guard", "enabled"),
              apply_mode="daemon_restart", modules=("gpu_guard",), enable_check=check_gpu_guard),
    ]
