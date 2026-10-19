# Production signals an agent can fetch (#732)

Each signal the tower's production daemon gives off, with one read-only command to fetch it and how fresh it is
expected to be. An agent sweeping the box (dflippojr/agent-ops#52) reads this page first; nothing here changes
state. A signal older than its freshness is itself a finding: the thing that should refresh it has stopped.

Production runs on the tower: bound to `127.0.0.1:8100`, data in `D:\Agents\harness`, logs in
`D:\Agents\harness\logs` (`docs/CI-CD.md`, Locations). `/health` and `/metrics` answer a caller on the tower itself
without a token (`harness/local_owner.py`); everything else needs the owner identity, so it isn't listed here.
Commands are PowerShell on the tower unless they say otherwise.

| Signal | Fetch (read-only) | Expected freshness |
| --- | --- | --- |
| Liveness | `Invoke-RestMethod http://127.0.0.1:8100/health` | Live: computed per request. No answer means the daemon is down or restarting (the supervisor restarts it within about 10 s). |
| Deployed commit | `(Invoke-RestMethod http://127.0.0.1:8100/health).build.commit` | Changes at each deploy (`ops/harness/deploy-commit.ps1` passes it as `HARNESS_BUILD_COMMIT`). Compare with `git rev-parse origin/main`: a lag of more than one deploy run means deploys have stopped. Empty when the daemon was started by hand, without the supervisor. |
| Metrics | `Invoke-RestMethod http://127.0.0.1:8100/metrics` | Live per scrape. Session and event counters are cached for 10 s; the gauges below are read on every scrape. |
| Canary silence | ``(Invoke-RestMethod http://127.0.0.1:8100/metrics) -split "`n" \| Select-String 'harness_canary_last_complete_age_seconds\|harness_canary_consecutive_unrun_slots\|harness_gpu_manual_hold_age_seconds'`` | See [The silence gauges](#the-silence-gauges). |
| Canary results | The query under [Canary tables](#canary-tables) | One `canary_slots` row a night, written when the slot at `canary.at` (03:00 tower-local) finishes: usually within an hour of it (up to 10 min waiting for the GPU, a 45 min run cap, then any confirmation rerun). The newest row older than 26 h means the nightly loop isn't running (canary off, or the daemon down at 03:00). |
| Real-environment lane (#731) | `gh api repos/dflippojr/agent-harness/commits/<sha>/statuses --jq '.[] \| select(.context \| startswith("real-env/")) \| [.context, .state, .updated_at] \| @tsv'` (from any machine with `gh`), and `Get-Content D:\Agents\real-env\latest.json` on the tower | Nightly against `origin/main` once #731 lands: a status `real-env/<suite>` on that night's main HEAD, and `latest.json` rewritten after each run. Until #731 merges, neither exists. |
| Daemon log | `Get-Content D:\Agents\harness\logs\daemon.log -Tail 200` | Written continuously while the daemon runs; at 20 MB it moves to `daemon.log.prev`. `supervisor.log` next to it records each start and exit. |
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

Read-only, with the harness virtual environment's Python (the daemon keeps writing while this reads). The
script goes in a single-quoted here-string, which PowerShell passes on unchanged, so the SQL quotes survive:

```powershell
@'
import sqlite3
db = sqlite3.connect("file:D:/Agents/harness/harness.sqlite3?mode=ro", uri=True)
for row in db.execute("SELECT datetime(at, 'unixepoch', 'localtime'), substr(sha, 1, 8), outcome, reason "
                      "FROM canary_slots ORDER BY id DESC LIMIT 7"):
    print(row)
for row in db.execute("SELECT substr(sha, 1, 8), status, pass_rate, datetime(finished_at, 'unixepoch', 'localtime'), "
                      "note FROM canary_results ORDER BY started_at DESC LIMIT 7"):
    print(row)
'@ | & D:\Projects\agent-harness\.venv\Scripts\python.exe -
```

If the checkout's virtual environment lives elsewhere, `.venv-path` in the checkout names it. `canary_slots` has one
row per nightly slot: `outcome` is `complete`, `timeout`, `blocked` or `skipped`, and `reason` says why it didn't
run (a manual hold, the resource guard and its trigger, a busy queue, a missing fixture, a commit already skipped).
`canary_results` has one row per commit ([canary-evals.md](canary-evals.md#storage-and-metrics)).

## Real-environment lane

#731 adds a nightly lane that runs the Docker sandbox, local model and hosted CLI suites against `origin/main`. Its
results are to be read from two places, and #731 writes them there:

- **Commit statuses** `real-env/<suite>` on the tested SHA, one per suite. A skipped suite (the GPU held, say)
  must read as skipped in its description, never as a plain pass.
- **`D:\Agents\real-env\latest.json`** on the tower, outside the live data directory: the last run's summary,
  with at least the tested `sha`, `finished_at` and each suite's state.

Freshness: one run a night. A `latest.json` whose `finished_at` is more than 26 h old means the lane's scheduled
task has stopped.

## Daemon log

`D:\Agents\harness\logs\daemon.log`, one line per record as `<time> <LEVEL> <logger>: <message>`, followed by any
traceback. Error scan:

```powershell
Select-String -Path D:\Agents\harness\logs\daemon.log -Pattern ' (ERROR|CRITICAL) |^Traceback' | Select-Object -Last 50
```

On Unix installs the log is `~/.local/share/agent-harness/logs/daemon.log` ([INSTALL.md](INSTALL.md)); scan it
with `grep -nE ' (ERROR|CRITICAL) |^Traceback' ~/.local/share/agent-harness/logs/daemon.log | tail -n 50`.
