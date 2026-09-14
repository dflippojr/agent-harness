"""What does a bigger context window cost? Starts one model at several --ctx-size values and records memory use and
prompt/decode speed at prompt lengths up to near each limit.

    python -m bakeoff.context_test --model qwen3.6-35b-a3b --ctx 32768,65536,131072

`--fit on` places whatever doesn't fit in VRAM into system RAM, so a larger KV cache can push more expert layers out
of VRAM and slow decode even for short prompts. Writes runs/context-<timestamp>/context.md and context.json.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from datetime import datetime

from . import perf
from .run import RUNS
from .server import LlamaServer, gpu_memory_used_mib, load_config, system_memory_mib


def process_memory_mib(pid: int) -> tuple[int, int]:
    """(private bytes, working set) of a process in MiB."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$p = Get-Process -Id {pid}; \"$($p.PrivateMemorySize64) $($p.WorkingSet64)\""],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return int(out[0]) // 2**20, int(out[1]) // 2**20


def snapshot(server: LlamaServer) -> dict:
    private, working_set = process_memory_mib(server.proc.pid)
    avail, commit, _ = system_memory_mib()
    return {"vram_mib": gpu_memory_used_mib(), "server_private_mib": private, "server_ws_mib": working_set,
            "system_avail_mib": avail, "system_commit_mib": commit}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.6-35b-a3b")
    parser.add_argument("--ctx", default="32768,65536,131072")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out_dir = RUNS / f"context-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True)
    results = []
    for ctx in (int(c) for c in args.ctx.split(",")):
        config = copy.deepcopy(load_config())
        config["ctx_size"] = ctx
        sizes = [s for s in (2000, 16000, 30000, 60000, 120000) if s < ctx - 3000]
        row: dict = {"ctx": ctx}
        try:
            with LlamaServer(args.model, out_dir / f"server-{ctx}.log", config) as server:
                row["load_seconds"] = round(server.load_seconds or 0, 1)
                row["after_load"] = snapshot(server)
                print(f"[ctx {ctx}] loaded in {row['load_seconds']}s: {row['after_load']}", flush=True)
                row["perf"] = perf.measure(server.base_url, args.model, sizes=sizes)
                for p in row["perf"]:
                    print(f"[ctx {ctx}] {p}", flush=True)
                row["after_longest"] = snapshot(server)
                print(f"[ctx {ctx}] after longest prompt: {row['after_longest']}", flush=True)
        except Exception as e:  # e.g. the server can't fit this context at all
            row["error"] = str(e)
            print(f"[ctx {ctx}] error: {e}", flush=True)
        results.append(row)
        (out_dir / "context.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    lines = [f"# Context size test: {args.model} ({out_dir.name})", "",
             "| ctx | Load s | VRAM MiB | Server private MiB | Server WS MiB (after longest) | System avail MiB (after longest) |",
             "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['ctx']} | error: {r['error'][:80]} | | | | |")
            continue
        a, b = r["after_load"], r["after_longest"]
        lines.append(f"| {r['ctx']} | {r['load_seconds']} | {a['vram_mib']} | {a['server_private_mib']} | "
                     f"{b['server_ws_mib']} | {b['system_avail_mib']} |")
    lines += ["", "| ctx | Prompt tokens | Prompt s | Prompt tok/s | Gen tok/s |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        for p in r.get("perf", []):
            lines.append(f"| {r['ctx']} | {p['context_tokens']} | {p['prompt_seconds']} | {p['prompt_tps']} | {p['gen_tps']} |")
    (out_dir / "context.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report: {out_dir / 'context.md'}", flush=True)


if __name__ == "__main__":
    main()
