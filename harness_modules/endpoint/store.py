"""The inference endpoint's request log on the core's shared, transactional SQLite database (#992).

The endpoint_requests table stays in the core's frozen baseline, so existing installs and backups keep their schema
while this add-on is absent (#334). The core's `/keys` listing still counts each key's rows (docs/modules.md); the
log itself is written here, on the core's writer queue.
"""

from __future__ import annotations

import time

from harness.modules import ModuleStore, database_writes


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
            self.core.touch_api_key(r["key_id"])
