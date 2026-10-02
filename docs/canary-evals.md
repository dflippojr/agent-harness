# Continuous agent regression canary (#265)

A small pinned benchmark runs on the tower every night against the deployed commit, so a change to the system
prompt, tool schemas, compaction, masking or policy wording that makes the agent worse shows up the next morning.
It is advisory: it never blocks a deploy.

Code: `bakeoff/canary.py` (task runner), `bakeoff/canary.yaml` (the set), `harness/canary.py` (storage, regression
rule, nightly trigger), `harness/migrations/0047_canary_results.py` (table). Switched on with `canary.enabled: true`
in `harness.yaml`; off by default.

## The set

`bakeoff/canary.yaml` lists 6 hard tasks (`bakeoff/tasks_hard.py`) and the recorded-web tasks
(`bakeoff/web_suite.py`), each run twice. Hard tasks are written into the session's workspace and graded by their
existing hidden checkers (Docker sandbox); web tasks replay the recorded web in `canary.fixture_dir` through a
per-session `WebTools`. Everything goes through `Manager` and the runner: it does not import `bakeoff/agent.py`.

Selection, from `docs/phase0-results.md` only (no new GPU measurement): hard tasks Qwen3.6 passed in some but not all
recorded attempts, then the remainder by `max_turns`, which is 50 for every hard task, so ties were broken by
misses of the other models/harnesses in the same tables.

| Task | Evidence |
| --- | --- |
| `sqlite_report` | Qwen 1/2: the only hard task Qwen missed |
| `merge_conflict` | gpt-oss 1/2; git workflow, follow-up commits |
| `cli_json_output` | gpt-oss 1/2; the agent writes and runs its own tests |
| `log_correlation` | compaction failure under OpenCode at 32K: exercises compaction and masking |
| `duration_parser` | OpenHands miss (`"1hm"` accepted): edge-case discipline |
| `multi_bug_inventory` | three independent bugs: multi-step edits |

`web_suite.py` has only three tasks (`pdf_transformer`, `llama_sleep_endpoints`, `searxng_license`), not the four
the issue assumed, so the set is 9 tasks and 18 attempts. The thresholds below are in percentage points, so they
don't depend on that count. Add a task to `web_suite.TASKS` (and record its fixture) and to `canary.yaml` to grow it.

## Trigger

One run at **03:00 tower-local time** (`canary.at`) on the deployed commit (`HARNESS_BUILD_COMMIT`, else the
checkout's `HEAD`). There is no post-deploy trigger and no admin endpoint.

`canary.at` is a time of day, `"HH:MM"`. Quote it: YAML 1.1 reads an unquoted `3:05` as the number 185, which
the loader turns back into `"03:05"`. Anything else that isn't a time of day (or a canary number below its minimum,
`min_prior_runs` above `baseline_runs`, or an enabled canary whose `suite` file is missing) is a config error at
load, naming the key. If the nightly loop still can't schedule a run it logs the error and turns itself off; it never
takes the daemon down.

- A commit that already has a finished row (`complete`, `timeout`, `skipped`) is not run again: one row per SHA.
- A run that could not start (GPU slot busy, queue not empty or guard not `clear` for `canary.start_wait_seconds`)
  leaves the row `blocked` and is retried at the next nightly slot. If that also cannot start the row becomes
  `skipped`.
- A run past `canary.total_cap_seconds` (45 min) stops; the remaining tasks are recorded as `timeout`. Timed-out
  runs do not count towards baselines or alerts.
- A task attempt's time limit (the hard task's `wall_limit`, 1500 s for web tasks) counts only time the canary had
  the GPU: not time queued behind a real session (including before its first turn), stepped aside, paused by the
  guard, or held up by an image batch that took the GPU over. An attempt that reaches the limit is cancelled and
  recorded as `wall_limit`, a graded fail. One that finishes in the same poll as the limit keeps its real outcome;
  stopping a session that already ended is never an error. An attempt still waiting for the GPU when the run reaches
  its cap is cancelled and recorded as `suspended` (excluded, never a fail). An attempt's `seconds` (and the row's
  `wall_seconds`) are that GPU time.
- A run with web tasks refuses to start if `canary.fixture_dir` has no `manifest.json`: replaying an empty web
  would fail every web task and look like a regression. The row is left `blocked`.
- Once a SHA's row is claimed it is always finished, whatever goes wrong: a crash or shutdown in the first run
  leaves it `blocked`; one during the confirmation rerun keeps the first run's results with no alert.

## Yielding

The canary session is registered as low priority (`GpuScheduler.low_priority`).

- It starts only when the slot is free, nobody is queued and the guard is `clear`; checked before every task.
- Between turns (`Runner._gpu_gate`) it releases the slot and re-queues at the back whenever a real session is
  waiting, and the scheduler never grants the slot to a low-priority session while a real one is queued. A real
  session waits at most for the turn in flight.
- If the guard pauses (a game or Plex transcode) or a real session took over mid-task, that task attempt restarts
  from scratch once the GPU is free (at most 2 restarts per attempt); its result is never taken from a suspended
  attempt. An attempt still suspended after the 2 restarts is recorded as `suspended` (not a pass or a fail): it is
  left out of the pass rate, the per-task regression rule and the confirmation rerun, and counted as excluded. A
  run with no valid attempt gets no pass rate, no verdict and is not a baseline.

## Storage and metrics

`canary_results`, one row per SHA: `sha, started_at, finished_at, status, tries, outcomes (JSON per attempt),
passes, attempts, pass_rate, turns, prompt_tokens, wall_seconds, baseline_sha, baseline_rate, alerted`.

`/metrics` exports `harness_canary_pass_rate`, `harness_canary_turns`, `harness_canary_prompt_tokens` and
`harness_canary_wall_seconds`, each labelled `sha` (first 8 characters), for the latest 30 results only.

No Grafana dashboard JSON is checked in under `ops/`, so there is no panel to edit. PromQL for one:

```promql
harness_canary_pass_rate                                  # one series per recent commit
min(harness_canary_pass_rate)                             # worst of the latest 30
harness_canary_prompt_tokens                              # cost per commit, same labels
```

Because the label is the commit, plot it as a table or bar gauge sorted by time, not as a time series.

## Regression rule

Only finished, graded attempts are evidence: status `done`, `failed` or `wall_limit` (`harness.canary.valid`).
`timeout`, `suspended`, `blocked` and `cancelled` attempts are neither a pass nor a fail, in the first run and in the
confirmation rerun.

- Baseline: median pass rate of the previous 5 `complete` runs.
- No alert with fewer than 3 prior results.
- Alert when the new pass rate is at least 15 points below the baseline, **and** a confirmation rerun still leaves
  it that far below. The rerun covers only the tasks that failed this time and did better in the earlier runs (or, if
  there are none, such as a newly added task with no history, every task that failed this time); its results replace
  theirs in the row (marked `confirm`). An alert is never sent without a confirmation rerun, and the rerun counts
  only if it is `complete` and finished at least one attempt of every rerun task. Otherwise (it timed out, could not
  start, raised, or a task was only suspended or cancelled) the row keeps the first run's results, no alert is sent
  and the daemon logs why.
- One ntfy notification (through `Notifier.send`) with the SHA, baseline, new rate and
  `https://github.com/dflippojr/agent-harness/compare/<baseline_sha>...<new_sha>`. `baseline_sha` is the earlier run
  closest to the median.

## Manual check on staging: a degraded prompt must alert

The unit tests (`tests/test_canary.py`) cover the rule with a scripted model that gets worse. To see the whole chain
once on real hardware:

1. Seed history: let the nightly run on at least 3 earlier commits, or run `python -m bakeoff.canary` on staging
   after deploying 3 harmless commits.
2. On a branch, degrade `SYSTEM_PROMPT` in `harness/manager.py` (for example drop the tool-use guidance or tell the
   agent to answer without using tools). Do not merge it.
3. Dispatch `staging.yml` from `main` with that branch ref, which deploys it with `HARNESS_BUILD_COMMIT` set.
4. On staging set `canary.enabled: true` and `canary.at` to a minute a few minutes ahead (tower-local), restart the
   staging daemon, and keep the GPU free of games and other sessions.
5. After the run, expect a `canary_results` row for that SHA with a lower `pass_rate`, `/metrics` showing it, and one
   phone notification with the compare link. Reset staging afterwards (the `staging.yml` reset input) and set
   `canary.at` back.

`python -m bakeoff.canary --tasks sqlite_report --repeats 1` runs part of the set by hand with the daemon stopped;
it prints results but does not write a row or notify.
