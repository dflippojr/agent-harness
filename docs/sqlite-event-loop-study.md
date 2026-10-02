# SQLite blocking of the daemon event loop (#257, #294)

Phase 1 (measure, #257) of the question "does synchronous SQLite work stall the asyncio loop enough to need a refactor?".
Decision rule (owner, 2026-10-01): refactor if the p99 event-loop stall under the benchmark load exceeds **50 ms**.

## Verdict

**p99 stall exceeds 50 ms in both runs** (about 1 s with `/metrics` scraping, about 100 ms without it), so a refactor
is warranted. Follow-up issue #294 carries these numbers and the Phase 2 design. No refactor lands with #257.

**Phase 2 (#294) brings p99 under 50 ms in both runs** (0.5 ms with scraping, 10 ms without), while doing 6-9x the
work; see [Phase 2 results](#phase-2-results-294).

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
  labelled by the calling method (`harness/telemetry.py` `TimedLock`). Since #294 each connection (the writer and
  every pooled reader) has its own lock, so this is the time a method kept its connection busy, not time others
  waited; a `db.write(fn)` transaction is labelled by `fn`'s name.
- `harness_event_loop_stall_seconds`: histogram of how much later than scheduled a 10 ms probe sleep wakes up
  (`LoopLagProbe`, started in the app lifespan).
- `HARNESS_ASYNCIO_DEBUG=1`: opt-in asyncio debug with `slow_callback_duration = 0.05`, so slow callbacks are logged.

Benchmark: `python scripts/bench_event_loop.py [--sessions 4] [--seconds 30] [--large-every 10] [--no-scrape] > out.md` (the report goes to stdout).
Four fake sessions write events on the loop (every 10th is a ~1 MB tool result), a searcher runs `search.search` and
`session_search` every 50 ms on the loop, and a scraper renders `/metrics` in a thread every 250 ms. Quantiles are
histogram bucket upper bounds, so "10 ms" means the 5-10 ms bucket. The loop-lag p50 is inflated because the loop is
saturated by synchronous work, which is the point of the test. The benchmark database grows to hundreds of MB within
the run, larger than a typical daemon database, so absolute values are an upper-end estimate; the relative picture
(`/metrics` dominating) does not depend on that. Numbers vary run to run (two runs of the scraped config gave p99
1 s and 2.5 s).

## Results: sessions + search + `/metrics` scrapes

```
Machine: Windows 10 (AMD64), 28 logical CPUs, Python 3.10.11, SQLite 3.40.1
Config: 4 concurrent sessions, 30 s, one ~1 MB tool result every 10 events per session, search every 50 ms, /metrics render every 250 ms
Work done: 2165 event pairs (216 large), 169 search rounds, 36 scrapes, database 243 MB

Event-loop stall (10 ms probe overshoot, bucket upper bounds)
- samples: 560, p50: 10 ms, p99: 1000 ms, max bucket: 2500 ms
- p99 EXCEEDS the 50 ms threshold

Lock hold per Database method (sorted by total time held)
| method | count | p50 | p99 | total (s) |
|---|---|---|---|---|
| _core_metrics | 36 | 1000 ms | 2500 ms | 21.55 |
| insert_event | 4330 | 0.5 ms | 25 ms | 3.07 |
| search_events | 844 | 0.5 ms | 10 ms | 0.76 |
| session_brief | 336 | 0.5 ms | 0.5 ms | 0.00 |
| _skill_metrics | 36 | 0.5 ms | 0.5 ms | 0.00 |
| _smart_review_metrics | 36 | 0.5 ms | 0.5 ms | 0.00 |
| _endpoint_metrics | 36 | 0.5 ms | 0.5 ms | 0.00 |
| insert_session | 4 | 0.5 ms | 0.5 ms | 0.00 |
```

## Results: sessions + search, no scraping (attribution run)

```
Machine: Windows 10 (AMD64), 28 logical CPUs, Python 3.10.11, SQLite 3.40.1
Config: 4 concurrent sessions, 30 s, one ~1 MB tool result every 10 events per session, search every 50 ms, /metrics render every 250 ms (disabled)
Work done: 6072 event pairs (604 large), 456 search rounds, 0 scrapes, database 676 MB

Event-loop stall (10 ms probe overshoot, bucket upper bounds)
- samples: 1521, p50: 10 ms, p99: 100 ms, max bucket: 250 ms
- p99 EXCEEDS the 50 ms threshold

Lock hold per Database method (sorted by total time held)
| method | count | p50 | p99 | total (s) |
|---|---|---|---|---|
| insert_event | 12144 | 0.5 ms | 25 ms | 7.99 |
| search_events | 2280 | 0.5 ms | 25 ms | 5.48 |
| session_brief | 912 | 0.5 ms | 0.5 ms | 0.01 |
| insert_session | 4 | 0.5 ms | 0.5 ms | 0.00 |
```

## Phase 2 design (for the follow-up issue)

Single writer thread draining a queue over one connection (an awaited write returns only after commit, which keeps
commit-before-act ordering); reads from a small pool of read-only WAL connections via `asyncio.to_thread`. The
`/metrics` aggregates should move to the read pool first, since they are the largest stall.

## Phase 2 results (#294)

What landed (owner decisions of 2026-10-02, one PR):

- `harness/db.py`: one writer thread owns the read-write connection and runs queued jobs in order; reads use up to
  `READ_CONNECTIONS` (4) read-only WAL connections (`PRAGMA query_only`). Every `Database` method is marked `@_writes`
  or `@_reads`, so the sync API still works from any thread (a write blocks its caller until committed). Event-loop
  code awaits `db.aio.<method>(...)`, `db.awrite(fn)` or `db.aread(fn)`.
- `Database.tx()` is gone: each former `with db.tx():` block is a callable run whole on the writer thread by
  `db.write(fn)` / `await db.awrite(fn)`, so it stays atomic and cannot await. An event emitted inside one is
  published to subscribers only after the commit, in the caller's thread (`db.after_commit`).
- The runner's per-turn writes (`_commit_completion`, `_record_result`, status changes, events emitted from async
  code) are awaited, so the loop keeps running while the writer commits. The end of a run commits the job status,
  the quote check and `run_finished` in one transaction.
- `/metrics` renders on a pooled read connection, and the heavy session/event aggregates (`_core_rows`) are cached
  for `CORE_CACHE_SECONDS` (10 s), so frequent scrapes don't repeat them. No counters were added to write paths.

The benchmark now drives the same paths as the daemon: sessions await `db.aio.insert_event` (as `EventBus.aemit`
does), and the searcher runs `search.search` and the `session_search` tool in worker threads (as `api.search` and
`SessionSearch.call` already did; in Phase 1 it called them on the loop). With the loop no longer blocked, the fake
sessions write much faster (the 5 ms "model latency" sleep is the only throttle), so the after runs push 6-9x more
events and a database several GB in size, a harsher load than the before runs.

Before (`origin/main` at 8ee0aae, re-run 2026-10-02 with the Phase 1 benchmark) and after, 30 s each, same machine:

| run | event pairs | DB size | loop-stall p50 | p99 | max bucket |
|---|---|---|---|---|---|
| before, with scraping | 2684 | 302 MB | 10 ms | **1000 ms** | 2500 ms |
| after, with scraping | 23395 | 4066 MB | 0.5 ms | **0.5 ms** | 25 ms |
| before, no scraping | 6580 | 733 MB | 10 ms | **100 ms** | 100 ms |
| after, no scraping | 17519 | 1974 MB | 0.5 ms | **10 ms** | 25 ms |
| after + atomic writes, with scraping | 17885 | 2893 MB | 0.5 ms | **5 ms** | 25 ms |
| after + atomic writes, no scraping | 23236 | 2611 MB | 0.5 ms | **0.5 ms** | 25 ms |
| after + cancel-safe `awrite`, with scraping | 22744 | 3729 MB | 0.5 ms | **0.5 ms** | 25 ms |
| after + cancel-safe `awrite`, no scraping | 22949 | 2579 MB | 0.5 ms | **0.5 ms** | 25 ms |
| after + cancel-safe follow-ups, with scraping | 23043 | 3785 MB | 0.5 ms | **0.5 ms** | 25 ms |
| after + cancel-safe follow-ups, no scraping | 21763 | 2453 MB | 0.5 ms | **0.5 ms** | 25 ms |

The atomic-writes rows are a re-run (30 s each, same machine) after every `@_writes` method became one transaction on the
writer (`BEGIN IMMEDIATE ... COMMIT`), so that a pooled reader can no longer see an event without its search-index row
or an allowlist mid-save. p99 stays well under 50 ms; the 0.5 / 5 / 10 ms differences between runs are run-to-run
noise (one histogram bucket), and `insert_event` still holds the writer about 26 of the 30 s. The cancel-safe rows
are a further re-run after `awrite` began running its after-commit callbacks from the writer job's completion (so a
cancelled awaiter cannot drop them) and holding a cancel until its write has committed: no measurable change. The
cancel-safe follow-up rows are a re-run after the work a commit requires (a follow-up's spawn, a run's end under any
cancel, the `end_pending` marker on `run`) was tied to its commit: no measurable change either. The full outputs below
are from the first after runs.

After, with scraping:

```
Machine: Windows 10 (AMD64), 28 logical CPUs, Python 3.10.11, SQLite 3.40.1
Config: 4 concurrent sessions, 30 s, one ~1 MB tool result every 10 events per session, search every 50 ms, /metrics render every 250 ms
Work done: 23395 event pairs (2336 large), 376 search rounds, 67 scrapes, database 4066 MB

Event-loop stall (10 ms probe overshoot, bucket upper bounds)
- samples: 47496, p50: 0.5 ms, p99: 0.5 ms, max bucket: 25 ms
- p99 is within the 50 ms threshold

Lock hold per Database method (sorted by total time held)
| method | count | p50 | p99 | total (s) |
|---|---|---|---|---|
| insert_event | 46790 | 0.5 ms | 10 ms | 26.04 |
| _core_rows | 3 | >2.5 s | >2.5 s | 13.34 |
| search_events | 1880 | 0.5 ms | 100 ms | 12.71 |
| session_brief | 752 | 0.5 ms | 0.5 ms | 0.05 |
| _smart_review_metrics | 67 | 0.5 ms | 5 ms | 0.02 |
| _endpoint_metrics | 67 | 0.5 ms | 5 ms | 0.01 |
| _skill_metrics | 67 | 0.5 ms | 0.5 ms | 0.00 |
| insert_session | 4 | 0.5 ms | 0.5 ms | 0.00 |
```

After, no scraping:

```
Machine: Windows 10 (AMD64), 28 logical CPUs, Python 3.10.11, SQLite 3.40.1
Config: 4 concurrent sessions, 30 s, one ~1 MB tool result every 10 events per session, search every 50 ms, /metrics render every 250 ms (disabled)
Work done: 17519 event pairs (1751 large), 400 search rounds, 0 scrapes, database 1974 MB

Event-loop stall (10 ms probe overshoot, bucket upper bounds)
- samples: 36037, p50: 0.5 ms, p99: 10 ms, max bucket: 25 ms
- p99 is within the 50 ms threshold

Lock hold per Database method (sorted by total time held)
| method | count | p50 | p99 | total (s) |
|---|---|---|---|---|
| insert_event | 35038 | 0.5 ms | 25 ms | 27.20 |
| search_events | 2000 | 0.5 ms | 50 ms | 11.44 |
| session_brief | 800 | 0.5 ms | 0.5 ms | 0.04 |
| insert_session | 4 | 0.5 ms | 0.5 ms | 0.00 |
```

Reading the after numbers:

- `_core_rows` ran 3 times in 67 scrapes (the 10 s cache) and each run took several seconds over a 4 GB database, but on
  a reader connection: it no longer delays a single write or the loop.
- The writer is the bottleneck now (`insert_event` holds its connection about 26 of the 30 s), which is what throttles
  the fake sessions. The loop stays free while they wait.
- The remaining no-scrape p99 of 10 ms is most likely GIL contention: the writer thread's `json.dumps` and search-text
  extraction for 1 MB results are Python code that holds the GIL while the loop wants to run. This was not measured
  separately; the loop no longer waits on SQLite itself.
