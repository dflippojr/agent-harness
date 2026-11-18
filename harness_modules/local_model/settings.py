"""Resource guard registry keys with their original YAML and overlay names."""
from harness.modules import (
    PLEX_TRANSCODES_ALLOWED_MAX, Config, cfg_field, module_installed, setting_float as _float, setting_bool as _bool,
    setting_int as _int,
)

GPU_GUARD = "GPU guard"

def check_gpu_guard(cfg: Config) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("gpu_guard requires the local_model module")
    if not cfg.gpu_guard.pause_flag.strip():
        errors.append("gpu_guard.pause_flag is not configured")
    return errors


def specs():
    return [
        _float("gpu_guard.poll_seconds", "GPU guard poll (seconds)",
               "How often the GPU guard looks for games or Plex transcodes.",
               GPU_GUARD, 10, *cfg_field("gpu_guard", "poll_seconds", cast=float), 2, 60, ("gpu_guard", "poll_seconds"),
               modules=("gpu_guard",)),
        _float("gpu_guard.resume_after_seconds", "GPU resume delay (seconds)",
               "The GPU must stay clear this long before the model is reloaded.",
               GPU_GUARD, 180, *cfg_field("gpu_guard", "resume_after_seconds", cast=float), 10, 1800,
               ("gpu_guard", "resume_after_seconds"), modules=("gpu_guard",)),
        _float("gpu_guard.drain_timeout_seconds", "GPU drain timeout (seconds)",
               "Longest wait for the current model turn before the server is stopped.",
               GPU_GUARD, 300, *cfg_field("gpu_guard", "drain_timeout_seconds", cast=float), 30, 1800,
               ("gpu_guard", "drain_timeout_seconds"), modules=("gpu_guard",)),
        _int("gpu_guard.min_free_vram_mib", "Minimum free VRAM (MiB)",
             "Free VRAM under this for two polls in a row parks the model. 0 turns the check off.",
             GPU_GUARD, 0, *cfg_field("gpu_guard", "min_free_vram_mib", cast=int), 0, 65536,
             ("gpu_guard", "min_free_vram_mib"), modules=("gpu_guard",)),
        _int("gpu_guard.plex_transcodes_allowed", "Plex transcodes allowed",
             "Plex hardware transcodes the model can share the GPU with. One more parks the model. 0 parks on the "
             "first, as before.",
             GPU_GUARD, 0, *cfg_field("gpu_guard", "plex_transcodes_allowed", cast=int), 0, PLEX_TRANSCODES_ALLOWED_MAX,
             ("gpu_guard", "plex_transcodes_allowed"), modules=("gpu_guard",)),
        _float("gpu_guard.vram_drain_timeout_seconds", "Low VRAM drain timeout (seconds)",
               "Longest wait for the current model turn when low free VRAM alone starts the pause.",
               GPU_GUARD, 30, *cfg_field("gpu_guard", "vram_drain_timeout_seconds", cast=float), 5, 1800,
               ("gpu_guard", "vram_drain_timeout_seconds"), modules=("gpu_guard",)),
        _bool("gpu_guard.enabled", GPU_GUARD,
              "Runtime enable for pausing the model while a game or Plex transcode needs the GPU.",
              "Features", False, *cfg_field("gpu_guard", "enabled", cast=bool), ("gpu_guard", "enabled"),
              apply_mode="daemon_restart", modules=("gpu_guard",), enable_check=check_gpu_guard),
    ]
