# UI testing contract

Contract version: 1

This contract covers testing Agent Harness Web against a disposable Agent Harness Server that runs in **fake model
mode** (#588). Every local model reply comes from a fixture script instead of llama-server. Everything else is the
real server: the runner, policy and approvals, the file tools, compaction, jobs, events and streams. The agent-ops
`ui-testing-pass` skill reads this file. The commands and the safety boundary are here; the fixture format is in the
docstring of [`harness/fake_model.py`](../harness/fake_model.py).

## Scope and isolation

- **Target.** Agent Harness Web (`harness/web`, native JavaScript, no build step) served against Agent Harness Server
  (`harness/`) from this repository, `dflippojr/agent-harness`.
- **Loopback only.** Both listeners bind `127.0.0.1` and nothing else:
  - the Server at `http://127.0.0.1:<port>`
  - a separately hosted copy of Web at `http://127.0.0.1:<web-port>`

  There is no Tailscale serve, no public URL and no `allowed_logins`. Never point a test at `:8100`, at a
  `*.ts.net` URL, or at any other running harness.
- **Disposable data.** Each instance writes only inside its instance directory (`--dir`). The SQLite databases,
  workspaces, checkpoints, transcripts, generated config and the downloaded scanner all live under
  `<dir>/config`, `<dir>/data` and `<dir>/repos`. The real `config/` and data directory are never read.
- **How isolation is enforced.**
  - Fake mode turns on only through `HARNESS_FAKE_MODEL`, and the daemon refuses that variable unless `data_dir`
    holds the `fake-model-instance` marker the start command writes.
  - A `fake_model:` key in any YAML file stops the daemon from starting.
  - Fake mode removes the local-model supervision, inference-endpoint and image modules, so there is no warm-up, no
    GPU guard and no llama-server call. The manual GPU hold has no effect on it.
  - The instance never touches the Docker engine that a real harness on the machine shares:
    - The shell sandbox is off, so `run_shell`, `git_clone` and `verify` return the tool error "the shell sandbox is
      off in fake model mode". No container, network or cache volume is created.
    - The maintenance sweep (scheduled, or Settings → Disk → Cleanup) neither lists nor removes session containers.
    - Only Chat's **Run code** still uses Docker: its own short-lived, mount-less snippet containers, and only if the
      pinned images are present. Otherwise it reports an error.
  - `tests/test_fake_model.py` and `tests/test_fake_instance.py` cover these guards.
- **Disabled or stubbed integrations.**
  - Model: scripted (stubbed).
  - Off: hosted Claude, Codex and Cursor backends (not configured), notifications (ntfy), backup, homelab, memory
    library, Claude Remote Control, Hub, web search and fetch (SearXNG), images and image edit, inference endpoint,
    GPU guard, canary, telemetry export, GitHub tasks (no token) and Google sign-in.
  - Skills are off, because their validator runs Docker. Jobs and session search stay on because they are local.
  - The one outbound request: on first start the Server downloads its pinned secret scanner (gitleaks) from GitHub
    releases into `<dir>/data/tools`. If that download fails, the Server still starts; only push and merge review,
    which no journey uses, then refuse.
- **Tools.**
  - Python 3.10 or later (CI uses 3.12) with `python -m pip install -r requirements.txt`.
  - Node.js 20 or later and npm, only for the Playwright suite.
  - Windows (PowerShell or Git Bash), macOS or Linux shells all work.
  - No GPU, Docker, Tailscale or llama-server is needed.
- **Secrets.** None are needed. No model or provider API keys, GitHub token, Tailscale login, ntfy token or Google
  client secret. The instance creates its own owner token and local owner token at start. They are valid only for
  that instance and die with it. Keep them out of screenshots, reports and commits.

## Start

Run this from the repository root. It is the same command in PowerShell and Git Bash:

```
python -m harness.fake_instance --dir <instance-dir> [--port N] [--web-port N] [--fixtures PATH] [--no-seed] [--keep]
```

For example:

- PowerShell: `python -m harness.fake_instance --dir "$env:TEMP\harness-ui-pass"`
- Git Bash: `python -m harness.fake_instance --dir "$TEMP/harness-ui-pass"`

What the command does:

- **Fresh instance only.** `--dir` must be absent or empty, or the command exits 2 with `not empty`. Pass a
  directory you own so Teardown can find it. Without `--dir` it uses a new temp directory and prints its path.
- **Ports.**
  - `--port` and `--web-port` default to free ports.
  - An explicitly given port that is in use exits 2 with `--port N is in use; this command never reuses a running
    server`, before anything is created.
  - Every instance has its own process, directory, owner token and key id, so it never attaches to another server.
- **Environment.**
  - The command sets `HARNESS_FAKE_MODEL` for its own process. Don't set it yourself.
  - `--fixtures` swaps the fixture set (default `harness/fake_fixtures`). The fixtures are validated before anything
    is created, and a problem is named and exits 1.
- **Readiness.** The command seeds the data below through the real API and waits for every seeded run to settle (about
  10 s; it fails after 120 s). Only then does it:
  - print the summary block, ending with a single `READY {...}` JSON line and `Ctrl+C to stop.`
  - write `<dir>/instance.json` with `"status": "ready"`
  - answer `GET http://127.0.0.1:<web-port>/ready` with 200 and the same JSON. It answers 503 until then.

  The two readiness signals to wait on are the `READY` line on stdout or `/ready` returning 200.
- **URL output.** stdout looks like this:

  ```
  Agent Harness fake model instance is up (scripted model, no GPU).
    Web (signed in):  http://127.0.0.1:52011/connect
    Server:           http://127.0.0.1:52010   (bundled Web there needs the local token header)
    Owner token:      ho-...
    Local token:      ...   (header X-Agent-Harness-Local-Token)
    Fixtures:         .../harness/fake_fixtures
    Instance dir:     ...\harness-ui-pass   (deleted on exit)
    Process:          12345   (stop: python -m harness.fake_instance stop --dir "...")
  READY {"server": "http://127.0.0.1:52010", "web": "http://127.0.0.1:52011", "connect": "http://127.0.0.1:52011/connect", "dir": "...", "pid": 12345, "key_id": "k-1a2b3c4d"}
  Ctrl+C to stop.
  ```

- **Instance handle.** `<dir>/instance.json` records `pid`, `pid_created`, `server`, `web`, `connect`, `dir`, `fixtures`,
  `key_id` and `status` (`starting` or `ready`). It holds no tokens. Teardown uses it.
- **Logs.** Server logs go to stderr. They start with a `FAKE MODEL MODE` warning banner, and each model reply logs
  `fake model: script <name> turn <n>`. Redirect stderr to a file outside the instance directory, for example
  `2> output/ui-pass/agent-harness/<date>/server.log`, so it survives teardown.
- **Partial failure.**
  - A failed start (port taken, seeding or settling failed, or a server that didn't start) prints `FAILED <reason>`
    on stderr, stops both servers, deletes the instance directory unless `--keep`, and exits 1.
  - If the process is killed instead, the directory stays. Run Teardown, which works after a partial start.
- **The bundled copy.** Web is also served by the Server itself, at the Server URL. It needs the local token header
  (see Seed and sign-in). Journeys use the separately hosted copy at `<web>` unless they say otherwise.

## Seed and sign-in

**Synthetic fixtures.** Unless you pass `--no-seed`, every start creates the same data through the real API as the
owner. Runs queue one at a time, in this order:

| Seed | Kind | Status when READY | Content |
| --- | --- | --- | --- |
| Tab bar tour | session in project `ui` | `done` | Answer starting "Hi! This is the fake model." |
| Tool rows | session | `done` | `write_file` notes/todo.md and notes/log.txt (40 lines); `read_file` notes/log.txt; `list_files .`; `search fox`; `read_file notes/missing.md` (error); a masked-output compaction row; answer starting "Done. The tool rows above" |
| Retry after a model error | session | `done` | One retry row with "HTTP 500: simulated model server error"; answer starting "The model server failed once" |
| Failed run | session | `failed` | Error "HTTP 400: simulated fatal model error (fake model mode)" |
| Long task with compaction | session | `done` | Three long replies, two compaction summaries; answer starting "Done. The transcript shows the compaction rows" |
| Protected settings | session | `waiting_approval` | A pending `write_file protected/settings.json` approval, reason "protected file (fake-model UI testing)" |
| "Hello from Chat. What can you do?" | chat | `done` | The tab-bar-tour answer |
| Nightly report | job, `0 3 * * *`, enabled | – | Prompt "Write the nightly report.", project `ui` |
| Weekly cleanup | job, `0 4 * * 1`, disabled | – | The same prompt |

Session and job IDs are random. Find seeded rows by title or name. The recent list is bounded; after many mutations, use the search field and the exact seed title to find older sessions rather than expecting them in Recent. Read-only deep-link cases may reuse the IDs found at the start of that disposable instance.

The `ui` project's policy asks before any `write_file` or `edit_file` under `protected/`. A `scratch` project exists
too.

Each message picks a script by a case-insensitive phrase. That works for a new task's prompt, a follow-up in a session
and a chat message:

| Phrase | Script | Result |
| --- | --- | --- |
| "hello", "tab bar" | `tab-bar-tour` | Streamed answer with reasoning |
| "tool rows", "tool calls" | `tool-rows` | The Tool rows sequence above |
| "two approvals" | `approval-batch` | Two approvals in one turn (a.json and b.json under `protected/`) |
| "protected" | `approval-sheet` | One approval, then "Finished. The tool row shows whether the write was approved or denied." |
| "slow", "running" | `slow-running` | Six turns 8 s apart (about 50 s running), then "Done with the slow task. It wrote three files under slow/." |
| "retry" | `model-retry` | Retry row, then the answer |
| "failure", "fail" | `model-failure` | Fatal error, session `failed` |
| "compaction" | `compaction` | Long replies and compaction rows |
| "markdown" | `markdown-answer` | "## Summary", a list, a table (Tab bar #506, Approval sheet #507) and a Python code block |
| "nightly report" | `nightly-report` | Writes reports/nightly.md, answers "Nightly report written to reports/nightly.md. All quiet." |
| "bad tool call" | `invalid-tool-call` | A malformed `write_file` call the harness rejects |
| "stuck loop" | `stuck-loop` | The same failing `read_file` eight times: a nudge from the third, then the run ends `stuck_repeat` |
| anything else | fallback | "[fake model] No fixture script matches this message. Scripts: ..." |

**Reset.** There is no in-place reset. Reset is Teardown followed by Start, which takes about 10 s and gives the same
seed content with new IDs.

**Sign-in.** There is no password. Both ways in act as the **owner**:

- **The `/connect` page** (`<web>/connect`) is the default. It stores the Server URL and the instance's owner token in
  that origin's `localStorage` (`harness.daemonUrl`, `harness.ownerToken`). Then it opens Web as a separately hosted
  copy, the same flow as Settings → Connection. The token is an owner admin token approved only for `<web>`.
- **The bundled copy** at `<server>` needs the local owner token on every request. In Playwright that is
  `browser.newContext({ extraHTTPHeaders: { "X-Agent-Harness-Local-Token": "<local token>" } })`. A browser on its
  own gets 401 there.

For API checks, use `Authorization: Bearer <owner token>` with `X-Agent-Harness-Client: cli/2`.

**Roles.**
- Owner is the only role these tests can exercise.
- Household members and guests are recognized only through a tailnet identity that tailscaled forwards. A loopback
  instance can't produce one, so member and guest journeys are not applicable and need a real tailnet device.

## Journeys

`<web>` is the READY `web` URL, signed in through `/connect`. Every route is a hash route. The role is always the
owner. "Ready" is the first signal that data has loaded. Page titles are set before data loads, so they are never a
readiness signal.

| ID | Path / ready signal | Seed | Actions | Expected outcome, and after reload | Audit expectation |
| --- | --- | --- | --- | --- | --- |
| AH-01 sign-in and connection | `<web>/connect` → `#/chat`; ready: `#conn[data-state="live"]` and `textarea[aria-label="Message"]` | default | Open `/connect`. | The chip `#conn` reads "Live", and the tab bar `nav#tab-bar` has Chat, Agents, Jobs, Images and Profile. After reload: still Live with no token prompt. | Not applicable: sign-in writes no audit row |
| AH-02 agents groups and search | `#/agents`; ready: `section.agent-group[aria-label="Needs you"]` with `a.agent-row` | default | Read the groups. Type "compaction" in `input[type=search]`, then clear it. | Needs you: Protected settings and Failed run. Recent: the four done seeds. Search is full-text and may also return "Tab bar tour" because its reply mentions the compact header. Search the complete title "Long task with compaction" when identifying that seed. After reload: the same groups. | Not applicable (read-only; Web has no session delete) |
| AH-03 tool rows | `#/agents` → "Tool rows" → `#/s/<id>`; ready: `.tabs.session-tabs` and the `.badge` "done" | default | Expand the `read_file notes/log.txt` row, use Copy and the full-screen viewer, close it. Open Changes, then Info. | Rows as seeded; the missing read is marked "error". Changes says "No git repositories in this workspace yet.": these files are loose workspace files, not a git repository. Verify their contents in the tool output viewer. After reload: the same rows. | Not applicable (read-only) |
| AH-04 approve | Agents → "Protected settings"; ready: `section.approval-sheet[aria-label="Approval needed"]` | default, **consumes the seeded approval** | Check the diff (`+  "theme": "system",`) and the reason. Click "✓ Approve". | The sheet closes, the card says "approved", the badge goes to done, and the answer is "Finished. The tool row shows whether the write was approved or denied." Changes still says "No git repositories in this workspace yet."; the write is confirmed by the tool result and approval audit. After reload: done and approved. | `GET <server>/api/admin/v1/audit?action=approval.decide`: newest row (after a `started` row) has outcome `ok`, `metadata.decision` `approved`, `target_kind` `approval`, `actor_kind` `owner_key` and `key_id` equal to READY `key_id` |
| AH-05 deny with a note | Agents → "Tab bar tour"; ready: composer `textarea` with placeholder "Continue this session…" | default | Send "Now edit the protected settings file." When the sheet shows, click "Add a note for the agent", type "UI pass deny" and click "✕ Deny". | The card says "denied" with the note, the tool row is an error, and the same final answer arrives. After reload: still denied. | As in AH-04, with `decision` `denied` |
| AH-06 new task: create and validation | `#/new`; ready: `section.new-task-page[aria-label="New task"]` | default | Click "Start" with an empty prompt. Then pick Project `ui`, Title "UI pass markdown", Prompt "Show me some markdown." and Start. | The empty prompt gives the toast "Write a prompt first" and creates nothing. Start opens `#/s/<id>` and streams "Summary", the table and the code block, then done. After reload: the session is done and listed under Recent. | None: session creation is not audited |
| AH-07 running, queue and cancel | `#/new` | default | Start "Run a slow task so I can watch it running.", then right away start "Hello again". On the slow session, open Session menu → "Cancel task" → "Cancel task". | The slow session is under Running and the second shows queued until the slow one ends. Cancel ends it as cancelled, and the queued one then finishes done. After reload: cancelled and done. | None: cancel is not audited |
| AH-08 retry and failure | Agents → "Retry after a model error", then "Failed run"; ready: `.session-tabs` | default | Read both. On Failed run, open Session menu → "Run again as new session". | Retry: the row "Model call retried (1)", then the answer. The seeded HTTP 500 is a server-side fixture detail; the row does not render its error text. Failed: the failed badge and the HTTP 400 text, and the rerun fails the same way (scripted). After reload: unchanged. | None |
| AH-09 compaction | Agents → "Long task with compaction" | default | Scroll the transcript. | Compaction rows appear between the long replies, then the final answer. After reload: unchanged. | Not applicable (read-only) |
| AH-10 rename: edit and cancel | Any done session; ready: `button.session-menu-btn[aria-label="Session menu"]` | default | Session menu → Rename, type "Renamed by UI pass", Enter. Rename again, type "discard", press Escape. | The header shows "Renamed by UI pass", and the Escape edit is dropped. After reload: "Renamed by UI pass" in the header and in Agents. | None: owner session rename is not audited (verified) |
| AH-11 jobs CRUD | `#/jobs`; ready: `.job-row[data-job]` with section labels "Scheduled" and "Paused" | default | 1. Switch `Weekly cleanup enabled` on. 2. `#/jobs/new`: Name "UI pass job", Task "Write the nightly report.", Cron "bad", then Create. 3. Fix Cron to `0 5 * * *`, Project `ui`, Create. 4. Rename it to "UI pass job v2" and Save. 5. Run now. 6. Delete → Cancel. 7. Delete → "Delete job". | 1: toast "Weekly cleanup resumed" with Undo. 2: "a schedule needs 5 fields: minute hour day-of-month month day-of-week" under Cron and as a toast; nothing is created. 3: toast "Saved · next run …". 5: a new session ends with "Nightly report written to reports/nightly.md. All quiet." 6: no change. 7: the job is gone. After reload: Weekly cleanup is enabled, "UI pass job v2" exists after steps 3 and 4, and it is absent after step 7. | `/api/admin/v1/audit?target_id=<job id>`: `job.create` (step 3), `job.update` with `metadata.fields` `["name"]` (step 4), `job.run` (step 5; a `started` row, then `ok`), `job.delete` (step 7), each ending `ok` with READY `key_id`. Step 1 is a `job.update` on the Weekly cleanup id. |
| AH-12 chat | `#/chat`; ready: `textarea[aria-label="Message"]` and `section[aria-label="Recent chats"]` | default | Open the seeded chat. Send "Hello there". Rename it to "UI pass chat" (sheet "Rename chat", field "Title"). Then delete it, confirming in the sheet. | The reply is the tab-bar-tour text. The rename shows in the header and in Recent chats. After reload: renamed, and after the delete it is gone from Recent chats. | None: chats are not audited |
| AH-13 settings and theme | `#/settings`; ready: `.version-row` and `a.set-row[data-setting]` | default | Open Appearance (`#/profile/appearance`) and pick "Dark", then "Light", then "System". Open Connection (`#/profile/connection`). | Groups: This device, Agents, Server and Integrations. Appearance takes effect at once (see Theme and layout). Connection shows the Server URL. After reload: the chosen theme persists (in `localStorage`). | Not applicable: theme is per browser |
| AH-14 connection state | Any page; ready: `#conn[data-state="live"]` | default | `context.setOffline(true)`, then `setOffline(false)`. | `#conn` goes to `data-state="offline"` ("Offline") at once, then back to "Live". Inside a session, the strip `.conn-strip[role=status]` shows while it is offline. | Not applicable |
| AH-15 images | `#/images` | – | Open the tab. | **Not applicable.** Fake mode drops the images module, so the page shows a `p.note.bad` error (HTTP 404). That is expected here and is not a defect. | Not applicable |

**Independence between cases.**

- AH-04 consumes the seeded approval. Each device or theme case that needs it gets a fresh instance (Teardown, then
  Start), or creates its own approval as AH-05 does.
- AH-05, AH-06, AH-07, AH-10, AH-11 and AH-12 create their own entities. Use unique names per case (for example a
  `-<device>-<theme>` suffix) when they share an instance.
- AH-07 occupies the one run slot for about 50 s, so run it alone.
- Everything else is read-only on the seed.

## Theme and layout

- **Selecting the theme.**
  - The app's own switch is Settings → Appearance (`#/profile/appearance`): buttons `button.theme-choice` named
    "System", "Light", "Dark", "Midnight", "Forest", "Paper" and "Custom". The selected one has the class `.on`.
  - The choice is stored in `localStorage["harness.theme"]` (`auto` means System) and applied before first paint.
  - The default is System, which follows `prefers-color-scheme`. So `colorScheme: 'light' | 'dark'` emulation alone
    switches the effective theme, as long as no explicit theme was picked in that browser profile.
  - The Light and Dark buttons set `<html data-theme="light|dark">`. System removes the attribute.
- **Verifying the effective theme.** Read `getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()`.
  It is `#f3f4f6` for light (and System + light) and `#000000` for dark (and System + dark). `<meta
  name=theme-color>` updates only on an explicit choice.
- **Live resize: menu plus a partly filled form.**
  1. Open a done session, for example "Tab bar tour" (`#/s/<id>`).
  2. Fill the composer ("Continue this session…") with "Partly typed follow-up" without sending.
  3. Click `button[aria-label="Session menu"]` so `[role=menu][aria-label="Session"]` is open.
  4. Resize 1440 → 375 → 1440 without reloading.

  Expected:
  - The composer keeps its text and Transcript stays the selected tab.
  - The menu is either still open or closed cleanly. The menu does not close on resize, and nothing stale blocks taps.
  - At 1280 px and wider a list pane appears beside the page without re-routing.
  - No console errors.

  An alternative with a sheet: on `#/jobs/<id>`, edit Name partly and click Delete so the "Delete job" sheet is open.
- **Layout breakpoints.** Below 768 px the tab bar is a bottom bar, hidden on detail pages. From 768 px it is a left
  rail. From 1280 px, Agents, Jobs, Profile and Settings show a list pane beside the page. The new-task button is
  in the DOM twice, at phone and desktop positions, with CSS hiding one, so role queries can match two elements.
- **Sheets.**
  - In-app sheets are `<dialog class="sheet">` opened with `showModal()`, reachable as `getByRole('dialog', { name:
    <title> })`.
  - They close with Escape, a backdrop click, browser Back (any hash change) or Cancel.
  - The approval sheet is not a dialog. It is a `region` named "Approval needed" that stays until the approval is
    decided.
- **Touch, drag and PWA.**
  - Web has no swipe or drag gestures: every action is a tap.
  - The manifest is at `/manifest.webmanifest` (`display: standalone`, `start_url /`).
  - The service worker registers only on HTTPS, so this loopback HTTP instance runs without one. Offline and
    service-worker cache checks are not applicable here.
  - Standalone mode only hides the Install row in Settings.
- **Needs a real device.** Safe-area insets, the iOS Home Screen install and its label, a real on-screen keyboard
  covering the composer, and Tailscale-served HTTPS (service worker and push notifications).

## Playwright and CI

- **Current state.** The Playwright suite added by #589 lives at the paths below. The
  existing web tests are pytest wrappers around Node scripts with DOM stubs (`tests/test_web_*.py`).
- **Package manager and paths.** npm, in a self-contained package like `tools/web-types`:
  - `tools/ui-tests/package.json` with `@playwright/test` pinned to an exact version, plus its `package-lock.json`
  - config `tools/ui-tests/playwright.config.ts`
  - tests `tools/ui-tests/tests/*.spec.ts`
- **Commands.**

  ```
  npm --prefix tools/ui-tests ci
  npm --prefix tools/ui-tests exec -- playwright install chromium webkit
  npm --prefix tools/ui-tests test -- --workers=1
  ```

- **Instance and base URL.**
  - Playwright's `webServer.command` runs `node start.mjs`, which invokes `python -m harness.fake_instance --dir <instance-dir> --port 18800 --web-port
    18801` from the repository root. The wrapper suppresses stdout so the disposable tokens are never printed in CI.
  - Use `url: 'http://127.0.0.1:18801/ready'`, `reuseExistingServer: false` and `timeout: 180_000`.
  - Set `use.baseURL` to `http://127.0.0.1:18801`.
  - Use one fixed **base directory** for start and all teardown paths:
    `process.env.HARNESS_UI_INSTANCE_DIR ?? path.join(os.tmpdir(), 'harness-ui-tests')`. CI sets
    `HARNESS_UI_INSTANCE_DIR` to `$RUNNER_TEMP/harness-ui-tests` through `GITHUB_ENV`
    in an initial setup step, so the test and cleanup steps share the same base directory.
    Relative base paths resolve from the repository root for both npm and standalone cleanup.
  - `run.mjs` allocates a unique `run-<uuid>` child with an `owner.json` recording its identity and runner pid;
    the actual fake instance lives in `<base>/run-<uuid>/instance`. It propagates `HARNESS_UI_RUN_ID` to Start
    and Playwright's global teardown. The npm `finally` teardown uses that same identity, and repeated cleanup
    is a no-op. A second invocation gets a different directory even with an explicitly shared base.
  - The separate CI `cleanup.mjs` step recovers those persisted identities only after their runner has exited.
    Startup also recovers stale runs this way. A live or unreadable owner pid (including a reused live pid)
    leaves the run untouched. If stopping its recorded instance fails, the handle and ownership record remain.
    Ownership records are published atomically; incomplete or unreadable records are preserved without blocking
    allocation of another run.
    Legacy instances directly in the base directory are preserved; stop them explicitly with the manual command.
  - The start refuses busy ports, so a competing invocation fails loudly and cleans only its own allocation.
    It never pre-cleans the other invocation's live instance. Parallel browser suites are not supported.
- **Fixtures.**
  - A `signedIn` fixture calls `page.goto('/connect')` and waits for `#conn[data-state="live"]`.
  - Mutating specs create their own sessions, jobs and chats with unique names, or run with `workers: 1` against a
    fresh instance. Every mutating test uses a unique name; approval tests create their own pending approval, so the default protected seed stays available for layout tests.
  - Nothing is shared or persistent between runs.
- **Projects.** Desktop light and dark at 1440×900, and phone light and dark with `devices['iPhone SE']` (WebKit) and
  `devices['Pixel 7']` (Chromium).
- **Teardown.** Playwright stops the start process when the run ends. On Windows that is a hard kill, so the instance
  directory can remain: always run Teardown afterwards. Add a `globalTeardown` that runs it, plus a CI step with
  `if: always()`.
- **Reports and traces.** Reports go to `tools/ui-tests/playwright-report/` and traces and screenshots to
  `tools/ui-tests/test-results/`, with `trace: 'retain-on-failure'`. Both are gitignored. CI uploads them with
  `actions/upload-artifact` and `if: always()`, after redacting tokens from both original traces and HTML-report trace copies. `teardown.mjs` and `cleanup.mjs` implement that redaction; a failed cleanup prevents evidence upload. The npm runner stops the instance in `finally`, and CI repeats cleanup with `if: always()`.
- **CI.**
  - The repository is **public** (`gh api repos/dflippojr/agent-harness --jq .private` → `false`), so it uses
    GitHub-hosted runners only.
  - The existing `ci.yml` jobs run on `windows-latest` with Python 3.12. Add a UI job there that:
    1. runs `pip install -r requirements.txt`
    2. uses `actions/setup-node` with Node 20 or later
    3. runs the npm commands above
    4. runs `node tools/ui-tests/cleanup.mjs` with `if: always()` to redact traces and recover the finished run's
       recorded directory (the existing dependency-install failure fallback has no newly started instance)
  - Never use the tower's self-hosted runner for this public repository.
- **Never in tests.** Live URLs, `:8100`, a `*.ts.net` origin, the production data directory, or tokens copied
  between runs.

## Teardown

Run this from the repository root in PowerShell or Git Bash, with the same `--dir` you gave Start:

```
python -m harness.fake_instance stop --dir <instance-dir>
```

What it does:

- It reads `<dir>/instance.json`.
- It stops that process and its children, but only if the process's command line is `harness.fake_instance` and its
  start time matches the handle. A reused pid, even one belonging to another instance, is never killed.
- It deletes the directory, including the read-only git checkpoint objects.
- It works after a partial start:
  - with no handle file
  - with a handle whose process is already gone
  - on an empty directory
- It refuses (exit 2) any non-empty directory without the `data/fake-model-instance` marker, so it never deletes
  unrelated files. An `instance.json` alone is not enough.
- It is safe to repeat. A missing directory prints `nothing to stop` and exits 0.
- It returns nonzero and preserves the entire directory and handle if recorded process metadata cannot be
  inspected (for example, `AccessDenied` after an elevated Windows run), or if the owned process or its children
  cannot be stopped. It does not guess which process to kill or delete files before shutdown is established.
  Retry from a terminal with permission to inspect/stop that recorded process; do not delete its handle by hand.

For automated UI suites, use `node tools/ui-tests/cleanup.mjs` with the same `HARNESS_UI_INSTANCE_DIR` base rather
than manually stopping the base directory. Manual `harness.fake_instance stop --dir` still targets an actual
instance directory, such as `<base>/run-<uuid>/instance`, and intentionally stops that specific instance.

In an interactive terminal, Ctrl+C, Ctrl+Break or SIGTERM do the same: both servers stop, the SQLite files close and
the directory is deleted, unless you passed `--keep`.

To verify that nothing owned remains:

- `<instance-dir>` does not exist.
- `GET <server>/health` and `GET <web>/ready` refuse the connection.
- No process with the recorded `pid` is running (`Get-Process -Id <pid>` fails, or `ps -p <pid>` prints nothing).

Teardown touches nothing outside the instance directory. Screenshots, reports and logs you redirected elsewhere stay
where they are.

## First pass coverage and limitations (#589)

- Regression commands use one Playwright worker. On the tower, local pytest uses at most `-n 2` under the dispatcher memory limit. Hosted CI retains its existing four-worker pytest cap.
- Passing flow tests cover AH-01 through AH-14; AH-15 remains not applicable. The no-overflow regression covers Agents, New task, Jobs, Job form, Profile, Settings, Appearance, Connection, approval, and session Transcript/Changes/Info at 320/375/768/1440 in both themes and phone projects. The #591–#595 follow-up adds Chat landing to the same reflow sweep and behavioral regressions in `defects.spec.ts`: reachable starters/recent chats, New task and optional project label focus, create/edit Enabled targets and keyboard toggles, 8px session-tab gaps, and ordinary tool-action taps through portrait → 667×375 landscape → portrait. These run in all six browser/theme projects with no expected failures, retries, forced taps, or hidden page overflow.
- Optional disabled modules currently yield 404 probes for `/gpu`, `/models/status`, `/resources`, `/memory`, and `/remote-control`. Record these as expected feature-detection diagnostics in fake mode, separately from unexpected HTTP failures and JavaScript exceptions. No request reaches a GPU or real model provider.
- Installed iPhone SE descriptors can be 320×568; explicitly use 375×667 while retaining the descriptor UA/DPR/mobile/touch settings. Windows WebKit reports `navigator.maxTouchPoints=0` despite a context with `hasTouch=true`; record both rather than claiming physical touch hardware. Chromium Pixel emulation reports touch=1.
- Root-font scaling is only a text-size approximation: fixed 16px form text stays 16px. Record that limitation separately from actual 320 CSS px reflow. The desktop breakpoint sweep includes 2560px, and short-screen checks include 1280×600, 1024×500, and phone landscapes.

## Second pass coverage (#625)

- The second pass ran after the mobile redesign (#506–#515) on iPhone SE, iPhone 15, Pixel 7, iPad Mini and desktop, in light and dark. It covered every Settings subpage and Actions tab, chat threads, sheets, rotation mid-flow, the keyboard approximation, short screens and zoom.
- `tests/redesign.spec.ts` locks in the flows it found passing, in all six projects:
  - every Settings subpage and Actions tab reflows at 320, 375, 768 and 1440 px, and Back returns to Settings
  - the chat thread and an existing job's form reflow at the same widths
  - the Rename chat sheet keeps its draft and focus through portrait → landscape → portrait and closes with Escape, browser Back and a backdrop tap
  - the session composer and Send stay in view under the header at 60 % height in both orientations
  - live resize from 1440 to 375 px and back keeps the Delete job sheet open, focus inside it and a partly edited name; Cancel returns focus to Delete, and a reload shows the unsaved name was never stored
- The pass filed these defects: #626–#634. Their regression tests come with their fixes.
- WebKit does not focus a button on click, so a sheet opened by a click returns focus to `body` there. Tests open the sheet from the keyboard when they check where focus returns.
- A Web origin opened without `/connect` (no stored Server URL or token) shows "Demo access — look around only" and an HTTP 404 note. That is the expected state for an unconfigured, separately hosted copy, not a sign-in path.
