"""Endpoint settings retain their existing registry and YAML names."""
from harness.modules import Config, module_installed, setting_int as _int, setting_float as _float, setting_bool as _bool

def check_endpoint(cfg: Config) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("endpoint requires the local_model module")
    if not cfg.models:
        errors.append("endpoint requires a configured local model")
    return errors


def apply_endpoint_queue(manager, old, new) -> None:
    manager.runner.gate.max_waiting = manager.cfg.endpoint.max_waiting
    manager.runner.gate.fair_seconds = manager.cfg.endpoint.agent_fair_seconds


def _get_max_waiting(cfg: Config):
    return cfg.endpoint.max_waiting


def _set_max_waiting(cfg: Config, value):
    cfg.endpoint.max_waiting = int(value)


def _get_fair(cfg: Config):
    return cfg.endpoint.agent_fair_seconds


def _set_fair(cfg: Config, value):
    cfg.endpoint.agent_fair_seconds = float(value)


def _get_req_timeout(cfg: Config):
    return cfg.endpoint.request_timeout_seconds


def _set_req_timeout(cfg: Config, value):
    cfg.endpoint.request_timeout_seconds = float(value)


def _get_enabled(cfg):
    return cfg.endpoint.enabled


def _set_enabled(cfg, value):
    cfg.endpoint.enabled = bool(value)


def specs():
    return [
        _int("endpoint.max_waiting", "Endpoint queue depth",
             "Inference requests waiting for the GPU before new ones get 429.",
             "Endpoint", 4, _get_max_waiting, _set_max_waiting, 0, 32, ("endpoint", "max_waiting"),
             modules=("endpoint",), live_apply=apply_endpoint_queue),
        _float("endpoint.agent_fair_seconds", "Agent fairness (seconds)",
               "After an agent turn waits this long, new endpoint requests queue behind it.",
               "Endpoint", 90, _get_fair, _set_fair, 10, 600, ("endpoint", "agent_fair_seconds"),
               modules=("endpoint",), live_apply=apply_endpoint_queue),
        _float("endpoint.request_timeout_seconds", "Endpoint request timeout (seconds)",
               "Give up on a hung inference request after this long.",
               "Endpoint", 1800, _get_req_timeout, _set_req_timeout, 30, 7200,
               ("endpoint", "request_timeout_seconds"), modules=("endpoint",)),
        _bool("endpoint.enabled", "Inference endpoint",
              "Runtime enable for the OpenAI/Anthropic-compatible endpoint.",
              "Features", False, _get_enabled, _set_enabled, ("endpoint", "enabled"),
              apply_mode="daemon_restart", modules=("endpoint",), enable_check=check_endpoint),
    ]
