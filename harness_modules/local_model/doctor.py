"""Optional hardware, model server and resource guard diagnostics."""
from __future__ import annotations
from pathlib import Path
import httpx
from harness.modules import doctor_run as run_command

GPU_CHECK, MODEL_CHECK = "NVIDIA GPU", "Model server"

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
        props = httpx.get(f"{model.base_url}/props", timeout=5).json()
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
        gpu = httpx.get(f"{base}/gpu", timeout=5).json()
        if gpu.get("enabled"):
            flag = Path(cfg.gpu_guard.pause_flag).exists()
            if flag and gpu["state"] == "clear" and not gpu.get("lazy_load"):
                r.warn("Resource guard", f"pause flag {cfg.gpu_guard.pause_flag} exists but the guard is clear")
            else:
                parked = "; model parked until needed" if flag and gpu["state"] == "clear" else ""
                memory = gpu.get("memory") or {}
                low = "; RAM low, new work waits" if memory.get("low") else ""
                r.ok("Resource guard", f"state {gpu['state']}{parked}{low}")
    except (httpx.HTTPError, ValueError, KeyError) as error:
        r.warn("Resource guard", f"{base} not answering ({type(error).__name__})")

def run(report, cfg):
    check_gpu(report, cfg)
    if getattr(report, "not_started", False):
        report.warn("Local model services", "not checked: startup was disabled; run doctor after starting services")
    else:
        check_model_server(report, cfg)
        check_guard(report, cfg)
