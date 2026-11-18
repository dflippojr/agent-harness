# Production signals an agent can fetch (#732)

Each signal the tower's production daemon gives off, with one read-only command to fetch it and how fresh it is
expected to be. An agent sweeping the box (dflippojr/agent-ops#52) reads this page first; nothing here changes
state. A signal older than its freshness is itself a finding: the thing that should refresh it has stopped.

Production runs on the tower, which runs Ubuntu since 2026-10-26 (`docs/CI-CD.md`, Tower runner): bound to
`127.0.0.1:8100`, data in `~/.local/share/agent-harness/data`, logs in `~/.local/share/agent-harness/logs` (the
`agent-harness-main-daemon.service` user unit's `ExecStart`). `/health` and `/metrics` answer a caller on the tower
itself without a token (`harness/local_owner.py`); everything else needs the owner identity, so it isn't listed here.
Commands are bash on the tower unless they say otherwise. On a Windows boot of the tower the same files are under
`D:\Agents\harness`.

| Signal | Fetch (read-only) | Expected freshness |
| --- | --- | --- |
| Liveness | `curl -s http://127.0.0.1:8100/health` | Live: computed per request. No answer means the daemon is down or restarting (the supervisor restarts it within about 10 s). |
| Deployed commit | `curl -s http://127.0.0.1:8100/health \| jq -r .build.commit` | Changes at each deploy (`install/run-daemon.sh` sets `HARNESS_BUILD_COMMIT` from the deployed environment's name in `.venv-path`; `ops/harness/deploy-commit.ps1` on Windows). Compare with `git rev-parse origin/main`: a lag of more than one deploy run means deploys have stopped. Empty when the daemon was started by hand, without the supervisor. |
| Metrics | `curl -s http://127.0.0.1:8100/metrics` | Live per scrape. Session and event counters are cached for 10 s; the gauges below are read on every scrape. |
| Canary silence | `curl -s http://127.0.0.1:8100/metrics \| grep -E '^harness_(canary_last_complete_age_seconds\|canary_consecutive_unrun_slots\|gpu_manual_hold_age_seconds)'` | See [The silence gauges](#the-silence-gauges). |
| Canary results | The query under [Canary tables](#canary-tables) | One `canary_slots` row a night, written when the slot at `canary.at` (03:00 tower-local) finishes: usually within an hour of it (up to 10 min waiting for the GPU, a 45 min run cap, then any confirmation rerun). The newest row older than 26 h means the nightly loop isn't running (canary off, or the daemon down at 03:00). |
| Real-environment lane (#731) | `gh api repos/dflippojr/agent-harness/commits/<sha>/statuses --jq '.[] \| select(.context \| startswith("real-env/")) \| [.context, .state, .description, .updated_at] \| @tsv'` (from any machine with `gh`), and `jq '{sha, finished_at, ok, suites: (.suites \| map_values(.state))}' ~/.local/share/agent-harness-real-env/latest.json` on the tower | Nightly against `origin/main` once the owner enables the lane's timers ([real-env-lane.md](real-env-lane.md#scheduling-the-owner-enables-it)): a status `real-env/<suite>` on that night's main HEAD, and `latest.json` rewritten after each run. The `hosted-cli` suite runs weekly. Until the timers are enabled, neither exists. |
| Daemon log | `tail -n 200 ~/.local/share/agent-harness/logs/daemon.log` | Written continuously while the daemon runs; at 20 MB it moves to `daemon.log.prev`. `daemon-supervisor.log` next to it records each start and exit (`supervisor.log` on Windows). |
| Daemon errors | The error scan under [Daemon log](#daemon-log) | Live. Any line is worth a look; a traceback repeated at every slot or poll is a defect. |

## The silence gauges

Read on every scrape of `/metrics` (`harness/metrics.py`, `_silence_metrics`), whether or not the canary is on:

| Gauge | Meaning | Healthy |
| --- | --- | --- |
| `harness_canary_last_complete_age_seconds` | Seconds since the newest real canary result finished: a `complete` row with a pass rate. `+Inf` when there has never been one. | Under about 2 days while commits are being deployed. It also grows while nothing new is deployed (a commit already run isn't run again), so read it next to the unrun count. |
| `harness_canary_consecutive_unrun_slots` | Nightly slots in a row that ended `blocked` or `skipped`, newest first, back to the last slot that ran. A slot on a commit that already has a result counts as run. | 0, or 1 to 2 for a night or two of games. At 3 the daemon sends the silence alarm. |
| `harness_gpu_manual_hold_age_seconds` | Seconds the manual GPU hold (`POST /gpu/pause`, `harness resources pause`) has lasted; 0 with no hold. The hold's start survives restarts (`gpu-guard-hold.json`). | 0, or short. An indefinite hold over a day old sends a daily reminder. |

Example from a fake instance (`python -m harness.fake_instance`, which has no canary runs and no GPU guard):

```text
# HELP harness_canary_last_complete_age_seconds Seconds since the newest complete canary result (+Inf when there is none).
# TYPE harness_canary_last_complete_age_seconds gauge
harness_canary_last_complete_age_seconds +Inf
# HELP harness_canary_consecutive_unrun_slots Nightly canary slots in a row that ended blocked or skipped.
# TYPE harness_canary_consecutive_unrun_slots gauge
harness_canary_consecutive_unrun_slots 0
# HELP harness_gpu_manual_hold_age_seconds Seconds the manual GPU hold has lasted (0 when none).
# TYPE harness_gpu_manual_hold_age_seconds gauge
harness_gpu_manual_hold_age_seconds 0
```

The daemon also pushes two of these as ntfy notifications, so the owner hears about them without a sweep
([canary-evals.md](canary-evals.md#silence-alarm)): the canary silence alarm and the indefinite-hold reminder.

## Canary tables

Read-only (the daemon keeps writing while this reads). The script needs only the standard library, so the
system `python3` runs it; the quoted heredoc passes the SQL quotes through unchanged:

```bash
python3 - <<'EOF'
import os, sqlite3
db = sqlite3.connect("file:" + os.path.expanduser("~/.local/share/agent-harness/data/harness.sqlite3") + "?mode=ro", uri=True)
for row in db.execute("SELECT datetime(at, 'unixepoch', 'localtime'), substr(sha, 1, 8), outcome, reason "
                      "FROM canary_slots ORDER BY id DESC LIMIT 7"):
    print(row)
for row in db.execute("SELECT substr(sha, 1, 8), status, pass_rate, datetime(finished_at, 'unixepoch', 'localtime'), "
                      "note FROM canary_results ORDER BY started_at DESC LIMIT 7"):
    print(row)
EOF
```

On a Windows boot the database is `D:/Agents/harness/harness.sqlite3`. `canary_slots` has one
row per nightly slot: `outcome` is `complete`, `timeout`, `blocked` or `skipped` (`pending` while the slot runs, or
until the next start after a crash), and `reason` says why it didn't run (a manual hold, the resource guard and its
trigger, a busy queue, a missing fixture, a commit already skipped).
`canary_results` has one row per commit ([canary-evals.md](canary-evals.md#storage-and-metrics)).

## Real-environment lane

[real-env-lane.md](real-env-lane.md) runs the Docker sandbox, local model and hosted CLI suites against
`origin/main` from a scheduled task on the tower. It writes its results to two places:

- **Commit statuses** `real-env/<suite>` (`sandbox`, `gpu`, `hosted-cli`) on the tested SHA, one per suite. A
  skipped suite (the GPU held, the live daemon busy, the weekly hosted cap) posts `success` with a description that
  starts `skipped:`, never a plain pass. A failure also opens a `bug` issue, or comments on the open one for that
  suite.
- **`latest.json`** at `~/.local/share/agent-harness-real-env/latest.json`, outside the live data directory: the
  last run's summary. It has the tested `sha`, `run_id`, `started_at` and `finished_at` (UTC ISO 8601), `ok`, and
  under `suites` each suite's `state` (`passed`, `failed`, `skipped`, `error`), `summary` and `description`. Each
  run's own copy is `runs/<run_id>/summary.json` next to it.

Freshness: one run a night. A `latest.json` whose `finished_at` is more than 26 h old means the lane's scheduled
task has stopped (`systemctl --user list-timers 'agent-harness-real-env*'`). A suite that reads `skipped` night
after night (`gpu-held`, `busy`) is a finding too: the real path isn't being judged.

## Daemon log

`~/.local/share/agent-harness/logs/daemon.log` on the tower and other Unix installs ([INSTALL.md](INSTALL.md)),
one line per record as `<time> <LEVEL> <logger>: <message>`, followed by any traceback. Error scan:

```bash
grep -nE ' (ERROR|CRITICAL) |^Traceback' ~/.local/share/agent-harness/logs/daemon.log | tail -n 50
```

On a Windows boot the log is `D:\Agents\harness\logs\daemon.log`:
`Select-String -Path D:\Agents\harness\logs\daemon.log -Pattern ' (ERROR|CRITICAL) |^Traceback' | Select-Object -Last 50`.

## Sweep configuration

`ao signals` (dflippojr/agent-ops, `scripts/signal_sweep.py`) reads the block below from this page on `main`, in place
of the seed kept in agent-ops. The sweep expands `~` in a fetch `path` for the user running it, and the paths are the
Unix install's defaults (`install/install.sh`), so the block names no account. It doesn't follow `XDG_DATA_HOME` or
`--data-dir`, and it doesn't cover a Windows boot of the tower: there, or on an install with its data elsewhere,
`canary` and `daemon-errors` report a fetch error.
`tests/test_production_signals_doc.py` checks the paths against the installer's defaults.

```yaml production-signals
signals:
  - id: deployed-sha
    kind: deployed_sha
    description: Commit the deployed daemon runs (HARNESS_BUILD_COMMIT).
    fetch: {type: http, url: "http://127.0.0.1:8100/health", field: build.commit}
  - id: health
    kind: health
    description: The deployed daemon answers /health with ok true.
    fetch: {type: http, url: "http://127.0.0.1:8100/health", field: ok}
    expect: {equals: true}
  - id: canary
    kind: freshness
    description: Last complete nightly canary result
    max_age: 48h
    hint: "Rows that stay blocked usually mean the GPU slot never came free; check harness_gpu_manual_hold_age_seconds on /metrics for a manual hold."
    fetch:
      type: sqlite
      path: ~/.local/share/agent-harness/data/harness.sqlite3
      query: "SELECT max(finished_at) FROM canary_results WHERE status = 'complete'"
  - id: backup
    kind: freshness
    description: Last successful daily backup
    max_age: 2d
    fetch: {type: prometheus, url: "http://127.0.0.1:8100/metrics", metric: harness_backup_last_success_timestamp_seconds}
  - id: daemon-errors
    kind: errors
    description: ERROR and CRITICAL records in the deployed daemon's log.
    fetch: {type: log, path: ~/.local/share/agent-harness/logs/daemon.log, window: 24h}
    public: {loggers: [asyncio]}
```
