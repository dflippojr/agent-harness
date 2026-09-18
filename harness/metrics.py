"""Prometheus metrics (GET /metrics), scraped by the observability stack for the "Agent Harness" dashboard.

Counters are computed from SQLite on each scrape (sessions and events are never deleted, so they only grow);
gauges come from the live scheduler, guard, runners, and maintenance state.
"""

from __future__ import annotations

import shutil
import time

from .manager import Manager
from .runner import ACTIVE

STATUSES = ("queued", "running", "waiting_approval", "waiting_target", "waiting_app", "waiting_limit",
            "done", "failed", "cancelled")


def _esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Out:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_: str, samples: list[tuple[dict, float]]) -> None:
        self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            label = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            self.lines.append(f"{name}{{{label}}} {float(value):g}" if label else f"{name} {float(value):g}")


def render(m: Manager) -> str:
    db, out = m.db, _Out()
    with db.lock:
        by_status = dict(db.conn.execute("SELECT status, COUNT(*) FROM sessions GROUP BY status").fetchall())
        tokens = db.conn.execute(
            "SELECT model, COALESCE(SUM(json_extract(totals, '$.prompt_tokens')), 0), "
            "COALESCE(SUM(json_extract(totals, '$.completion_tokens')), 0), "
            "COALESCE(SUM(json_extract(totals, '$.turns')), 0) FROM sessions GROUP BY model").fetchall()
        tools = db.conn.execute(
            "SELECT json_extract(data, '$.name'), json_extract(data, '$.ok'), COUNT(*) FROM events "
            "WHERE type = 'tool_result' GROUP BY 1, 2").fetchall()
        kinds = dict(db.conn.execute(
            "SELECT type, COUNT(*) FROM events WHERE type IN ('error', 'llm_retry', 'compaction', 'gpu_paused', "
            "'model_waking') GROUP BY type").fetchall())
        approvals = db.conn.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(decided_at - created_at), 0) FROM approvals GROUP BY status"
        ).fetchall()
        finished = db.conn.execute(
            "SELECT status, stop_reason, COUNT(*) FROM sessions WHERE status IN ('done', 'failed', 'cancelled') "
            "GROUP BY 1, 2").fetchall()

    out.metric("harness_up", "gauge", "The harness daemon is answering.", [({}, 1)])
    out.metric("harness_sessions", "gauge", "Sessions by current status.",
               [({"status": s}, by_status.get(s, 0)) for s in STATUSES])
    out.metric("harness_sessions_finished_total", "counter", "Finished sessions by status and stop reason.",
               [({"status": st, "reason": (r or "").split(":", 1)[0]}, n) for st, r, n in finished])
    positions = m.scheduler.positions()
    out.metric("harness_queue_depth", "gauge", "Sessions waiting for the GPU slot.",
               [({}, sum(1 for p in positions.values() if p > 0))])
    out.metric("harness_gpu_slot_busy", "gauge", "1 while a session holds the GPU slot.",
               [({}, 1 if m.scheduler.holder else 0)])
    out.metric("harness_generating", "gauge", "Model calls in flight.", [({}, len(m.runner.generating))])
    out.metric("harness_tokens_total", "counter", "Tokens across all sessions, compaction summaries included.",
               [({"model": model, "kind": "prompt"}, p) for model, p, _, _ in tokens]
               + [({"model": model, "kind": "completion"}, c) for model, _, c, _ in tokens])
    out.metric("harness_model_turns_total", "counter", "Model turns.", [({"model": model}, t) for model, _, _, t in tokens])
    last = m.runner.last_completion
    if last:
        out.metric("harness_last_turn_tokens_per_second", "gauge", "Speed of the latest model turn.",
                   [({"model": last["model"], "phase": "prompt"}, last["prompt_tps"]),
                    ({"model": last["model"], "phase": "generation"}, last["gen_tps"])])
        out.metric("harness_last_turn_timestamp_seconds", "gauge", "When the latest model turn ended.",
                   [({}, last["at"])])
    out.metric("harness_tool_calls_total", "counter", "Tool calls by tool and outcome.",
               [({"tool": name or "?", "ok": "true" if ok else "false"}, n) for name, ok, n in tools])
    out.metric("harness_events_total", "counter", "Errors, model-call retries, compactions, GPU pauses, and wakes.",
               [({"type": t}, kinds.get(t, 0)) for t in ("error", "llm_retry", "compaction", "gpu_paused",
                                                          "model_waking")])
    out.metric("harness_approvals_total", "counter", "Approval requests by outcome.",
               [({"status": st}, n) for st, n, _ in approvals])
    out.metric("harness_approval_wait_seconds_total", "counter", "Time approvals waited for a decision.",
               [({"status": st}, secs) for st, _, secs in approvals if st != "pending"])
    out.metric("harness_approvals_pending", "gauge", "Approvals waiting now.",
               [({}, sum(n for st, n, _ in approvals if st == "pending"))])
    backend_limits, backend_costs = [], []
    for name in m.cfg.backends:
        state = db.get_backend_usage(name)["data"]
        windows = state.get("unifiedWindows") or {}
        if state.get("rateLimitType") and state.get("utilization") is not None:
            windows = {**windows, state["rateLimitType"]: {"utilization": state["utilization"]}}
        for window, value in windows.items():
            if isinstance(value, dict) and value.get("utilization") is not None:
                backend_limits.append(({"backend": name, "window": window}, value["utilization"]))
        backend_costs.append(({"backend": name}, db.usage_tally(name, 0)["cost_usd"]))
    out.metric("harness_backend_utilization", "gauge", "Latest hosted-backend utilization from 0 to 1.",
               backend_limits)
    out.metric("harness_backend_cost_usd_total", "counter", "Hosted-backend reported cost estimate.",
               backend_costs)

    with db.lock:
        endpoint = db.conn.execute(
            "SELECT k.name, r.route, r.status, COUNT(*), COALESCE(SUM(r.prompt_tokens), 0), "
            "COALESCE(SUM(r.completion_tokens), 0), COALESCE(SUM(r.wait_ms), 0) FROM endpoint_requests r "
            "LEFT JOIN api_keys k ON k.id = r.key_id GROUP BY 1, 2, 3").fetchall()
    out.metric("harness_endpoint_requests_total", "counter", "Inference endpoint requests by key, route and status.",
               [({"key": k or "?", "route": route, "status": st}, n) for k, route, st, n, _, _, _ in endpoint])
    tokens: dict[tuple, float] = {}
    waits: dict[str, float] = {}
    for k, _, _, _, p, c, w in endpoint:
        tokens[(k or "?", "prompt")] = tokens.get((k or "?", "prompt"), 0) + p
        tokens[(k or "?", "completion")] = tokens.get((k or "?", "completion"), 0) + c
        waits[k or "?"] = waits.get(k or "?", 0) + w / 1000
    out.metric("harness_endpoint_tokens_total", "counter", "Inference endpoint tokens by key.",
               [({"key": k, "kind": kind}, v) for (k, kind), v in tokens.items()])
    out.metric("harness_endpoint_wait_seconds_total", "counter", "Time endpoint requests waited for the GPU.",
               [({"key": k}, v) for k, v in waits.items()])
    gate = m.runner.gate
    out.metric("harness_endpoint_active", "gauge", "Endpoint requests running and waiting now.",
               [({"state": "running"}, gate.endpoint_active), ({"state": "waiting"}, gate.endpoint_waiting)])

    if m.images is not None:
        with db.lock:
            images = db.conn.execute("SELECT model, source, status, COUNT(*), COALESCE(SUM(seconds), 0) FROM images "
                                     "GROUP BY 1, 2, 3").fetchall()
        out.metric("harness_images_total", "counter", "Image jobs by model, source and status.",
                   [({"model": mo, "source": so, "status": st}, n) for mo, so, st, n, _ in images])
        out.metric("harness_images_seconds_total", "counter", "Time spent on image jobs (ComfyUI execution).",
                   [({"model": mo}, sum(s for mo2, _, st, _, s in images if mo2 == mo and st == "done"))
                    for mo in sorted({row[0] for row in images})])
        out.metric("harness_images_gpu_taken", "gauge", "1 while image generation or upscaling has the GPU (language model unloaded).",
                   [({}, 1 if m.images.gpu_taken else 0)])
        out.metric("harness_images_queued", "gauge", "Image jobs waiting.", [({}, m.images.queue.qsize())])
        upscale = m.images.status().get("upscale") or {}
        out.metric("harness_images_upscale_available", "gauge",
                   "1 when optional Real-ESRGAN 2×/4× weights are installed.",
                   [({}, 1 if upscale.get("available") else 0)])

    hub = m.hub.status()
    out.metric("harness_runner_online", "gauge", "1 while a runner (the MacBook) is connected.",
               [({"runner": r["name"]}, 1 if r["online"] else 0) for r in hub])

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

    backup = m.maintenance.last_backup
    if backup.get("ok_at"):
        out.metric("harness_backup_last_success_timestamp_seconds", "gauge", "Last successful backup.",
                   [({}, backup["ok_at"])])
        out.metric("harness_backup_size_bytes", "gauge", "Size of the last backup.", [({}, backup.get("bytes", 0))])
    archive = m.image_archive.health()
    if archive.get("enabled"):
        out.metric("harness_image_archive_last_reconciliation_timestamp_seconds", "gauge",
                   "Last image archive reconciliation.", [({}, archive.get("last_reconciliation", 0))])
        out.metric("harness_image_archive_images", "gauge", "Image archive jobs by state.",
                   [({"state": state}, archive.get(state, 0)) for state in ("archived", "missing", "errors", "retained")])
        out.metric("harness_image_archive_bytes", "gauge", "Verified bytes in the image archive.",
                   [({}, archive.get("bytes", 0))])
        out.metric("harness_image_archive_free_bytes", "gauge", "Free space on the image archive volume.",
                   [({}, archive.get("free_bytes", 0))])
        out.metric("harness_image_archive_free_space_warning", "gauge",
                   "1 when image archive free space is below its configured threshold.",
                   [({}, 1 if archive.get("free_space_warning") else 0)])
    if m.maintenance.last_report.get("at"):
        out.metric("harness_cleanup_last_run_timestamp_seconds", "gauge", "Last cleanup run.",
                   [({}, m.maintenance.last_report["at"])])
    try:
        free = shutil.disk_usage(m.cfg.data_dir).free
        out.metric("harness_data_disk_free_bytes", "gauge", "Free space on the data drive.", [({}, free)])
    except OSError:
        pass
    active = sum(by_status.get(s, 0) for s in ACTIVE)
    out.metric("harness_sessions_active", "gauge", "Sessions not yet finished.", [({}, active)])
    return "\n".join(out.lines) + "\n"
