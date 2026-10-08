"""Resource guard and supervised model metrics."""
import time

def _guard_metrics(m, out) -> None:
    g = m.guard
    if g is not None:
        out.metric("harness_gpu_guard_paused", "gauge", "1 while the GPU guard holds the queue (pausing, paused, "
                                                        "or reloading the model).", [({}, 1 if g.active else 0)])
        out.metric("harness_gpu_guard_state", "gauge", "Current GPU guard state.",
                   [({"state": st}, 1 if g.state == st else 0) for st in ("clear", "pausing", "paused", "resuming")])
        out.metric("harness_gpu_guard_triggers", "gauge", "Detected GPU users by kind.",
                   [({"kind": k}, sum(1 for s in g.signals if s["kind"] == k)) for k in ("game", "plex")])
        out.metric("harness_gpu_guard_pauses_total", "counter", "Pauses since the daemon started.", [({}, g.pauses)])
        extra = time.time() - g._paused_at if g.active and g._paused_at else 0
        out.metric("harness_gpu_guard_paused_seconds_total", "counter", "Time paused since the daemon started.",
                   [({}, g.paused_seconds_total + extra)])
        _resource_metrics(m, out)


def _resource_metrics(m, out) -> None:
    """The resource guard's RAM check, the model's load state and the GPU (the Actions -> Resources numbers)."""
    from .resources import cpu_percent_since_last, gpu_reading
    g = m.guard
    mem = g.memory.status()
    for name, key, help_ in (("harness_resource_ram_available_bytes", "available_bytes", "Available physical memory."),
                             ("harness_resource_ram_total_bytes", "total_bytes", "Total physical memory."),
                             ("harness_resource_commit_bytes", "commit_bytes", "Committed memory (Windows)."),
                             ("harness_resource_commit_limit_bytes", "commit_limit_bytes", "Commit limit (Windows)."),
                             ("harness_resource_ram_threshold_bytes", "threshold_bytes",
                              "Available RAM below which new model, worker and image work waits (0 = off).")):
        if mem.get(key) is not None:
            out.metric(name, "gauge", help_, [({}, mem[key])])
    out.metric("harness_resource_memory_low", "gauge", "1 while new work waits for memory.",
               [({}, 1 if mem["low"] else 0)])
    if m.warmer.load_min_available is not None:
        out.metric("harness_model_load_min_available_bytes", "gauge",
                   "Lowest available physical memory seen during the most recent model load.",
                   [({}, m.warmer.load_min_available)])
    out.metric("harness_model_parked", "gauge", "1 while the local model is unloaded until something needs it.",
               [({}, 1 if g.state == "clear" and g.control.flagged() else 0)])
    out.metric("harness_model_pinned_until_seconds", "gauge",
               "End of the \"Load local model now\" window (epoch seconds), 0 when none.",
               [({}, m.warmer.pinned_until if m.warmer.pinned() else 0)])
    gpu = gpu_reading(cached=True)
    if gpu is not None:
        out.metric("harness_resource_vram_used_bytes", "gauge", "VRAM in use (nvidia-smi).", [({}, gpu["vram_used"])])
        out.metric("harness_resource_vram_total_bytes", "gauge", "VRAM total (nvidia-smi).", [({}, gpu["vram_total"])])
        out.metric("harness_resource_gpu_load_percent", "gauge", "GPU utilization (nvidia-smi).",
                   [({}, gpu["gpu_load"])])
    cpu = cpu_percent_since_last()
    if cpu is not None:
        out.metric("harness_resource_cpu_load_percent", "gauge", "CPU load since the previous scrape.", [({}, cpu)])


