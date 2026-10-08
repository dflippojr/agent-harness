"""Prometheus metrics (GET /metrics), scraped by the observability stack for the "Agent Harness" dashboard.

Counters are computed from SQLite (sessions and events are never deleted, so they only grow); gauges come from the
live scheduler, guard, runners, and maintenance state. Every query runs on a pooled read-only connection, so a scrape
never holds up the writer (#294). The heavy session/event aggregates are cached for `CORE_CACHE_SECONDS`, so scrapes
closer together than that reuse one computation.
"""

from __future__ import annotations

import shutil
import threading
import time
import weakref

from . import telemetry
from .manager import Manager
from .runner import ACTIVE

CORE_CACHE_SECONDS = 10.0
_core_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # Database -> (computed_at, rows)
_core_cache_lock = threading.Lock()

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


def _histogram_lines(out: _Out, name: str, help_: str, series: list[tuple[dict, telemetry.Histogram]]) -> None:
    out.lines += [f"# HELP {name} {help_}", f"# TYPE {name} histogram"]
    for labels, hist in series:
        cumulative, total, count = hist.snapshot()
        base = "".join(f'{k}="{_esc(v)}",' for k, v in labels.items())
        for bound, n in cumulative:
            out.lines.append(f'{name}_bucket{{{base}le="{bound:g}"}} {n}')
        out.lines.append(f'{name}_bucket{{{base}le="+Inf"}} {count}')
        suffix = "{" + base.rstrip(",") + "}" if base else ""
        out.lines.append(f"{name}_sum{suffix} {total:g}")
        out.lines.append(f"{name}_count{suffix} {count}")


def _telemetry_metrics(out: _Out) -> None:
    _histogram_lines(out, "harness_db_lock_held_seconds",
                     "Time a SQLite connection was held per outermost acquisition of its lock, by calling method.",
                     [({"method": method}, h) for method, h in telemetry.lock_held.items()])
    _histogram_lines(out, "harness_event_loop_stall_seconds",
                     "Event-loop lag: how much later than scheduled a 10 ms probe sleep woke up.",
                     [({}, telemetry.loop_stall)])


def _core_rows(db) -> dict:
    """The aggregate queries behind `_core_metrics`: whole-table scans of sessions, events and approvals."""
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
        round_resets = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'compaction' AND json_extract(data, '$.tier') = 'round_reset'"
        ).fetchone()[0]
        dead_end = db.conn.execute(
            "SELECT COALESCE(SUM(json_extract(data, '$.dead_end_retries')), 0) FROM events "
            "WHERE type = 'turn_metrics' AND json_extract(data, '$.dead_end_retries') IS NOT NULL"
        ).fetchone()[0]
        correlated = {
            "elide": db.conn.execute(
                "SELECT COALESCE(SUM(json_extract(data, '$.compaction_correlated_retries.elide')), 0) "
                "FROM events WHERE type = 'turn_metrics' "
                "AND json_extract(data, '$.compaction_correlated_retries') IS NOT NULL").fetchone()[0],
            "summary": db.conn.execute(
                "SELECT COALESCE(SUM(json_extract(data, '$.compaction_correlated_retries.summary')), 0) "
                "FROM events WHERE type = 'turn_metrics' "
                "AND json_extract(data, '$.compaction_correlated_retries') IS NOT NULL").fetchone()[0],
            "round_reset": db.conn.execute(
                "SELECT COALESCE(SUM(json_extract(data, '$.compaction_correlated_retries.round_reset')), 0) "
                "FROM events WHERE type = 'turn_metrics' "
                "AND json_extract(data, '$.compaction_correlated_retries') IS NOT NULL").fetchone()[0],
        }
        cache_row = db.conn.execute(
            "SELECT COALESCE(SUM(json_extract(data, '$.cache_tokens')), 0), "
            "COALESCE(SUM(json_extract(data, '$.recomputed_tokens')), 0) FROM events "
            "WHERE type = 'turn_metrics' AND json_extract(data, '$.cache_tokens') IS NOT NULL"
        ).fetchone()
        approvals = db.conn.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(decided_at - created_at), 0) FROM approvals GROUP BY status"
        ).fetchall()
        finished = db.conn.execute(
            "SELECT status, stop_reason, COUNT(*) FROM sessions WHERE status IN ('done', 'failed', 'cancelled') "
            "GROUP BY 1, 2").fetchall()
    return {"by_status": by_status, "tokens": tokens, "tools": tools, "kinds": kinds, "round_resets": round_resets,
            "dead_end": dead_end, "correlated": correlated, "cache_row": cache_row, "approvals": approvals,
            "finished": finished}


def _cached_core_rows(db) -> dict:
    """`_core_rows`, reused for CORE_CACHE_SECONDS. One scrape computes while concurrent ones wait for its result."""
    with _core_cache_lock:
        hit = _core_cache.get(db)
        if hit is None or time.monotonic() - hit[0] >= CORE_CACHE_SECONDS:
            hit = (time.monotonic(), _core_rows(db))
            _core_cache[db] = hit
    return hit[1]


def _core_metrics(m: Manager, out: _Out, db) -> dict:
    rows = _cached_core_rows(db)
    by_status, tokens, tools, kinds = rows["by_status"], rows["tokens"], rows["tools"], rows["kinds"]
    round_resets, dead_end, correlated = rows["round_resets"], rows["dead_end"], rows["correlated"]
    cache_row, approvals, finished = rows["cache_row"], rows["approvals"], rows["finished"]
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
    out.metric("harness_round_resets_total", "counter",
               "Context round resets from compaction events with tier round_reset.",
               [({}, round_resets)])
    out.metric("harness_dead_end_retries_total", "counter",
               "Native-loop tool calls that repeated a prior failure with the same arguments.",
               [({}, dead_end)])
    out.metric("harness_compaction_correlated_retries_total", "counter",
               "Dead-end retries within 5 model turns after elide, summary, or round_reset. Unit: retries.",
               [({"tier": tier}, correlated[tier]) for tier in ("elide", "summary", "round_reset")])
    out.metric("harness_prompt_cache_tokens_total", "counter",
               "llama-server prompt tokens served from cache versus recomputed (native loop only). Unit: tokens.",
               [({"kind": "cached"}, cache_row[0]), ({"kind": "recomputed"}, cache_row[1])])
    out.metric("harness_approvals_total", "counter", "Approval requests by outcome.",
               [({"status": st}, n) for st, n, _ in approvals])
    out.metric("harness_approval_wait_seconds_total", "counter", "Time approvals waited for a decision.",
               [({"status": st}, secs) for st, _, secs in approvals if st != "pending"])
    out.metric("harness_approvals_pending", "gauge", "Approvals waiting now.",
               [({}, sum(n for st, n, _ in approvals if st == "pending"))])
    return by_status

def _smart_review_metrics(out: _Out, db) -> None:
    with db.lock:
        smart_rows = db.conn.execute(
            "SELECT outcome, COALESCE(NULLIF(escalate_reason, ''), ''), COUNT(*), "
            "COALESCE(SUM(latency_ms), 0), COALESCE(SUM(cost_usd), 0) FROM smart_reviews GROUP BY 1, 2"
        ).fetchall()
    attempts = sum(n for _, _, n, _, _ in smart_rows)
    out.metric("harness_smart_review_attempts_total", "counter",
               "Smart-review provider calls (eligible ASK only).", [({}, attempts)])
    out.metric("harness_smart_review_auto_approvals_total", "counter", "Calls auto-approved in auto mode.",
               [({}, sum(n for outcome, _, n, _, _ in smart_rows if outcome == "auto_approved"))])
    out.metric("harness_smart_review_escalations_total", "counter", "Smart-review escalations by reason.",
               [({"reason": reason or outcome}, n) for outcome, reason, n, _, _ in smart_rows
                if outcome in ("escalated", "failed")])
    out.metric("harness_smart_review_failures_total", "counter", "Smart-review provider or schema failures.",
               [({"reason": reason or "provider error"}, n) for outcome, reason, n, _, _ in smart_rows
                if outcome == "failed"])
    out.metric("harness_smart_review_latency_ms_total", "counter", "Smart-review provider latency.",
               [({}, sum(ms for _, _, _, ms, _ in smart_rows))])
    out.metric("harness_smart_review_cost_usd_total", "counter",
               "Estimated or API-reported smart-review cost.",
               [({}, sum(cost for _, _, _, _, cost in smart_rows))])


def _backend_metrics(m: Manager, out: _Out, db) -> None:
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


def _runner_metrics(m: Manager, out: _Out) -> None:
    hub = m.hub.status()
    out.metric("harness_runner_online", "gauge", "1 while a runner (the MacBook) is connected.",
               [({"runner": r["name"]}, 1 if r["online"] else 0) for r in hub])


def _canary_metrics(m: Manager, out: _Out) -> None:
    """Latest results only (cfg.canary.metrics_limit), labelled by short SHA, to bound label cardinality (#265)."""
    from .canary import CanaryStore, SHORT_SHA
    rows = CanaryStore(m.db).latest(m.cfg.canary.metrics_limit)
    for name, key, help_ in (("harness_canary_pass_rate", "pass_rate", "Canary pass rate (0-1) per commit."),
                             ("harness_canary_turns", "turns", "Agent turns used by the canary run."),
                             ("harness_canary_prompt_tokens", "prompt_tokens", "Prompt tokens used by the canary run."),
                             ("harness_canary_wall_seconds", "wall_seconds", "Seconds the canary run's attempts had the GPU.")):
        out.metric(name, "gauge", help_, [({"sha": r["sha"][:SHORT_SHA]}, r[key]) for r in rows])


def _maintenance_metrics(m: Manager, out: _Out) -> None:
    if m.maintenance.last_report.get("at"):
        out.metric("harness_cleanup_last_run_timestamp_seconds", "gauge", "Last cleanup run.",
                   [({}, m.maintenance.last_report["at"])])
    try:
        free = shutil.disk_usage(m.cfg.data_dir).free
        out.metric("harness_data_disk_free_bytes", "gauge", "Free space on the data drive.", [({}, free)])
    except OSError:
        pass


def _skill_metrics(out: _Out, db, by_status) -> None:
    active = sum(by_status.get(s, 0) for s in ACTIVE)
    out.metric("harness_sessions_active", "gauge", "Sessions not yet finished.", [({}, active)])



def render(m: Manager) -> str:
    """Blocking (SQLite on a pooled read connection): the endpoint runs it in a worker thread. Sessions and their
    rows are counted in Web's store, which holds the owner's and members' (#330 decision 4); everything else in the
    main store. App sessions are counted in each App's metadata instead."""
    db, out = m.db, _Out()
    web = db.for_app("")
    with db.reading(), web.reading():
        by_status = _core_metrics(m, out, web)
        _smart_review_metrics(out, web)
        _backend_metrics(m, out, db)
        m.modules.metrics(out, db)  # add-on modules' own (images, image archive)
        _runner_metrics(m, out)
        _canary_metrics(m, out)
        _maintenance_metrics(m, out)
        _skill_metrics(out, db, by_status)
    _telemetry_metrics(out)
    return "\n".join(out.lines) + "\n"
