"""What does a bigger context window cost? Starts one model at several --ctx-size values and records memory use and
prompt/decode speed at prompt lengths up to near each limit.

    python -m bakeoff.context_test --model qwen3.6-35b-a3b --ctx 32768,65536,131072

`--fit on` places whatever doesn't fit in VRAM into system RAM, so a larger KV cache can push more expert layers out
of VRAM and slow decode even for short prompts. Writes runs/context-<timestamp>/context.md and context.json.

    python -m bakeoff.context_test --base-url http://127.0.0.1:8080 --ctx 65536 --server-pid 4242

--base-url measures a llama-server that is already running (on the tower, say) instead of starting one. It takes that
server's --ctx-size as one value, to pick the prompt lengths. --server-pid adds the server's process memory: on Linux it
is read from /proc, elsewhere through PowerShell.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import perf
from .run import RUNS
from .server import LlamaServer, gpu_memory_used_mib, load_config, system_memory_mib

PROC = Path("/proc")


def _mib_fields(path: Path, keys: tuple[str, ...]) -> dict[str, int]:
    """The named `Key: <n> kB` lines of a /proc file, in MiB."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        if key in keys:
            found[key] = int(rest.split()[0]) // 1024
    missing = [k for k in keys if k not in found]
    if missing:
        raise RuntimeError(f"{path} has no {', '.join(missing)} line")
    return found


def process_memory_linux(pid: int, proc_root: Path = PROC) -> tuple[int, int]:
    """(private, working set) of a process in MiB, from /proc/<pid>/status. Linux has no counter that matches Windows'
    PrivateMemorySize64, so RssAnon (resident anonymous pages) stands in for private bytes; VmRSS is the working set."""
    fields = _mib_fields(proc_root / str(pid) / "status", ("RssAnon", "VmRSS"))
    return fields["RssAnon"], fields["VmRSS"]


def system_memory_linux(proc_root: Path = PROC) -> tuple[int, int, int]:
    """(available physical, committed, commit limit) in MiB, from /proc/meminfo."""
    fields = _mib_fields(proc_root / "meminfo", ("MemAvailable", "Committed_AS", "CommitLimit"))
    return fields["MemAvailable"], fields["Committed_AS"], fields["CommitLimit"]


def process_memory_mib(pid: int) -> tuple[int, int]:
    """(private bytes, working set) of a process in MiB."""
    if sys.platform.startswith("linux"):
        return process_memory_linux(pid)
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$p = Get-Process -Id {pid}; \"$($p.PrivateMemorySize64) $($p.WorkingSet64)\""],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return int(out[0]) // 2**20, int(out[1]) // 2**20


def system_memory() -> tuple[int, int, int]:
    """(available physical, committed, commit limit) in MiB."""
    if sys.platform.startswith("linux"):
        return system_memory_linux()
    return system_memory_mib()


def snapshot(pid: int | None) -> dict:
    """Memory and VRAM now. Process memory needs the server's pid; without one those fields are None."""
    private, working_set = process_memory_mib(pid) if pid is not None else (None, None)
    avail, commit, _ = system_memory()
    return {"vram_mib": gpu_memory_used_mib(), "server_private_mib": private, "server_ws_mib": working_set,
            "system_avail_mib": avail, "system_commit_mib": commit}


@contextmanager
def serving(args: argparse.Namespace, ctx: int, out_dir: Path) -> Iterator[tuple[str, int | None, float | None]]:
    """Yields (base URL, server pid, load seconds): a server started at this ctx, or the one --base-url names."""
    if args.base_url:
        yield args.base_url, args.server_pid, None
        return
    config = copy.deepcopy(load_config())
    config["ctx_size"] = ctx
    with LlamaServer(args.model, out_dir / f"server-{ctx}.log", config) as server:
        yield server.base_url, server.proc.pid, round(server.load_seconds or 0, 1)


def na(value):
    return "n/a" if value is None else value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.6-35b-a3b")
    parser.add_argument("--ctx", default="32768,65536,131072",
                        help="context sizes to start a server at; with --base-url, the running server's --ctx-size")
    parser.add_argument("--base-url", help="measure the llama-server already running at this URL instead of starting one")
    parser.add_argument("--server-pid", type=int, help="with --base-url: that server's pid, for its process memory")
    args = parser.parse_args(argv)
    ctxs = [int(c) for c in args.ctx.split(",")]
    if args.base_url and len(ctxs) != 1:
        parser.error("--base-url measures one running server: give its --ctx-size as one value")
    if args.server_pid and not args.base_url:
        parser.error("--server-pid goes with --base-url")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out_dir = RUNS / f"context-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True)
    results = []
    for ctx in ctxs:
        sizes = [s for s in (2000, 16000, 30000, 60000, 120000) if s < ctx - 3000]
        row: dict = {"ctx": ctx}
        try:
            with serving(args, ctx, out_dir) as (base_url, pid, load_seconds):
                row["load_seconds"] = load_seconds
                row["after_load"] = snapshot(pid)
                if load_seconds is not None:
                    print(f"[ctx {ctx}] loaded in {load_seconds}s", flush=True)
                print(f"[ctx {ctx}] after load: {row['after_load']}", flush=True)
                row["perf"] = perf.measure(base_url, args.model, sizes=sizes)
                for p in row["perf"]:
                    print(f"[ctx {ctx}] {p}", flush=True)
                row["after_longest"] = snapshot(pid)
                print(f"[ctx {ctx}] after longest prompt: {row['after_longest']}", flush=True)
        except Exception as e:  # e.g. the server can't fit this context at all
            row["error"] = str(e)
            print(f"[ctx {ctx}] error: {e}", flush=True)
        results.append(row)
        (out_dir / "context.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    lines = [f"# Context size test: {args.model} ({out_dir.name})", ""]
    if args.base_url:
        lines += [f"Measured the server already running at {args.base_url}; this run did not start one.", ""]
    lines += ["| ctx | Load s | VRAM MiB | Server private MiB | Server WS MiB (after longest) | System avail MiB (after longest) |",
              "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['ctx']} | error: {r['error'][:80]} | | | | |")
            continue
        a, b = r["after_load"], r["after_longest"]
        lines.append(f"| {r['ctx']} | {na(r['load_seconds'])} | {a['vram_mib']} | {na(a['server_private_mib'])} | "
                     f"{na(b['server_ws_mib'])} | {b['system_avail_mib']} |")
    lines += ["", "| ctx | Prompt tokens | Prompt s | Prompt tok/s | Gen tok/s |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        for p in r.get("perf", []):
            lines.append(f"| {r['ctx']} | {p['context_tokens']} | {p['prompt_seconds']} | {p['prompt_tps']} | {p['gen_tps']} |")
    (out_dir / "context.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report: {out_dir / 'context.md'}", flush=True)


if __name__ == "__main__":
    main()
