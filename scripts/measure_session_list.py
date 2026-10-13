"""Measure the database part of building the session list's cards (#667).

Seeds a throwaway database with 50 sessions (about 131 KB of context each, two user messages, and tool results mixing
short, medium and long outputs) and times, per call of `GET /sessions`, the reads the cards used to make
(`get_session` + `events` + `last_event_seq` per row) against the ones they make now (`session_card_bits` +
`last_event_seq`). Prints the median and range of 5 runs in milliseconds and the event JSON the old reads decoded.
Run from the repository root: python scripts/measure_session_list.py [events per session, default 300]
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.db import Database

SESSIONS = 50
RUNS = 5


def seed(db: Database, events: int) -> list[str]:
    context = [{"role": "user", "content": "x" * 1300} for _ in range(100)]  # ~131 KB of JSON
    outputs = ["ok", "line of output\n" * 40, "long line of tool output\n" * 200]
    sids = []
    for i in range(SESSIONS):
        sid = f"s{i:02d}"
        now = time.time()
        db.insert_session({"id": sid, "project": "scratch", "target": "tower", "model": "fake", "title": sid,
                           "status": "done", "workspace": "", "created_at": now, "updated_at": now,
                           "context": context, "owner_id": "owner", "answer": "Finished the change. " * 20})
        db.insert_event(sid, "user_message", {"content": "Make dark mode black"})
        for n in range(events - 2):
            db.insert_event(sid, "tool_result", {"id": f"c{n}", "name": "run_shell", "output": outputs[n % 3]})
        db.insert_event(sid, "user_message", {"content": "Also move the profile control"})
        sids.append(sid)
    return sids


def before(db: Database, sids: list[str]) -> None:
    for sid in sids:
        db.get_session(sid)
        db.events(sid)
        db.last_event_seq(sid)


def decoded_bytes(db: Database, sids: list[str]) -> int:
    return sum(len(json.dumps(e["data"])) for sid in sids for e in db.events(sid))


def after(db: Database, sids: list[str]) -> None:
    for sid in sids:
        db.session_card_bits(sid)
        db.last_event_seq(sid)


def timed(fn) -> str:
    runs = []
    for _ in range(RUNS):
        start = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - start) * 1000)
    return f"{statistics.median(runs):.0f} ms ({min(runs):.0f}-{max(runs):.0f})"


def main() -> None:
    events = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "bench.db")
        try:
            sids = seed(db, events)
            decoded = decoded_bytes(db, sids)
            print("| events per session | before | after | event JSON decoded before |")
            print("| ---: | ---: | ---: | ---: |")
            print(f"| {events:,} | {timed(lambda: before(db, sids))} | {timed(lambda: after(db, sids))} "
                  f"| {decoded / 1e6:.1f} MB |")
        finally:
            db.close()


if __name__ == "__main__":
    main()
