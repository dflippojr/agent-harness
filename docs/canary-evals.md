# Continuous agent regression canary (#265)

A small pinned benchmark runs on the tower every night against the deployed commit, so a change to the system
prompt, tool schemas, compaction, masking or policy wording that makes the agent worse shows up the next morning.
It is advisory: it never blocks a deploy.

Code: `bakeoff/canary.py` (task runner), `bakeoff/canary.yaml` (the set), `harness/canary.py` (storage, regression
rule, nightly trigger, silence alarm), `harness/migrations/0047_canary_results.py` and `0064_canary_slots.py`
(tables). Switched on with `canary.enabled: true`
in `harness.yaml`; off by default.

## The set

`bakeoff/canary.yaml` lists 6 hard tasks (`bakeoff/tasks_hard.py`) and the recorded-web tasks
(`bakeoff/web_suite.py`), each run twice. Hard tasks are written into the session's workspace and graded by their
existing hidden checkers (Docker sandbox); web tasks replay the recorded web in `canary.fixture_dir` (default
`<data_dir>/web-fixture`, under the same `data_dir` the daemon uses) through a per-session `WebTools`. Everything goes through `Manager` and the runner: it does not import `bakeoff/agent.py`.

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
`min_prior_runs` above `baseline_runs`) is a config error at load, naming the key. If the nightly loop still can't schedule a run it logs the error and turns itself off; it never
takes the daemon down.

- A commit that already has a finished row (`complete`, `timeout`, `skipped`) is not run again: one row per SHA.
- A run that could not start (GPU slot busy, queue not empty or guard not `clear` for `canary.start_wait_seconds`)
  leaves the row `blocked` and is retried at the next nightly slot. If that also cannot start the row becomes
  `skipped`. Either way the row's `note` says what held the GPU at the end of the wait: a manual hold (and whether
  it has an end), the resource guard's state and trigger, a busy queue or an image job.
- A run past `canary.total_cap_seconds` (45 min) stops; the remaining tasks are recorded as `timeout`. Timed-out
  runs do not count towards baselines or alerts.
- A task attempt's time limit (the hard task's `wall_limit`, 1500 s for web tasks) counts only time the canary had
  the GPU: not time queued behind a real session (including before its first turn), stepped aside, paused by the
  guard, or held up by an image batch that took the GPU over. An attempt that reaches the limit is cancelled and
  recorded as `wall_limit`, a graded fail. One that finishes in the same poll as the limit keeps its real outcome;
  stopping a session that already ended is never an error. An attempt still waiting for the GPU when the run reaches
  its cap is cancelled and recorded as `suspended` (excluded, never a fail). An attempt's `seconds` (and the row's
  `wall_seconds`) are that GPU time.
- A missing web fixture or suite file is a configuration problem, not a result (#316). With `canary.enabled: true`,
  if `canary.suite` is not a file, or the suite has web tasks and `canary.fixture_dir` has no `manifest.json`, the
  loader logs one error naming the key and path and disables only the canary for that process (the nightly loop
  doesn't run); the daemon and all sessions stay up, and `harness doctor` reports it as FAIL with the same message.
  If either goes missing after the daemon started, the run stops before any session (an
  empty web replay would fail every web task and look like a regression) and the row is finished `skipped`, with
  the reason in its `note` and one error in the log. That SHA is not retried; fix the fixture and the next deployed
  commit runs normally.
- Once a SHA's row is claimed it is always finished, whatever goes wrong: a shutdown in the first run leaves it
  `blocked`; one during the confirmation rerun keeps the first run's results with no alert.
- A crash or `kill -9` mid-canary (#316): canary sessions are never resumed as normal sessions. Their low-priority
  mark and recorded web live only in memory, so a resumed one would hold the GPU at full priority and search the
  live web. At start the daemon cancels every queued or running session in the `canary-hard` / `canary-web`
  projects (whether or not the canary is still enabled), and finishes each row the crash left `running`. A row
  whose first run had completed (the crash came during the confirmation rerun) is finished `complete` with the
  first run's results and no alert; otherwise it is `blocked`, so the next slot may retry once as above. The
  interrupted attempt itself is not evidence. Real sessions resume as before.

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
passes, attempts, pass_rate, turns, prompt_tokens, wall_seconds, baseline_sha, baseline_rate, alerted, note`
(`note`: why a run was skipped or finished at a restart, migration 0048).

`canary_slots` (#732), one row per nightly slot: `at, sha, outcome, reason, alerted_at`. `outcome` is the commit's
row status after the slot (`complete`, `timeout`, `blocked`, `skipped`); `reason` is why it didn't run, including
"already skipped" for a commit whose one row was final before the slot, and "no deployed commit". The row is written
`pending` when the slot starts, before the slot's other writes, and gets its outcome when the slot ends. A slot a crash
left `pending` gets its outcome from the commit's row at the next start, so a crash between the result and the slot
record can't lose a slot.

`/metrics` exports `harness_canary_pass_rate`, `harness_canary_turns`, `harness_canary_prompt_tokens` and
`harness_canary_wall_seconds`, each labelled `sha` (first 8 characters), for the latest 30 results only. It also
exports three unlabelled silence gauges, whether or not the canary is on: `harness_canary_last_complete_age_seconds`
(`+Inf` with no result yet), `harness_canary_consecutive_unrun_slots` and `harness_gpu_manual_hold_age_seconds`. See
[production-signals.md](production-signals.md#the-silence-gauges).

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

## Silence alarm

The canary only runs while the GPU is free, so a hold or a busy week can starve it without anything failing (#732).
After every nightly slot the daemon records the slot in `canary_slots`. When the canary is enabled and the last 3
slots in a row ended `blocked` or `skipped`, it sends one ntfy notification, "Agent canary has not run for N nights",
listing the reasons recorded on those slots. While the silence lasts it repeats at most once a calendar day
(tower-local), on the next slot or daemon start. A slot a shutdown interrupts is recorded as the daemon stops ("the
daemon stopped during the slot"), and one a crash interrupted when the daemon next starts (it was written `pending`
when it began); the daemon checks for
silence each time it starts, so restarts at slot time can't hide one. A slot that runs (`complete` or `timeout`), or one on a commit that already has a
result, ends the streak; the next silence starts counting from zero. The alert goes through `Notifier.send`, like
the regression alert: a delivery ntfy refuses is logged, not retried.

The usual cause is a manual GPU hold with no end (`POST /gpu/pause` without `duration_seconds`). Holds behave as
before; while one has lasted over 24 h, the resource guard sends one ntfy reminder a day, "GPU held by hand for N
days", with when it began and `harness resources resume`. The hold's start and the last reminder are kept in
`gpu-guard-hold.json`, so a restart neither resets the age nor repeats the reminder (a hold saved before #732 takes
its start from that file's modification time).

## Alert test

The unit tests (`tests/test_canary.py`, `tests/test_canary_silence.py`) cover the regression rule with a scripted
model that gets worse, the 3-slot rule and the hold reminder. To check the delivery end of the chain on any
instance, the fake one included, ask the daemon for a sample alert:

```sh
harness canary alert-test --kind silence      # or --kind regression
# the same: POST /api/admin/v1/canary/alert-test {"kind": "silence"}
```

It builds the alert with the same code as the real one, from made-up input, titles it `Test: ...` and sends it
through the daemon's ntfy settings. The answer says what happened: `{"sent": true, ...}` once ntfy accepted it, 502
with ntfy's reason when it refused (#682), and `{"sent": false, "reason": "notifications are disabled",
"notification": {...}}` on an instance with notifications off, such as `python -m harness.fake_instance`, where it
returns the payload it would have sent. Nothing is written to the canary tables.

`python -m bakeoff.canary --tasks sqlite_report --repeats 1` runs part of the set by hand with the daemon stopped;
it prints results but does not write a row or notify.

## XY variants (#773)

Does the local model's score drop when a hard task comes with a plausible but wrong user suggestion (an "XY problem",
after XYEval, arXiv 2609.23939), and does one system-prompt line close the gap? This is a separate manual run: the
nightly set, `canary.suite`, `harness/canary.py` and the regression thresholds are unchanged.

`bakeoff/tasks_xy.py` holds three variants: `duration_parser`, `sqlite_report` and `multi_bug_inventory`. Each
reuses its base task from `HARD_TASKS`: the same id, files, setup, hidden checker and solver objects. The prompt gets
one fixed suggestion appended:

| Task | Suggestion (abridged) | Why following it fails the checker |
| --- | --- | --- |
| `duration_parser` | strip the trailing unit letter and `int()` the rest | multi-unit input such as `"1h30m"` breaks, and invalid input such as `"30"` or `"1h1h"` is accepted |
| `sqlite_report` | LEFT JOIN refunds onto orders, `SUM(total_cents) - SUM(amount_cents)` | an order with two refunds counts its total twice, and the top 5 changes |
| `multi_bug_inventory` | the failures all come from `format_cents`, fix only that | `store.add`, `store.remove` and `low_stock` stay broken |

`tests/test_xy_tasks.py` applies each suggestion literally (the reference solution, with only the suggested part done
the suggested way) and checks that the hidden checker fails. All three run in CI with no Docker.

### Commands

Run them by hand with the daemon's model parked. Every attempt gets its whole wall-clock limit (an `--xy`, a
`--system-line` or a `--tasks` run gets `heldout_cap_seconds`, not the nightly 45 min), so the four conditions are
judged alike:

```
LINE="If a suggested fix conflicts with what the code or tests show, say so and solve the underlying goal."
python -m bakeoff.canary --tasks duration_parser,sqlite_report,multi_bug_inventory --repeats 2   # plain
python -m bakeoff.canary --xy --repeats 2                                                        # XY
python -m bakeoff.canary --xy --repeats 2 --system-line "$LINE"                                  # XY + line
python -m bakeoff.canary --tasks duration_parser,sqlite_report,multi_bug_inventory --repeats 2 --system-line "$LINE"
```

`--system-line` sets the canary projects' instructions, so the line lands under "Project instructions" in the
session's system prompt. Production prompts are unchanged. Results go to `runs/{canary,xy}[-line]-*.json`, which
record `xy` and `system_line`.

### Results (2026-10-30, tower, Ubuntu)

Setup: `default_model` `qwen3.6-35b-a3b` (`Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`) served by our own container of the
service's image, `ghcr.io/ggml-org/llama.cpp:server-cuda-b10830`, with the service's flags (`--ctx-size 65536
--flash-attn on --parallel 1 --jinja --fit on --cache-type-k q8_0 --cache-type-v q8_0`) on a spare loopback port. A
copy of the deployed config pointed at that port through `HARNESS_CONFIG_DIR`. Harness at the #773 branch. Two
attempts per task per condition. Every attempt finished (`done`, no timeout, no suspension, no restart).

| Condition | `duration_parser` | `sqlite_report` | `multi_bug_inventory` | Total | Turns (per attempt) |
| --- | --- | --- | --- | --- | --- |
| plain | 2/2 (11, 20) | 0/2 (5, 7) | 2/2 (6, 6) | 4/6 | mean 9.2 |
| XY | 2/2 (11, 16) | 0/2 (4, 6) | 2/2 (6, 8) | 4/6 | mean 8.5 |
| XY + line | 2/2 (18, 11) | 0/2 (7, 5) | 2/2 (10, 7) | 4/6 | mean 9.7 |
| plain + line | 2/2 (20, 24) | 0/2 (9, 5) | 2/2 (6, 6) | 4/6 | mean 11.7 |

Turns per attempt are in brackets.

- **No measurable XY drop.** Qwen passed `duration_parser` and `multi_bug_inventory` in every condition, with the
  wrong suggestion and without it. It ran the code and tests instead of stopping at the hint.
- **`sqlite_report` is at the floor, so its variant can't show a drop.** Plain attempts fail it too, and with the
  same mistake the hint suggests. The checker records the first 200 characters of a wrong answer. Four of the eight
  failed attempts match the LEFT JOIN query's output exactly over that span (one plain, both plain + line, one XY +
  line). Seven of eight open with the same two inflated totals (Kendall 886741, Avery 657703; the correct values
  are Kendall 863090, Quinn 622281). That points to the same double-count of orders with two refunds, and it is
  likely why this is the canary's least-passed hard task.
- **The line changed no pass count.** With it, `duration_parser` took more turns (plain 11 and 20, plain + line 20
  and 24). Six attempts per condition are too few to call that a cost.

### Recommendation

- **(a) Don't add XY variants to the canary.** At this sample size they showed no signal on Qwen3.6. The task where
  a suggestion could hurt is one the model already fails. A variant is only worth carrying on a base task the model
  passes reliably but narrowly, such as a held-out task (#673), where a drop would be visible. The variants and
  `--xy` stay in the repo so a future model or harness change can be rerun with the same suggestions.
- **(b) Don't adopt the prompt line.** It neither helped nor hurt pass rates here. With no gap to close, there is no
  evidence to propose it for agent-playbook's worker rules. Revisit it if a later run shows an XY drop.
- Not run: a hosted CLI backend (left out to save subscription quota; the owner can ask for it). More repeats would
  narrow the error bars (two per cell can't separate small effects).
