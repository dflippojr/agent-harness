"""Replay captured canary sessions against a running llama-server and price each request's prefill (#856).

    python -m bakeoff.checkpoint_replay OUT.json CAPTURE.jsonl [CAPTURE.jsonl ...] [--server URL] [--container NAME]

CAPTURE.jsonl is `python -m bakeoff.canary --capture-prompts` output, one session per file (docs/prefix-cache-study.md).
Every request that got an answer in the run is sent again in order, with the same messages, tools and template
options, but generating one token, so servers started with different flags (for #856, `--checkpoint-min-step`) read
exactly the same prompts. The server's `timings` give the tokens it read (`prompt_n`), the tokens it reused (`cache_n`)
and `prompt_ms`.

One deviation from the live run: the server generated the run's reply, and here it generates one token, so the next
prompt reads the reply as prompt tokens. That costs the same at every setting.

With --container, the container's anonymous memory (where llama-server keeps context checkpoints and its prompt cache;
the mmap'd model is file cache and is left out) and swap are sampled every --sample-seconds from its cgroup, and the
peaks are recorded.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .prefix_diff import completed, is_summary_call, read_jsonl

CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")


def request_body(row: dict) -> dict:
    """The captured call as llama-server's chat endpoint takes it, cut to one generated token."""
    extra = dict(row.get("extra") or {})
    return {"messages": row["messages"], **({"tools": row["tools"]} if row.get("tools") else {}), **extra,
            "max_tokens": 1, "stream": False}


def replay_session(client: httpx.Client, rows: list[dict]) -> list[dict]:
    out = []
    for row in completed(rows):
        timings = client.post("/v1/chat/completions", json=request_body(row)).raise_for_status().json()["timings"]
        out.append({"seq": row.get("seq"), "summary": is_summary_call(row),
                    "run_prompt_tokens": row.get("prompt_tokens"), "run_cache_tokens": row.get("cache_tokens"),
                    "prompt_n": timings["prompt_n"], "cache_n": timings["cache_n"],
                    "prompt_ms": round(timings["prompt_ms"], 1)})
    return out


def totals(requests: list[dict]) -> dict:
    read = sum(r["prompt_n"] for r in requests)
    cached = sum(r["cache_n"] for r in requests)
    return {"requests": len(requests), "prompt_tokens": read + cached, "cached": cached, "read": read,
            "reuse": round(cached / (read + cached), 3) if read + cached else None,
            "prefill_s": round(sum(r["prompt_ms"] for r in requests) / 1000, 1)}


class MemorySampler:
    """Peak anonymous memory and swap of a cgroup, read every `interval` seconds on a thread (a file read; no load)."""

    def __init__(self, cgroup: Path, interval: float = 2.0):
        self.cgroup, self.interval = cgroup, interval
        self.peak_anon = self.peak_swap = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def sample(self) -> None:
        stat = dict(line.split() for line in (self.cgroup / "memory.stat").read_text().splitlines())
        self.peak_anon = max(self.peak_anon, int(stat.get("anon", 0)))
        swap = self.cgroup / "memory.swap.current"
        if swap.exists():
            self.peak_swap = max(self.peak_swap, int(swap.read_text()))

    def _run(self) -> None:
        while not self._stop.wait(self.interval):  # __enter__ took the first sample
            self.sample()

    def __enter__(self) -> MemorySampler:
        self.sample()
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        self.sample()

    def result(self) -> dict:
        return {"peak_anon_mib": round(self.peak_anon / 2**20), "peak_swap_mib": round(self.peak_swap / 2**20)}


def container_cgroup(name: str) -> Path:
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name], check=True, capture_output=True,
                         text=True).stdout.strip()
    return CGROUP_ROOT / f"docker-{cid}.scope"


def replay(captures: list[Path], server: str, sampler: MemorySampler | None = None,
           client: httpx.Client | None = None) -> dict:
    sessions = []
    own = client is None
    client = client or httpx.Client(base_url=server, timeout=1800)
    started = time.time()
    try:
        if sampler:
            sampler.__enter__()
        try:
            for path in captures:
                requests = replay_session(client, read_jsonl(path))
                sessions.append({"capture": path.name, **totals(requests), "per_request": requests})
                print(json.dumps({k: v for k, v in sessions[-1].items() if k != "per_request"}), flush=True)
        finally:
            if sampler:
                sampler.__exit__(None, None, None)
    finally:
        if own:
            client.close()
    all_requests = [r for s in sessions for r in s["per_request"]]
    return {"server": server, "seconds": round(time.time() - started, 1), "total": totals(all_requests),
            **({"memory": sampler.result()} if sampler else {}), "sessions": sessions}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bakeoff.checkpoint_replay")
    ap.add_argument("out", type=Path)
    ap.add_argument("captures", type=Path, nargs="+")
    ap.add_argument("--server", default="http://127.0.0.1:8082")
    ap.add_argument("--container", help="sample this Docker container's anonymous memory and swap (Linux, cgroup v2)")
    ap.add_argument("--sample-seconds", type=float, default=2.0)
    ap.add_argument("--label", default="", help="recorded in OUT.json, e.g. the --checkpoint-min-step value")
    a = ap.parse_args(argv)
    sampler = MemorySampler(container_cgroup(a.container), a.sample_seconds) if a.container else None
    result = {"label": a.label, **replay(a.captures, a.server, sampler)}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("label", "seconds", "total") + (("memory",) if sampler else ())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
