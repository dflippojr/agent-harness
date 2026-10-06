"""Local stdio fixture. No Docker, external server, model or network."""

import json
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request or method is None:
        continue
    result = {}
    if method == "initialize":
        result = {"protocolVersion": "bad" if mode == "version" else "2024-11-05",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        tool = {"name": "echo", "description": "Echo arguments", "inputSchema": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
        result = {"tools": [tool]}
        if mode == "invalid_tool":
            tool["name"] = "bad/name"
        elif mode == "schema":
            tool["inputSchema"] = None
        elif mode == "bad_schema":
            tool["inputSchema"] = {"type": "not-a-type"}
        elif mode == "noargs":
            tool["inputSchema"] = {"type": "object"}
        elif mode == "duplicate":
            result["tools"] *= 2
        elif mode == "empty_pages":
            result = {"tools": [], "nextCursor": "same"}
        elif mode == "pagination" and "cursor" not in request["params"]:
            result = {"tools": [], "nextCursor": "next"}
        elif mode == "invalid_list":
            result = {"tools": ["bad"]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": request["params"]["arguments"].get("text", "")}],
                  "isError": mode == "tool_error"}
        if mode == "env":
            import os
            result["content"][0]["text"] = os.environ.get("TOKEN", "") + ":" + os.environ.get("DAEMON_SECRET", "")
    elif mode == "timeout":
        import time
        time.sleep(10)
        continue
    if mode in ("notification", "noisy"):
        for _ in range(32 if mode == "noisy" else 1):
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": "server", "method": "roots/list"}), flush=True)
    response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    if mode == "protocol_error":
        response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -1, "message": "bad"}}
    elif mode == "id":
        response["id"] = -1
    elif mode == "result":
        response["result"] = []
    elif mode == "malformed":
        print("not json", flush=True)
        continue
    elif mode == "oversize":
        print("x" * 1_000_001, flush=True)
        continue
    elif mode == "nonobject":
        response = []
    print(json.dumps(response), flush=True)
    if mode == "noisy":
        for _ in range(32):
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
