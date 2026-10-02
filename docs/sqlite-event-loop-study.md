# SQLite blocking of the daemon event loop (#257)

Phase 1 (measure) of the question "does synchronous SQLite work stall the asyncio loop enough to need a refactor?".
Decision rule (owner, 2026-10-01): refactor if the p99 event-loop stall under the benchmark load exceeds **50 ms**.

## Verdict

**p99 stall exceeds 50 ms in both runs** (about 1 s with `/metrics` scraping, about 100 ms without it), so a refactor
is warranted. Follow-up issue #294 carries these numbers and the Phase 2 design. No refactor lands with #257.

Two distinct causes show up:

1. **`/metrics` holds the DB lock for 1-2.5 s per scrape** (`harness.metrics._core_metrics` runs several
   `json_extract` aggregates over the whole `events` table inside `with db.lock:`). `render` runs in a worker thread,
   but any loop-thread `Database` call waits for that lock, so the loop freezes for the duration. Cost grows with the
   events table and is the dominant stall.
2. **Event inserts and FTS searches on the loop** hold the lock up to 25 ms at p99 (large ~1 MB tool results and
   `search_events` over a growing FTS index). Without scraping, p99 stall is still about 100 ms because several
   sessions' calls queue back to back on the one loop thread.

## How it is measured

Permanent instrumentation (always on, cheap) on `GET /metrics`:

- `harness_db_lock_held_seconds{method}`: histogram of how long each outermost `with db.lock:` block held the lock,
  labelled by the calling method (`harness/telemetry.py` `TimedLock`).
- `harness_event_loop_stall_seconds`: histogram of how much later than scheduled a 10 ms probe sleep wakes up
  (`LoopLagProbe`, started in the app lifespan).
- `HARNESS_ASYNCIO_DEBUG=1`: opt-in asyncio debug with `slow_callback_duration = 0.05`, so slow callbacks are logged.

Benchmark: `python scripts/bench_event_loop.py [--sessions 4] [--seconds 30] [--large-every 10] [--no-scrape] [--markdown out.md]`.
Four fake sessions write events on the loop (every 10th is a ~1 MB tool result), a searcher runs `search.search` and
`session_search` every 50 ms on the loop, and a scraper renders `/metrics` in a thread every 250 ms. Quantiles are
histogram bucket upper bounds, so "10 ms" means the 5-10 ms bucket. The loop-lag p50 is inflated because the loop is
saturated by synchronous work, which is the point of the test. The benchmark database grows to hundreds of MB within
the run, larger than a typical daemon database, so absolute values are an upper-end estimate; the relative picture
(`/metrics` dominating) does not depend on that. Numbers vary run to run (two runs of the scraped config gave p99
1 s and 2.5 s).

## Results: sessions + search + `/metrics` scrapes

```
@@WITH@@
```

## Results: sessions + search, no scraping (attribution run)

```
@@WITHOUT@@
```

## Phase 2 design (for the follow-up issue)

Single writer thread draining a queue over one connection (an awaited write returns only after commit, which keeps
commit-before-act ordering); reads from a small pool of read-only WAL connections via `asyncio.to_thread`. The
`/metrics` aggregates should move to the read pool first, since they are the largest stall.
