"""Benchmark how long synchronous SQLite work blocks the daemon's event loop (#257).

Runs in one process on one asyncio loop against a temp database, the way the daemon does: fake sessions call
`Database.insert_event` directly on the loop (some with ~1 MB tool results), a searcher calls `session_search`
and `search.search` on the loop, and a scraper renders `/metrics` in a worker thread (as `api.metrics` does).
The always-on loop-lag probe and the per-method lock-hold histograms from `harness.telemetry` do the measuring.

    python scripts/bench_event_loop.py [--sessions 4] [--seconds 20] [--large-every 10] [--markdown out.md]

Quantiles come from the histogram buckets, so they are bucket upper bounds (">2.5s" past the last bucket).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import config as config_mod  # noqa: E402
from harness import metrics, search, telemetry  # noqa: E402
from harness.config import Config, ModelConfig, Project, SandboxConfig  # noqa: E402
from harness.db import Database  # noqa: E402
from harness.manager import Manager  # noqa: E402

WORDS = ("alpha beta gamma delta parser cache router widget session budget compaction approval tunnel sandbox "
         "token queue lambda vector export import reader writer shell patch commit branch review failure").split()
LARGE_BYTES = 1_000_000
STALL_THRESHOLD = 0.05


def _text(n_words: int, salt: int) -> str:
    return " ".join(WORDS[(salt * 7 + i * 13) % len(WORDS)] + str((salt + i) % 97) for i in range(n_words))


def _session(sid: str, workspace: str) -> dict:
    now = time.time()
    return {"id": sid, "project": "scratch", "target": "local", "model": "fake", "title": f"bench {sid}",
            "status": "running", "workspace": workspace, "created_at": now, "updated_at": now, "context": "[]"}


async def fake_session(db: Database, sid: str, stop: asyncio.Event, large_every: int, stats: dict, workspace: str) -> None:
    db.insert_session(_session(sid, workspace))
    blob = ("x" * 63 + "\n") * (LARGE_BYTES // 64)
    i = 0
    while not stop.is_set():
        i += 1
        db.insert_event(sid, "assistant", {"text": _text(60, i), "tool_calls": []})
        if i % large_every == 0:
            db.insert_event(sid, "tool_result", {"name": "bash", "ok": True, "output": blob})
            stats["large"] += 1
        else:
            db.insert_event(sid, "tool_result", {"name": "bash", "ok": True, "output": _text(300, i)})
        stats["events"] += 1
        await asyncio.sleep(0.005)  # model latency stand-in; the loop is otherwise free


async def searcher(db: Database, stop: asyncio.Event, stats: dict) -> None:
    tool = search.SessionSearch(db)
    queries = ["parser cache", "compaction approval", "tunnel sandbox failure", "alpha1 beta2"]
    i = 0
    while not stop.is_set():
        q = queries[i % len(queries)]
        i += 1
        search.search(db, q, limit=10)
        tool.session_search(q, limit=5)
        stats["searches"] += 1
        await asyncio.sleep(0.05)


async def scraper(m: Manager, stop: asyncio.Event, stats: dict) -> None:
    while not stop.is_set():
        await asyncio.to_thread(metrics.render, m)
        stats["scrapes"] += 1
        await asyncio.sleep(0.25)


def make_manager(tmp: Path) -> Manager:
    cfg = Config(host="127.0.0.1", port=0, data_dir=tmp / "data", repos_dir=tmp / "repos", default_model="fake",
                 models={"fake": ModelConfig(name="fake", base_url="http://unused", context_tokens=65536)},
                 sandbox=SandboxConfig(image="unused", network=f"bench-{uuid.uuid4().hex[:6]}",
                                       egress_network=f"bench-egress-{uuid.uuid4().hex[:6]}"),
                 projects={"scratch": Project(name="scratch")})
    return Manager(cfg)


def fmt(seconds: float) -> str:
    return ">2.5 s" if seconds == float("inf") else f"{seconds * 1000:g} ms"


async def run(args) -> dict:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        m = make_manager(Path(tmpdir))
        db = m.db
        telemetry.loop_stall.__init__(telemetry.LOOP_BUCKETS)  # drop warm-up samples
        telemetry.lock_held._by_label.clear()
        stats = {"events": 0, "large": 0, "searches": 0, "scrapes": 0}
        stop = asyncio.Event()
        probe = telemetry.LoopLagProbe()
        probe.start()
        workspace = str(Path(tmpdir) / "ws")
        tasks: set[asyncio.Task] = set()

        def spawn(coro) -> None:
            task = asyncio.create_task(coro)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        for n in range(args.sessions):
            spawn(fake_session(db, f"bench-{n}", stop, args.large_every, stats, workspace))
        spawn(searcher(db, stop, stats))
        if not args.no_scrape:
            spawn(scraper(m, stop, stats))
        await asyncio.sleep(args.seconds)
        stop.set()
        await asyncio.gather(*tasks)
        await probe.stop()
        db_file = Path(db.conn.execute("PRAGMA database_list").fetchone()["file"])
        db_bytes = sum(p.stat().st_size for p in db_file.parent.glob(db_file.name + "*"))
        db.close()
    methods = [(name, h.snapshot()[2], h.quantile(0.5), h.quantile(0.99), h.snapshot()[1])
               for name, h in telemetry.lock_held.items()]
    methods.sort(key=lambda r: -r[4])
    return {"stats": stats, "stall": telemetry.loop_stall, "methods": methods, "db_bytes": db_bytes}


def machine() -> str:
    return (f"{platform.system()} {platform.release()} ({platform.machine()}), {os.cpu_count()} logical CPUs, "
            f"Python {platform.python_version()}, SQLite {sqlite3.sqlite_version}")


def report(args, result: dict) -> str:
    stall = result["stall"]
    p50, p99, worst = stall.quantile(0.5), stall.quantile(0.99), stall.quantile(1.0)
    exceeded = p99 > STALL_THRESHOLD
    s = result["stats"]
    lines = [
        f"Machine: {machine()}",
        f"Config: {args.sessions} concurrent sessions, {args.seconds:g} s, one ~1 MB tool result every "
        f"{args.large_every} events per session, search every 50 ms, /metrics render every 250 ms{' (disabled)' if args.no_scrape else ''}",
        f"Work done: {s['events']} event pairs ({s['large']} large), {s['searches']} search rounds, "
        f"{s['scrapes']} scrapes, database {result['db_bytes'] / 1e6:.0f} MB",
        "",
        "Event-loop stall (10 ms probe overshoot, bucket upper bounds)",
        f"- samples: {stall.snapshot()[2]}, p50: {fmt(p50)}, p99: {fmt(p99)}, max bucket: {fmt(worst)}",
        f"- p99 {'EXCEEDS' if exceeded else 'is within'} the {STALL_THRESHOLD * 1000:g} ms threshold",
        "",
        "Lock hold per Database method (sorted by total time held)",
        "| method | count | p50 | p99 | total (s) |",
        "|---|---|---|---|---|",
    ]
    for name, count, q50, q99, total in result["methods"][:15]:
        lines.append(f"| {name} | {count} | {fmt(q50)} | {fmt(q99)} | {total:.2f} |")
    return "\n".join(lines)


def safe_output_path(raw: str) -> Path:
    """Resolve `raw` and require it to sit inside the repo root or the system temp dir."""
    path = Path(raw).resolve()
    for base in (ROOT, Path(tempfile.gettempdir()).resolve()):
        if path.is_relative_to(base):
            return path
    raise SystemExit(f"--markdown must be inside the repo ({ROOT}) or the temp dir; got {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--large-every", type=int, default=10, help="every Nth event per session is a ~1 MB tool result")
    ap.add_argument("--no-scrape", action="store_true", help="skip the /metrics scraper (attribution run)")
    ap.add_argument("--markdown", help="also write the report to this file")
    args = ap.parse_args()
    md_path = safe_output_path(args.markdown) if args.markdown else None
    out = report(args, asyncio.run(run(args)))
    print(out)
    if md_path:
        md_path.write_text(out + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
