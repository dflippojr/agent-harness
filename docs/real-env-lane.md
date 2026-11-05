# Real-environment lane (#731)

CI checks everything except the three paths that matter most: the Docker sandbox, the local model on the GPU and the
hosted CLI backends. GitHub's hosted runner has no sandbox image, no GPU and no CLI login. This repository is public,
so self-hosted Actions runners are never used. The lane runs those paths from a scheduled task on the deploy host
against `origin/main`, and reports on the commit it tested.

`scripts/real_env_lane.py` is the runner. `ops/real-env/` holds the wrapper script and the systemd user units.

## What a run does

1. **Takes the commit's files** with `git archive <sha>` into `runs/<id>/tree`. It makes no worktree and changes
   no checkout.
2. **Uses the commit's images.** These are the ones CI published, `ghcr.io/dflippojr/agent-harness-{sandbox,cli}:sha-<sha>`,
   used if they're on the host or can be pulled. For the sandbox image only, if neither works, the lane builds one
   from the tree as `real-env-lane-sandbox:<sha12>` and removes it after the run.
3. **Runs each suite.** Each session suite starts its own disposable daemon from that tree: a fresh data dir, a free
   loopback port and synthetic workspaces.
4. **Reports.** It posts a commit status per suite, files or updates an issue for each failure, and writes the
   summary.

| Suite | When | What it runs | Passes when |
| --- | --- | --- | --- |
| `sandbox` | Nightly | 1. `pytest -m docker` from the tree, with `HARNESS_TEST_SANDBOX_IMAGE` and `HARNESS_TEST_CLI_IMAGE` set to the commit's images and `HARNESS_LIVE_DOCKER=1`. <br>2. One scripted session in the real sandbox: `git_clone local:lane-fixture`, `run_shell` of the fixture, `verify` (the fixture's unittest), then three reads of a host file outside `/workspace` (a `../` path, its absolute host path, and a symlink committed in the fixture repo), and a `run_shell` `cat` of all three. | No marked test fails. The session ends `done`. The clone, the shell run and `verify` succeed. The host file's random contents appear nowhere in the session's events or transcript (#370 class). |
| `gpu` | Nightly | 1. A gate, which reads the live daemon's `/metrics`. It skips as `gpu-held: ...` unless the GPU guard is `clear`, there's no manual hold, the model isn't parked and RAM isn't low. <br>2. `pytest -m gpu` (`tests/test_real_model.py`: a text reply and a tool call through `harness.llm`). <br>3. One short session on the real llama-server that must call `write_file`. | The marked tests pass. The session ends `done` with a successful `write_file`. |
| `hosted-cli` | Weekly | 1. One minimal print-mode session ("reply with exactly `real-env-ok`") per configured backend that is available and logged in. <br>2. With `--pair` (the weekly unit passes it), Claude's session is replaced by two concurrent sessions sharing its one credential, followed by a login check (#390 class). | Every session ends `done` with `real-env-ok` in its transcript, and Claude is still logged in after the pair. |

The lane never approves anything. A session that asks for an approval is denied, and the denial is listed in the
summary.

### Skips are never passes

A suite can be skipped for one of these reasons:

- `gpu-held: <why>`: the GPU gate above.
- `busy: ...`: the live daemon still had sessions running or queued after `--yield-seconds` (default 30 min), so
  the lane yielded to real work.
- `weekly cap: ...`: a hosted run happened within the last 6.5 days. `hosted-ledger.json` records it.
- No hosted backend is logged in.

GitHub has no skipped state, so a skipped suite posts `success` with a description starting `skipped:`. The summary
keeps `"state": "skipped"`.

## Where the results go

- **Commit status `real-env/<suite>`** on the tested SHA:
  `gh api repos/dflippojr/agent-harness/commits/<sha>/statuses --jq '.[] | select(.context | startswith("real-env/"))'`.
  The states map as follows:
  - `passed` and `skipped` post `success`, and the description says which one it was.
  - `failed` posts `failure`.
  - `error` posts `error`. That means the lane itself couldn't run the suite, for example because Docker was missing.
- **An issue labelled `bug`** on a `failed` or `error` suite. It carries the SHA, the suite, the summary and the last
  60 log lines, with anything shaped like a credential redacted.
  - Each suite has at most one open issue, found by the hidden marker `<!-- real-env-lane suite=<suite> -->` in its body.
  - A later failure comments on that issue instead of opening another.
  - Closing the issue lets the next failure open a new one.
- **The summary**, at `~/.local/share/agent-harness-real-env/latest.json`, outside the live data dir. Each run's
  copy is at `runs/<id>/summary.json`. The summary holds:
  - `sha`, `run_id`, `started_at`, `finished_at` (UTC ISO 8601) and `ok`;
  - for each suite: `state`, `summary`, `status`, `description`, `seconds`, `details`, a redacted `excerpt` on
    failure, `posted` (the status post's outcome) and `issue`.

  `docs/production-signals.md` points the box's sweep here. A `finished_at` more than 26 h old means the nightly
  task has stopped.

A failed status post or issue call doesn't stop the run. The summary records it under `posted` or `issue`, and the
next run tries again.

## What it never touches

The lane shares the tower with the live instance. It reads exactly two things from it: `/metrics`, for the GPU guard
and the busy check, and the config files under `~/.local/share/agent-harness/config`, for the local model's address
and the hosted `backends:`.

The disposable daemon:

- Runs with **maintenance off.** Maintenance removes session containers its own database doesn't know about, and on
  the shared Docker engine those belong to the live instance.
- Runs **without the local-model supervision module**, as fake model mode does. So nothing in it can stop, park or
  start llama-server, and it never writes the live pause flag.
- Has its own Docker networks, `real-env-<run>-{sbx,egress,proxy}`.
- Has a sandbox proxy that doesn't exist, so a network command fails closed instead of going through the live
  egress proxy.

When the run ends, the lane removes its own session containers by name (`harness-<its session id>`) and its own
networks. It never sweeps by label or pattern.

The hosted suite does use the real credentials, as the owner's own sessions do. It mounts the backend's
configured login and state volumes, so each run adds one tiny conversation to that history. It stays inside the
weekly cap.

Python: the lane runs from its own clone and venv (`run-lane.sh`), never the deployed checkout or its venv.

pytest: at most 4 workers (`--workers`, default 2). The units run at the lowest CPU and I/O priority.

## Scheduling (the owner enables it)

Nothing installs or enables these units; that is a decision for the owner. On the deploy host:

1. Make sure `gh` is logged in as an account that may post statuses and open issues on dflippojr/agent-harness. The
   owner's own `gh auth login` is enough. The workers' GitHub App currently gets HTTP 403 on statuses: it has no
   `statuses: write` permission.
2. Install and enable:

   ```bash
   base=~/.local/share/agent-harness-real-env
   mkdir -p "$base" ~/.config/systemd/user
   cp ops/real-env/run-lane.sh "$base/"
   cp ops/real-env/agent-harness-real-env{,-hosted}.{service,timer} ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now agent-harness-real-env.timer agent-harness-real-env-hosted.timer
   ```

   The installed `run-lane.sh` hands over to the copy on `origin/main` after each fetch, so later changes to it
   arrive without reinstalling.
3. Check: `systemctl --user list-timers 'agent-harness-real-env*'`. After a run, read
   `journalctl --user -u agent-harness-real-env` and `latest.json`.

The nightly timer fires at 04:30, after the canary's 03:00 slot. The weekly hosted timer fires on Sunday at 05:30.

## Running it by hand

Before merging a risky change (sandbox, policy, GPU guard, CLI backends), run the lane against the PR's head SHA:

```bash
git fetch origin pull/<PR>/head
python scripts/real_env_lane.py run --sha <PR head SHA> --suite sandbox --suite gpu
```

- **No report.** `--no-report` writes the summary and posts nothing.
- **Images.** CI publishes images only for `main` commits. For a PR SHA, the lane builds the sandbox image from the
  tree. Pass `--sandbox-image` and `--cli-image` to reuse an image instead when the PR leaves `sandbox/` alone, for
  example main's `ghcr.io/dflippojr/agent-harness-sandbox:sha-<main sha>`.
- **Hosted suite.** `--suite hosted-cli` respects the weekly cap. `--force-hosted` ignores it, so use it sparingly:
  each backend costs one session.
- **Disk.** `--base` puts runs somewhere other than `~/.local/share/agent-harness-real-env`. Each run keeps its
  `runs/<id>/instance-*/` directory (the disposable daemon's log, database and session workspaces) for a post-mortem.
  `--keep` also keeps the extracted tree and the pytest temp dirs. Only the newest 14 run directories are kept
  (`--keep-runs`); `latest.json` is never pruned.

**Self-test of the reporting path.** `--broken-fixture` commits a failing test in the fixture repo, so `verify`
fails and the sandbox suite reports `failed`. Run it twice against the same SHA. You get one issue, plus one comment
on it from the second run. Close that issue afterwards, and run once without the flag to put the status back to
`success`.

## Tests

- `tests/test_real_env_lane.py` fakes each suite's result and checks the status, the issue and the summary outputs.
  It also covers de-duplication, redaction, the GPU gate, yielding to live sessions, the weekly cap, the disposable
  instance's config and the session checks.
- `tests/test_real_model.py` (`gpu`) and the `docker`-marked tests skip in CI. They are the tests the lane runs for
  real.
