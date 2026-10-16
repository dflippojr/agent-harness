---
name: verify-agent-harness
description: Prove a change works in the running app, not only in unit tests. Launches a disposable fake-model Agent Harness Server with Stables on synthetic data, runs doctor checks, drives the smoke journey (and one feature) in a browser on desktop and Pixel 7, saves screenshots, logs and a summary, and stops everything it started. Use for "verify this change", "launch and check", "does it work in the app", "smoke test" in agent-harness.
---

# Verify agent-harness

One command runs every step and always cleans up:

```
python .claude/skills/verify/scripts/verify.py run [--feature <name> ...] [--keep]
```

It prints `PASS`/`FAIL` per step and the path of `summary.md`, and exits 0 only if every step passed. The smoke
journey always runs; `--feature` adds a journey from the feature map. Paste the summary into the PR's `## Proof`
table. Never point anything here at the live harness, `:8100` or a `*.ts.net` URL.

**Before the first run**, set up what [docs/ui-testing.md](../../../docs/ui-testing.md) "Scope and isolation" →
**Tools** and "Playwright and CI" → **Commands** list. Chromium is the only browser this needs:

```
python -m pip install -r requirements.txt
npm --prefix tools/ui-tests ci
npm --prefix tools/ui-tests exec -- playwright install chromium
```

Then point `HARNESS_STABLES_DIR` at the `public/` directory of a dflippojr/stables checkout at the tag in
`tools/stables/pin.json`, or clone that checkout into this one as `stables/` (git-ignored). The commands are the same in
PowerShell, Git Bash and macOS shells. Without either, the launch step fails at once and names what is missing.

## 1. Launch

`verify.py` runs the contract's **Start** command, `python -m harness.fake_instance --dir <new temp dir>`, as its own
background process (docs/ui-testing.md, "Start"). The instance runs the real Server with the scripted model on
`127.0.0.1` ports, and the seed is synthetic. It gets this environment minus every credential-named variable
(tokens, keys, passwords) and `HARNESS_FAKE_MODEL`. Server logs go to `logs/server.log` in the evidence folder, so
they survive Teardown.

The start command prints the instance's owner and local tokens. `verify.py` keeps them in memory for the doctor and
writes `[REDACTED]` in their place in `launcher.txt`.

Before waiting for anything it writes `launch.json`: the instance directory and every process it starts, with start
times. All of them are also contained:

- **Windows:** one kill-on-close Job Object, so they die even if `verify.py` itself is killed.
- **Linux:** `verify.py` is a child subreaper.
- **POSIX:** every step carries the run's random `VERIFY_RUN_ID`, which finds a process that left the tree.

## 2. Doctor

```
python .claude/skills/verify/scripts/doctor.py --dir <instance-dir>
```

`verify.py` runs it in-process after READY and saves its output as `doctor.txt`. One line per check, exit 1 on any
failure:

| Check | Passes when |
| --- | --- |
| `ready` | `instance.json` says `ready`, the Server's `/health` answers 200 with `ok`, and Web's `/ready` answers 200 |
| `loopback` | both URLs are `http://127.0.0.1:<port>` and not `:8100`, and every socket the recorded process listens on is bound to `127.0.0.1`, including both ports |
| `isolation` | the `data/fake-model-instance` marker is present, the generated config's `data_dir` and `repos_dir` are inside the instance directory (which is not this checkout or its `config/`), and the instance process carries no credential variables |
| `seed` | the six seeded sessions (each with its settled status), the chat and both jobs from docs/ui-testing.md "Seed and sign-in" are listed, read with the owner token |

The doctor only reads. It contacts nothing unless both URLs are safe, and it reads the seed only once the other three
checks pass. Run on its own, it takes the owner token from `HARNESS_UI_OWNER_TOKEN`.

## 3. Drive

Playwright, Chromium only, one worker, a desktop (1440×900) and a phone (Pixel 7) project, with
[`tools/ui-tests/verify/drive.spec.ts`](../../../tools/ui-tests/verify/drive.spec.ts) and
[`tools/ui-tests/verify.config.ts`](../../../tools/ui-tests/verify.config.ts). It reuses that package's pinned
Playwright and the suite's `createTask` and `expectDone` helpers. The main suite's config does not pick it up. The
config refuses any `VERIFY_WEB_URL` that is not a loopback port. Output: `drive.txt`.

### Feature map

Every journey signs in through `<web>/connect` (docs/ui-testing.md, "Seed and sign-in") and fails on page errors and
console errors. Fake mode's expected 404 probes for dropped modules are the one exception.

| Feature | Runs | What it does | What proves it |
| --- | --- | --- | --- |
| `smoke` | always | Signs in, checks the tab bar, opens Agents (AH-01/AH-02) | `#conn` is Live, the tab bar has Chat, Agents, Jobs, Images and Profile, and Needs you shows the Protected settings approval row and Recent; `<project>-chat.png`, `<project>-agents.png` |
| `task` | `--feature task` | Starts a new task in project `ui` with "Show me some markdown.", then reloads (AH-06) | The session ends done with the "Summary" heading and keeps its title after reload; `<project>-task.png` |

To cover something the map lacks, add a tagged test (`@<name>`) to `drive.spec.ts`, its name to `FEATURES` in
`verify.py` and a row here. Journeys beyond these belong in `tools/ui-tests/tests`.

## 4. Evidence

Under `.verify/<UTC timestamp>-<random suffix>/` (git-ignored). `--evidence <empty dir>` overrides it, and a used
folder is refused:

- `summary.md`: the result of each step, the head commit (and whether the tree had uncommitted changes), the
  journeys, the loopback URLs, the doctor's lines, the screenshot list and log excerpts. The excerpts are the fake
  model banner and any `ERROR`, `Traceback` or `FAILED` lines. The seeded Failed run always logs one `crashed`
  traceback, and the summary marks it as expected. Read the summary first and quote it in the PR.
- `screenshots/<desktop|pixel7>-<name>.png`.
- `doctor.txt`, `drive.txt`, `launcher.txt`, `cleanup.txt`, `launch.json` and `logs/server.log`, plus Playwright
  traces under `playwright/` when a test failed.

Before `verify.py` exits it redacts every text file and every trace zip in the folder. It replaces the instance's
two tokens, any `ho-…` owner token and any `Bearer …` value (docs/ui-testing.md, **Secrets**).

## 5. Cleanup

Cleanup runs in a `finally`, so it also runs after a failed launch, doctor or drive:

1. The contract's **Teardown**: `python -m harness.fake_instance stop --dir <instance-dir>` (docs/ui-testing.md,
   "Teardown").
2. It kills each process still in the run's containment, then each process in `launch.json` that still runs with
   its recorded start time. Nothing is found or killed by name or port. If the instance directory is still there,
   Teardown runs once more.
3. The leftover checks: no recorded or contained process alive, both ports refuse connections, and the instance
   directory is gone. Any leftover fails the step and is listed in `cleanup.txt`.

A run passes only if launch, doctor, drive and cleanup all ran and passed. Ctrl+C or an error is recorded as a failed
`interrupted` step, and cleanup and the summary still run. Ctrl+C is ignored while they run; every wait in them has a
timeout.

`--keep` is for a look by hand. After the drive it prints the instance's `/connect` URL and leaves the instance up
until Ctrl+C, then cleans up as usual. Don't use it in an unattended run.

If a run was killed before its own cleanup, repeat the cleanup from the evidence folder. On Windows its processes
die with it, but the instance directory stays.

```
python .claude/skills/verify/scripts/verify.py cleanup --evidence .verify/<run>
```

## Tests

`tests/test_verify_skill.py` covers:

- argument parsing
- the doctor's verdicts: healthy; not ready; unsafe URLs never contacted; non-loopback listeners; data outside the
  instance; credentials; missing or unsettled seed; no token
- token redaction in `launcher.txt`, logs and trace zips
- full `run`s with a stand-in instance whose drive or doctor step fails, or whose launch fails, checking that cleanup
  still leaves no process, port or directory behind
- interrupted runs, `--keep`, and unique instance and evidence folders
