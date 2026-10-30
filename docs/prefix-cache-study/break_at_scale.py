"""#774: what one prefix break costs on a long prompt, on a running llama-server. No canary session gets this long.

    python docs/prefix-cache-study/break_at_scale.py CAPTURE.jsonl OUT.json [--server URL] [--tokens 40000]

CAPTURE.jsonl (`bakeoff.canary --capture-prompts`) gives the real system prompt, task and tool schemas. A conversation
of tool results is built to about --tokens tokens, sent once cold, then with one earlier result replaced by a masking
receipt at 90%, 50% and 10% of its length. Between them the original is sent again to refill the slot. One token is
generated each time; `timings` gives prompt_n, cache_n and prompt_ms.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
from pathlib import Path

import httpx

CHUNK_LINES = 60  # lines of log text in one tool result; a few thousand characters, like the sessions' outputs


def tool_result(rng: random.Random, index: int) -> str:
    return "\n".join(
        f"2026-09-12T{rng.randrange(24):02}:{rng.randrange(60):02}:{rng.randrange(60):02}Z ERROR export.worker "
        f"rid={rng.getrandbits(48):012x} result {index} row {i} rows={rng.randrange(10**7)}"
        for i in range(CHUNK_LINES))


def conversation(row: dict, count: int) -> list[dict]:
    """The captured system prompt and task, then `count` read_file calls and their results. Fixed seed: same bytes."""
    rng = random.Random(774)
    messages = copy.deepcopy(row["messages"][:2])
    for i in range(count):
        call_id = f"call{i:04}"
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": f"logs/part{i}.log"})}}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_result(rng, i)})
    return messages


def masked(messages: list[dict], fraction: float) -> list[dict]:
    """`messages` with the tool result `fraction` of the way through replaced by a short receipt."""
    tools = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    out = copy.deepcopy(messages)
    index = tools[int(fraction * (len(tools) - 1))]
    out[index]["content"] = f"[Observation receipt] tool=read_file characters={len(out[index]['content'])}"
    return out


def send(client: httpx.Client, messages: list[dict], tools: list[dict]) -> dict:
    body = {"messages": messages, "tools": tools, "max_tokens": 1, "temperature": 0, "stream": False}
    timings = client.post("/v1/chat/completions", json=body).raise_for_status().json()["timings"]
    return {"prompt_n": timings["prompt_n"], "cache_n": timings["cache_n"], "prompt_ms": round(timings["prompt_ms"])}


def tokens_of(client: httpx.Client, messages: list[dict], tools: list[dict]) -> int:
    prompt = client.post("/apply-template", json={"messages": messages, "tools": tools}).raise_for_status().json()
    return len(client.post("/tokenize", json={"content": prompt["prompt"], "add_special": False,
                                              "parse_special": True}).raise_for_status().json()["tokens"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--server", default="http://127.0.0.1:8082")
    ap.add_argument("--tokens", type=int, default=40000)
    a = ap.parse_args()
    row = json.loads(a.capture.read_text(encoding="utf-8").splitlines()[0])
    results = []
    with httpx.Client(base_url=a.server, timeout=600) as client:
        per_result = tokens_of(client, conversation(row, 2), row["tools"]) - tokens_of(client, conversation(row, 1),
                                                                                      row["tools"])
        messages = conversation(row, a.tokens // per_result)
        total = tokens_of(client, messages, row["tools"])
        print(f"{a.tokens // per_result} results, {total} tokens")

        def step(name: str, fraction, sent: list[dict]) -> None:
            results.append({"step": name, "fraction": fraction, "total": total, **send(client, sent, row["tools"])})
            print(results[-1], flush=True)
            a.out.write_text(json.dumps(results, indent=1), encoding="utf-8")

        step("cold", None, messages)
        step("same again", None, messages)
        for fraction in (0.9, 0.5, 0.1):
            step("masked", fraction, masked(messages, fraction))
            step("refill", None, messages)


if __name__ == "__main__":
    main()
