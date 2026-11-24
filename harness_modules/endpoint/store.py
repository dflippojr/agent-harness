"""The inference endpoint's request log on the core's shared, transactional SQLite database (#992).

The endpoint_requests table stays in the core's frozen baseline, so existing installs and backups keep their schema
while this add-on is absent (#334). Its queries live here: the log is written on the core's writer queue, and the
`/keys` listing's per-key `requests` count is read through `EndpointRuntime.key_request_counts`.
"""

from __future__ import annotations

import time

from harness.modules import ModuleStore, database_reads, database_writes

# Each key's logged requests, searched by key through the `endpoint_requests_key` index (#669): the log is never
# pruned, so the count never scans it.
REQUEST_COUNTS = "SELECT key_id, COUNT(*) FROM endpoint_requests WHERE key_id IN ({marks}) GROUP BY key_id"
_CHUNK = 500


class EndpointRequestStore(ModuleStore):
    """One accounting row per `/v1` request."""

    @database_writes
    def log_endpoint_request(self, r: dict) -> None:
        """Log the request and, in the same transaction, mark its key used (the core's `touch_api_key`)."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO endpoint_requests (key_id, route, model, stream, status, prompt_tokens, "
                "completion_tokens, wait_ms, total_ms, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["key_id"], r["route"], r["model"], int(r["stream"]), r["status"], r.get("prompt_tokens", 0),
                 r.get("completion_tokens", 0), r.get("wait_ms", 0), r.get("total_ms", 0), time.time()))
            self.core.credentials.touch_api_key(r["key_id"])

    @database_reads
    def request_counts(self, key_ids: list[str]) -> dict[str, int]:
        """{key id: its logged requests} for the keys in `key_ids` that have any."""
        ids = list(dict.fromkeys(key_ids))
        counts: dict[str, int] = {}
        with self.lock:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i:i + _CHUNK]
                rows = self.conn.execute(REQUEST_COUNTS.format(marks=",".join("?" * len(part))), part).fetchall()
                counts.update({row[0]: row[1] for row in rows})
        return counts
