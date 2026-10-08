"""Inference accounting metrics, supplied only by a present endpoint module."""
def endpoint_metrics(m, out, db) -> None:
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
