"""Scripted chat-completions endpoint. Standard library only; never loads a model.

    python -m bakeoff.fake_endpoint --script responses.json --port 8080

Each script entry has a `message` (OpenAI assistant message), optional `usage`,
`status`/`error`, or `delay`. Requests and the selected response index are logged
to JSONL when --requests is supplied. Exhaustion fails closed with HTTP 500.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ScriptedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, steps: list[dict], requests: Path | None = None):
        super().__init__(address, Handler)
        self.auxiliary = steps.get("auxiliary", []) if isinstance(steps, dict) else []
        self.steps = steps["steps"] if isinstance(steps, dict) else steps
        self.requests = requests
        self.index = 0
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status: int, value: dict):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/props"):
            self.send_json(200, {"status": "ok", "default_generation_settings": {"n_ctx": 32768}})
        elif self.path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{"id": "fake", "object": "model", "owned_by": "test"}]})
        else:
            self.send_json(404, {"error": {"message": "unsupported fake route"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": {"message": "only chat completions are supported"}})
            return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        with self.server.lock:
            auxiliary = next((s for s in self.server.auxiliary
                              if s["contains"] in json.dumps(payload.get("messages", []))), None)
            index = None if auxiliary else self.server.index
            if auxiliary:
                step = auxiliary
            else:
                self.server.index += 1
                step = self.server.steps[index] if index < len(self.server.steps) else {
                    "status": 500, "error": "script exhausted"}
            if self.server.requests:
                with self.server.requests.open("a", encoding="utf-8") as log:
                    log.write(json.dumps({"index": index, "request": payload}) + "\n")
        time.sleep(step.get("delay", 0))
        if step.get("status", 200) != 200:
            self.send_json(step["status"], {"error": {"message": step.get("error", "scripted model error"),
                                                     "type": "server_error"}})
            return
        message = {"role": "assistant", **step["message"]}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        usage = step.get("usage", {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
        base = {"id": f"fake-{index}", "created": int(time.time()), "model": payload.get("model", "fake")}
        if not payload.get("stream"):
            self.send_json(200, {**base, "object": "chat.completion",
                                 "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        delta = dict(message)
        if delta.get("tool_calls"):
            delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(delta["tool_calls"])]
        chunks = [{**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta,
                                                                                 "finish_reason": None}]},
                  {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {},
                                                                                 "finish_reason": finish}], "usage": usage}]
        body = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--requests", type=Path)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with ScriptedServer(("0.0.0.0", args.port), json.loads(args.script.read_text(encoding="utf-8")), args.requests) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
