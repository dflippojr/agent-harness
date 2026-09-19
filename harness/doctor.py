"""Diagnose an install: python -m harness.doctor [--config-dir DIR] [--instance NAME]

Read-only. Prints OK / WARN / FAIL per check and exits 1 if anything failed. Generic counterpart of the tower's
ops/check-stack.ps1 (a `doctor` command as in Hermes Agent, docs/phase6a-hermes-study.md).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

GPU_CHECK = "NVIDIA GPU"
MODEL_CHECK = "Model server"
GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


class Report:
    def __init__(self):
        self.failed = 0
        self.warned = 0

    def ok(self, name: str, detail: str = "") -> None:
        print(f"{GREEN}[ OK ]{RESET} {name}  {detail}")

    def warn(self, name: str, detail: str) -> None:
        self.warned += 1
        print(f"{YELLOW}[WARN]{RESET} {name}  {detail}")

    def fail(self, name: str, detail: str) -> None:
        self.failed += 1
        print(f"{RED}[FAIL]{RESET} {name}  {detail}")


def run(args: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def check_gpu(r: Report, cfg) -> None:
    if not cfg.modules.local_model:
        r.ok(GPU_CHECK, "not required by the service profile")
        return
    code, out = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used",
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


def check_data_dir(r: Report, cfg) -> None:
    try:
        data = cfg.data_dir
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        free = shutil.disk_usage(data).free / 2**30
        (r.ok if free >= cfg.cleanup.min_free_gb else r.warn)(
            "Data directory", f"{data} writable, {free:.0f} GB free (sessions need {cfg.cleanup.min_free_gb})")
    except OSError as e:
        r.fail("Data directory", str(e))


def check_provider_containers(r: Report, cfg) -> None:
    """Provider CLI images and their egress proxies; service profile only."""
    for image in sorted({backend.image for backend in cfg.backends.values() if backend.enabled}):
        code, _ = run(["docker", "image", "inspect", image])
        (r.ok if code == 0 else r.fail)("Provider CLI image", image if code == 0 else f"{image} missing")
    for name, backend in cfg.backends.items():
        if not backend.enabled:
            continue
        container = f"harness-egress-{name}"
        code, status = run(["docker", "inspect", "--format", "{{.State.Status}}", container])
        (r.ok if code == 0 and status == "running" else r.fail)(
            "Provider egress", f"{name}: {status}" if code == 0 else f"{container} missing")


def check_docker(r: Report, cfg) -> None:
    code, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        r.fail("Docker", "engine not reachable: install and start Docker Engine or Docker Desktop")
        return
    r.ok("Docker", f"engine {out}")
    code, _ = run(["docker", "image", "inspect", cfg.sandbox.image])
    if code == 0:
        r.ok("Sandbox image", cfg.sandbox.image)
    else:
        r.fail("Sandbox image", f"{cfg.sandbox.image} missing: docker build -t {cfg.sandbox.image} sandbox")
    if cfg.profile == "service":
        check_provider_containers(r, cfg)


def check_model_server(r: Report, cfg) -> None:
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
                                       + ("; the GPU guard has it paused" if paused else ""))


def check_daemon_profile(r: Report, cfg, base: str) -> None:
    """Health, plus model state or provider logins - whichever the profile runs."""
    health = httpx.get(f"{base}/health", timeout=5).json()
    if health.get("profile") != cfg.profile:
        raise ValueError(f"daemon reports profile {health.get('profile')!r}, expected {cfg.profile!r}")
    if cfg.modules.local_model:
        state = httpx.get(f"{base}/models/status", timeout=10).json()[0]["state"]
        r.ok("Daemon", f"{base} up; model state {state}")
        return
    r.ok("Daemon", f"{base} up; {cfg.profile} profile")
    backends = httpx.get(f"{base}/backends", timeout=100).json()
    logged_in = [backend["name"] for backend in backends if backend.get("logged_in")]
    (r.ok if logged_in else r.warn)(
        "Provider login", ", ".join(logged_in) if logged_in else
        "none detected; run ops/backends/login.sh (Unix) or ops\\backends\\login.ps1 (Windows)")


def check_daemon(r: Report, cfg) -> None:
    base = f"http://127.0.0.1:{cfg.port}"
    try:
        check_daemon_profile(r, cfg, base)
        gpu = httpx.get(f"{base}/gpu", timeout=5).json()
        if gpu.get("enabled"):
            flag = Path(cfg.gpu_guard.pause_flag).exists()
            if flag and gpu["state"] == "clear":
                r.warn("GPU guard", f"pause flag {cfg.gpu_guard.pause_flag} exists but the guard is clear")
            else:
                r.ok("GPU guard", f"state {gpu['state']}")
        backup = httpx.get(f"{base}/maintenance", timeout=60).json().get("backup") or {}
        if backup.get("enabled"):
            (r.ok if backup.get("ok_at") else r.warn)("Backups", backup.get("path") or
                                                      "no backup yet (the first runs 5 minutes after start)")
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        r.fail("Daemon", f"{base} not answering ({type(e).__name__}); see {cfg.data_dir / 'logs'}")


def check_autostart(r: Report, cfg, args) -> None:
    """Scheduled task / systemd unit / launchd agent, whichever the platform installs."""
    if not args.instance:
        return
    with_server = not (args.existing_server or not cfg.modules.local_model)
    if sys.platform == "win32":
        for suffix in (("LlamaServer", "Daemon") if with_server else ("Daemon",)):
            task = f"AgentHarness-{args.instance}-{suffix}"
            code, out = run(["schtasks", "/Query", "/TN", task, "/FO", "CSV", "/NH"])
            if code == 0:
                r.ok("Autostart", f"{task}: {out.split(',')[-1].strip(chr(34))}")
            else:
                r.warn("Autostart", f"{task} not registered (run install.ps1 without -NoTasks)")
    elif sys.platform.startswith("linux"):
        for suffix in (("llama", "daemon") if with_server else ("daemon",)):
            unit = f"agent-harness-{args.instance.lower()}-{suffix}.service"
            code, status = run(["systemctl", "--user", "is-active", unit])
            (r.ok if code == 0 and status == "active" else r.warn)(
                "Autostart", f"{unit}: {status or 'not active'}")
    elif sys.platform == "darwin":
        label = f"com.agent-harness.{args.instance.lower()}.daemon"
        code, _ = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
        (r.ok if code == 0 else r.warn)("Autostart", f"{label}: " + ("loaded" if code == 0 else "not loaded"))


def check_images(r: Report, cfg) -> None:
    py = Path(cfg.images.comfy_dir) / "python_embeded" / "python.exe"
    (r.ok if py.exists() else r.fail)("Image generation", f"ComfyUI at {cfg.images.comfy_dir}"
                                      + ("" if py.exists() else " not found"))
    from .images import LIGHTNING_LORA, lightning_lora_status
    lora = lightning_lora_status(cfg.images)
    if lora["available"]:
        r.ok("Qwen quality-fast LoRA",
             f"{lora['path']} ({LIGHTNING_LORA['filename']}, {LIGHTNING_LORA['bytes']} bytes, "
             f"revision {LIGHTNING_LORA['revision']})")
    else:
        r.warn("Qwen quality-fast LoRA", lora["setup"])
    from . import upscale as upscale_mod
    if upscale_mod.missing_weights(cfg.images, verify_hash=True):
        r.warn("Image upscaling", upscale_mod.remediation(cfg.images))
    else:
        r.ok("Image upscaling", f"Real-ESRGAN x2plus/x4plus in {upscale_mod.models_dir(cfg.images)}")


def check_optional(r: Report, cfg) -> None:
    if cfg.web.enabled:
        try:
            n = len(httpx.get(f"{cfg.web.searxng_url}/search", params={"q": "test", "format": "json"},
                              timeout=15).json().get("results", []))
            r.ok("Web search", f"SearXNG answered with {n} results")
        except (httpx.HTTPError, ValueError) as e:
            r.fail("Web search", f"SearXNG at {cfg.web.searxng_url} not answering ({type(e).__name__})")
    if cfg.images.enabled:
        check_images(r, cfg)
    code, out = run(["tailscale", "serve", "status", "--json"])
    if code == 0 and str(cfg.port) in out:
        r.ok("Phone access", "tailscale serve publishes the daemon")
    else:
        r.warn("Phone access", "not published on a tailnet (optional; see docs/INSTALL.md)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose an agent-harness install")
    ap.add_argument("--config-dir")
    ap.add_argument("--instance", default="", help="scheduled task name prefix used by install.ps1, e.g. Main")
    ap.add_argument("--existing-server", action="store_true", help="the install uses a model server it doesn't run")
    args = ap.parse_args(argv)
    r = Report()

    from . import config as config_mod
    try:
        cfg = config_mod.load(args.config_dir)
        mode = f"model {cfg.default_model}" if cfg.modules.local_model else "hosted providers only"
        r.ok("Config", f"{args.config_dir or config_mod.ROOT / 'config'}; {cfg.profile} profile, {mode}, port {cfg.port}")
    except Exception as e:  # noqa: BLE001 - report any config problem
        r.fail("Config", f"{type(e).__name__}: {e}")
        return 1

    check_gpu(r, cfg)
    check_data_dir(r, cfg)
    check_docker(r, cfg)
    check_model_server(r, cfg)
    check_daemon(r, cfg)
    check_autostart(r, cfg, args)
    check_optional(r, cfg)

    print(f"\n{r.failed} failed, {r.warned} warnings")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
