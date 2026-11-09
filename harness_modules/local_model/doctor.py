"""Optional hardware, model server and resource guard diagnostics."""
from __future__ import annotations
import os
import shlex
import sys
from pathlib import Path
import httpx
from harness.modules import doctor_run as run_command, local_owner

GPU_CHECK, MODEL_CHECK, FIT_CHECK = "NVIDIA GPU", "Model server", "GPU guard margin"
FIT_DEFAULT_MIB = 1024  # llama-server's own --fit-target when the unit sets none (docs/gpu-vram-budget.md)

def check_gpu(r, cfg) -> None:
    if not cfg.modules.local_model:
        r.ok(GPU_CHECK, "not required by the service profile")
        return
    code, out = run_command(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used",
                     "--format=csv,noheader"])
    if code != 0:
        r.fail(GPU_CHECK, "nvidia-smi not found or failed: install the NVIDIA driver")
        return
    name, driver, total, used = [x.strip() for x in out.splitlines()[0].split(",")]
    mib = int(total.split()[0])
    (r.ok if mib >= 15000 else r.warn)(GPU_CHECK, f"{name}, driver {driver}, {total} ({used} used)"
                                       + ("" if mib >= 15000 else "; under 16 GB, use the gpt-oss model"))
    if int(driver.split(".")[0]) < 580:
        r.warn("NVIDIA driver", f"{driver}; the CUDA 13 build of llama.cpp needs 580 or newer")


def check_model_server(r, cfg) -> None:
    if not cfg.modules.local_model:
        r.ok(MODEL_CHECK, "not installed by the hosted-provider service profile")
        return
    model = cfg.models[cfg.default_model]
    try:
        props = httpx.get(f"{model.base_url}/props", timeout=5).raise_for_status().json()  # #733: a 4xx isn't up
        n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        detail = f"{model.base_url}: {props.get('model_alias') or props.get('model_path', '?')}, n_ctx {n_ctx}"
        if props.get("is_sleeping"):
            detail += " (asleep; loads on the first request)"
        if n_ctx and n_ctx < model.context_tokens:
            r.warn(MODEL_CHECK, detail + f"; config expects {model.context_tokens}")
        else:
            r.ok(MODEL_CHECK, detail)
    except (httpx.HTTPError, ValueError) as e:
        paused = Path(cfg.gpu_guard.pause_flag).exists() if cfg.gpu_guard.enabled else False
        (r.warn if paused else r.fail)(MODEL_CHECK, f"{model.base_url} not answering ({type(e).__name__})"
                                       + ("; the resource guard has it paused or parked (unloaded until needed)"
                                          if paused else ""))


def check_guard(r, cfg):
    base = f"http://127.0.0.1:{cfg.port}"
    try:
        gpu = httpx.get(f"{base}/gpu", timeout=5,  # the daemon answers local callers only with its local owner token
                        headers={local_owner.HEADER: local_owner.read_token(cfg.data_dir)}).raise_for_status().json()
        if gpu.get("enabled"):
            flag = Path(cfg.gpu_guard.pause_flag).exists()
            if flag and gpu["state"] == "clear" and not gpu.get("lazy_load"):
                r.warn("Resource guard", f"pause flag {cfg.gpu_guard.pause_flag} exists but the guard is clear")
            else:
                parked = "; model parked until needed" if flag and gpu["state"] == "clear" else ""
                memory = gpu.get("memory") or {}
                low = "; RAM low, new work waits" if memory.get("low") else ""
                r.ok("Resource guard", f"state {gpu['state']}{parked}{low}")
            if gpu.get("plex_error"):
                r.warn("Plex detection", gpu["plex_error"])
    except (httpx.HTTPError, ValueError, KeyError) as error:
        r.warn("Resource guard", f"{base} not answering ({type(error).__name__})")

def llama_unit(instance: str) -> Path:
    """The llama-server unit install/install.sh writes for an instance (its default instance is Main)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd/user" / f"agent-harness-{(instance or 'Main').lower()}-llama.service"


def read_fit_target(unit: Path) -> int | None:
    """The unit's --fit-target in MiB, or None when it sets none. Undoes the installer's systemd_escape (every
    argument double-quoted, `"` and `\\` backslash-escaped, `%` doubled). OSError if unreadable, ValueError if the
    value isn't a whole number."""
    for line in unit.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            args = shlex.split(line.removeprefix("ExecStart=").replace("%%", "%"))
            for i, arg in enumerate(args):
                value = args[i + 1] if arg == "--fit-target" and i + 1 < len(args) else (
                    arg.removeprefix("--fit-target=") if arg.startswith("--fit-target=") else None)
                if value is not None:
                    if not value.isdigit():
                        raise ValueError(f"--fit-target {value!r} is not a whole number of MiB")
                    return int(value)
    return None


def check_fit_target(r, cfg) -> None:
    """#832: the Plex allowance needs a llama-server margin to land in, and the free-VRAM floor must sit below that
    margin, or the guard parks right after every load (docs/gpu-vram-budget.md, follow-up 6)."""
    guard = cfg.gpu_guard
    allowed, floor = guard.plex_transcodes_allowed, guard.min_free_vram_mib
    if not (cfg.modules.local_model and guard.enabled) or not (allowed or floor):
        return
    unit = llama_unit(getattr(r, "instance", ""))
    if getattr(r, "existing_server", False) or not sys.platform.startswith("linux"):
        why = ("this install uses a model server it doesn't run" if getattr(r, "existing_server", False)
               else f"doctor reads it from the systemd unit install/install.sh writes on Linux, not on {sys.platform}")
        r.ok(FIT_CHECK, f"not checked: llama-server's --fit-target is unknown ({why})")
        return
    try:
        fit = read_fit_target(unit)
    except (OSError, ValueError) as error:
        reason = error.strerror if isinstance(error, OSError) and error.strerror else error
        r.ok(FIT_CHECK, f"not checked: could not read --fit-target from {unit} ({reason})")
        return
    found = f"{unit} has --fit-target {fit}" if fit is not None else (
        f"{unit} sets no --fit-target (default {FIT_DEFAULT_MIB})")
    warned = False
    needed = 700 * allowed + 500 + 256
    if allowed and (fit is None or fit < needed):
        r.warn(FIT_CHECK, f"gpu_guard.plex_transcodes_allowed {allowed} needs llama-server --fit-target of at least "
                          f"{needed} MiB (700 × {allowed} + 500 + 256); {found}")
        warned = True
    margin = FIT_DEFAULT_MIB if fit is None else fit
    if floor and floor >= margin:
        held = (f"--fit-target {fit} ({unit})" if fit is not None else
                f"--fit-target {margin} (llama-server's default; {unit} sets none)")
        r.warn(FIT_CHECK, f"gpu_guard.min_free_vram_mib {floor} is not below {held}: the guard would park the model "
                          "right after every load")
        warned = True
    if not warned:
        r.ok(FIT_CHECK, f"--fit-target {margin}{'' if fit is not None else ' (default)'} in {unit} fits "
                        f"plex_transcodes_allowed {allowed} and min_free_vram_mib {floor}")


def run(report, cfg):
    check_gpu(report, cfg)
    check_fit_target(report, cfg)
    if getattr(report, "not_started", False):
        report.warn("Local model services", "not checked: startup was disabled; run doctor after starting services")
    else:
        check_model_server(report, cfg)
        check_guard(report, cfg)
