"""Raw throughput: prompt processing and decode speed at increasing context sizes."""

from __future__ import annotations

import httpx

FILLER = (
    "The scheduler batches queue entries so restarts do not lose work, while the sandbox manager streams "
    "transcript chunks back to the phone client and the model router keeps tail latency predictable. "
)


def build_prompt(base_url: str, target_tokens: int) -> tuple[str, int]:
    unit = httpx.post(f"{base_url}/tokenize", json={"content": FILLER}, timeout=30).json()["tokens"]
    repeats = max(1, target_tokens // len(unit))
    text = FILLER * repeats
    n = len(httpx.post(f"{base_url}/tokenize", json={"content": text}, timeout=120).json()["tokens"])
    return text, n


def measure(base_url: str, model: str, sizes=(2000, 8000, 16000, 30000), gen_tokens: int = 200) -> list[dict]:
    rows = []
    for size in sizes:
        text, n = build_prompt(base_url, size)
        resp = httpx.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": text + "\n\nWrite a long, detailed summary of the text above."}],
                "max_tokens": gen_tokens,
                "cache_prompt": False,
            },
            timeout=1800,
        )
        resp.raise_for_status()
        t = resp.json().get("timings", {})
        rows.append({
            "context_tokens": n,
            "prompt_seconds": round(t.get("prompt_ms", 0) / 1000, 2),
            "prompt_tps": round(t.get("prompt_per_second", 0), 1),
            "gen_tokens": t.get("predicted_n", 0),
            "gen_tps": round(t.get("predicted_per_second", 0), 1),
        })
    return rows
