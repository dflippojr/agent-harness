"""Smoke-test the harness inference endpoint from another machine using only the Python standard library.

The API key is read from HARNESS_API_KEY so it never appears in saved command history. Example:

    HARNESS_API_KEY=hk-... python3 probe_inference_endpoint.py https://tower.example.ts.net
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request


def request(url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    return urllib.request.urlopen(req, timeout=180)  # noqa: S310 - caller explicitly supplies its own daemon


def read_stream(response) -> tuple[int, bool]:
    """Count the SSE data chunks and report whether any delta carried a tool call."""
    if response.headers.get_content_type() != "text/event-stream":
        raise RuntimeError(f"expected text/event-stream, got {response.headers.get_content_type()}")
    chunks = 0
    tool_call = False
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        chunks += 1
        event = json.loads(line[5:].strip())
        for choice in event.get("choices", []):
            if (choice.get("delta") or {}).get("tool_calls"):
                tool_call = True
    return chunks, tool_call


def main() -> int:
    if len(sys.argv) != 2 or not os.environ.get("HARNESS_API_KEY"):
        print("usage: HARNESS_API_KEY=... probe_inference_endpoint.py BASE_URL", file=sys.stderr)
        return 2
    base = sys.argv[1].rstrip("/")
    token = os.environ["HARNESS_API_KEY"]

    with request(f"{base}/v1/models", token) as response:
        models = json.load(response).get("data", [])
    if not models:
        raise RuntimeError("the endpoint returned no models")

    started = time.monotonic()
    body = {
        "model": models[0]["id"],
        "stream": True,
        "messages": [{"role": "user", "content": "Call get_weather for Lisbon. Do not answer directly."}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }],
        "tool_choice": "required",
    }
    with request(f"{base}/v1/chat/completions", token, body) as response:
        chunks, tool_call = read_stream(response)
    if not chunks or not tool_call:
        raise RuntimeError(f"stream ended without a tool call ({chunks} data chunks)")
    print(json.dumps({
        "ok": True,
        "models": len(models),
        "stream_chunks": chunks,
        "tool_call": tool_call,
        "seconds": round(time.monotonic() - started, 2),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
