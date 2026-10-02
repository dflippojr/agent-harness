"""Machine diagnostics for Actions -> Resources and /metrics (#311, docs/resource-guard.md).

One reading per request: the tab reads once when it opens and again only when the refresh icon is pressed; nothing
here polls. Every probe is best-effort and returns None (shown as "n/a") when the tool or counter isn't available,
so the panel works on a machine without an NVIDIA card or Docker.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from . import gpu_guard

log = logging.getLogger("harness.resources")

MIB = 1024 ** 2
GPU_CACHE_SECONDS = 10  # /metrics scrapes every 15 s; don't start nvidia-smi more often than this
_gpu_cache: tuple[float, dict | None] = (-1e9, None)


def _run(args: list[str], timeout: float = 5) -> str | None:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def gpu_reading(cached: bool = False) -> dict | None:
    """VRAM used/total in bytes and GPU load in percent from nvidia-smi (the first GPU)."""
    global _gpu_cache
    if cached and time.monotonic() - _gpu_cache[0] < GPU_CACHE_SECONDS:
        return _gpu_cache[1]
    out = _run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits"])
    reading = None
    if out and out.strip():
        try:
            used, total, load = (float(x) for x in out.strip().splitlines()[0].split(","))
            reading = {"vram_used": int(used * MIB), "vram_total": int(total * MIB), "gpu_load": load}
        except ValueError:
            reading = None
    _gpu_cache = (time.monotonic(), reading)
    return reading


def cpu_load() -> float | None:
    """System CPU load in percent over a short sample."""
    try:
        import psutil
        return float(psutil.cpu_percent(interval=0.3))
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def cpu_percent_since_last() -> float | None:
    """CPU load since the previous call, without sleeping (for /metrics scrapes)."""
    try:
        import psutil
        return float(psutil.cpu_percent(interval=None))
    except Exception:  # noqa: BLE001
        return None


def _process_memory(match) -> int | None:
    """Summed working set of processes whose name matches."""
    try:
        import psutil
    except ImportError:
        return None
    total, found = 0, False
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            if match(proc.info["name"] or "") and proc.info["memory_info"] is not None:
                total += proc.info["memory_info"].rss
                found = True
        except (psutil.Error, KeyError):
            continue
    return total if found else 0


def daemon_memory() -> int | None:
    try:
        import psutil
        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # noqa: BLE001
        return None


def container_memory(prefix: str = "harness-") -> int | None:
    """Memory used by the harness's worker and sandbox containers, from `docker stats`."""
    out = _run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"], timeout=10)
    if out is None:
        return None
    total = 0
    for line in out.splitlines():
        name, _, usage = line.partition("\t")
        if name.startswith(prefix):
            total += parse_size(usage.split("/")[0].strip())
    return total


def parse_size(text: str) -> int:
    """'1.5GiB' / '512MiB' / '12kB' -> bytes (docker stats units)."""
    units = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": MIB, "gb": 1000 ** 3, "gib": 1024 ** 3}
    text = text.strip().lower()
    for unit in sorted(units, key=len, reverse=True):
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)]) * units[unit])
            except ValueError:
                return 0
    return 0


async def model_status(m) -> dict:
    """unloaded / waking / loaded / loaded-until, from the warmer, for the default local model."""
    from . import warmup
    if not m.cfg.modules.local_model:
        return {"state": "disabled"}
    model = m.cfg.models[m.cfg.default_model]
    state = await m.warmer.state(model)
    shown = {warmup.READY: "loaded", warmup.SLEEPING: "unloaded", warmup.UNLOADED: "unloaded",
             warmup.WAKING: "waking", warmup.PAUSED: "paused"}.get(state, state)
    pinned = m.warmer.pinned_until if m.warmer.pinned() else None
    return {"name": model.name, "state": shown, "server_state": state, "pinned_until": pinned,
            "waking_seconds": m.warmer.waking_for(model)}


async def diagnostics(m) -> dict:
    """Everything the Resources tab shows, read once."""
    import asyncio
    gpu, cpu, llama, daemon, containers = await asyncio.gather(
        asyncio.to_thread(gpu_reading), asyncio.to_thread(cpu_load),
        asyncio.to_thread(_process_memory, lambda n: n.lower().startswith("llama-server")),
        asyncio.to_thread(daemon_memory), asyncio.to_thread(container_memory))
    memory = m.guard.memory.status() if m.guard is not None else gpu_guard.MemoryWatch(0).status()
    model = await model_status(m)
    holders = []
    if model.get("server_state") in ("ready", "waking"):
        holders.append("llama-server")
    if m.images is not None and m.images.gpu_taken:
        holders.append("ComfyUI")
    return {
        "as_of": time.time(),
        "vram": {"used_bytes": gpu and gpu["vram_used"], "total_bytes": gpu and gpu["vram_total"],
                 "holders": holders},
        "ram": {**memory, "harness": {"daemon_bytes": daemon, "llama_server_bytes": llama,
                                      "containers_bytes": containers}},
        "gpu_load": gpu and gpu["gpu_load"],
        "cpu_load": cpu,
        "model": model,
        "guard": m.guard.status() if m.guard is not None else {"enabled": False, "state": "clear"},
    }
