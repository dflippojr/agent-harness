"""Stand-in for the per-session MCP relay's loopback port (#306), never the real daemon.

Listens on 127.0.0.1:8790 inside its own container; the study's Claude container joins that network namespace
(`--network container:<relay>`), as a real session does since #300. Answers a canned harness status, records the
events the mod posts, and has a slow route to probe the hook budget. Every request is logged as one JSON line.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATUS = {"gpu_hold": False, "model": "stub-model", "queue_depth": 2, "pr_gates": {"#306": "pending"}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - quiet; the JSON lines are the log
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/slow":
            ms = int(parse_qs(url.query).get("ms", ["0"])[0])
            time.sleep(ms / 1000)
            return self.reply({"slept_ms": ms})
        self.reply(STATUS if url.path == "/status" else {"path": url.path})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode("utf-8", "replace")
        print(json.dumps({"t": round(time.time(), 3), "path": self.path, "body": json.loads(body or "null")}),
              flush=True)
        self.reply({"ok": True})

    def reply(self, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8790), Handler).serve_forever()
