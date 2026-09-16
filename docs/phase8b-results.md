# Phase 8b: Claude Code Remote Control launches

Issue [#19](https://github.com/dflippojr/agent-harness/issues/19). Commit `e7b6775`.

## Decisions (user)

- Start a **`claude remote-control --spawn worktree`** server per project, not single `claude --bg` sessions.
  That gives one pairable entry per project, and every session opened from the phone gets its own git worktree.
- Sessions use permission mode **default**: Claude asks in the Claude app before edits and commands.
- Launches can come from **the web app, the app API, and an agent tool that always asks**.

## What Claude Code 2.1.272 already provides

- `claude remote-control` runs without a terminal. Its output includes the pairing link
  (`https://claude.ai/code?environment=env_…`), a `Capacity: n/N` line, and per-session links inside OSC 8
  hyperlink escapes.
- `claude --bg`, `claude agents --json`, `stop` and `rm` manage background sessions. They weren't needed here, but
  they're the fallback if single sessions are ever wanted.
- **Workspace trust:** `remote-control` exits with `Error: Workspace not trusted. Please run claude in <dir> first`
  until the folder's trust dialog has been accepted. Trust isn't inherited from parent folders. Worktree mode also
  needs a git repository.

## Built (`harness/remote_control.py`)

- **Eligible projects:** tower projects whose `repo` is a local folder (optionally limited by
  `remote_control.projects`) plus local paths configured under `remote_control.folders`. Standalone folders are
  native Claude Remote Control entries only: they do not become harness session projects or enter the Docker
  sandbox. On the tower this also exposes the agent-harness repo and the private memory-library working copy.
- **Launch:**
  - Refuses untrusted folders, reading `hasTrustDialogAccepted` from `~/.claude.json`, with instructions to run
    `claude` there once. The harness never accepts trust for the user.
  - Refuses non-git folders in worktree mode.
  - Starts the unmodified CLI with no console window, in its own process group, logging to
    `<data_dir>/remote-control/<project>-<time>.log`.
  - Waits up to 30 s for the pairing link, then sends a notification whose tap opens the pairing link. If the
    process exits first, the launch fails with the CLI's `Error:` line.
- **Registry:** `state.json` records the pid and process start time (so a reused pid isn't mistaken for the server).
  A restarted daemon still sees and stops servers it started. The restart script only kills the daemon, so servers
  keep running.
- **Stop:** kills the process tree. The npm `claude` shim starts `cmd.exe`, then `claude.exe`, then node children.
- **Web app:** Settings → Claude Remote Control lists each project with its trust state, running state, session
  count, and Trust in Claude / Start / Stop / Open in Claude. For a newly configured repository, Trust in Claude
  opens an interactive Claude window in that exact folder on the tower; the user accepts Claude's own workspace
  trust prompt, and the card polls until the repository becomes trusted. The harness never accepts trust itself.
- **API:** `GET /remote-control`, `POST /remote-control/{project}`, `POST /remote-control/{project}/trust`, and
  `POST /remote-control/{project}/stop`. The trust endpoint is local-web-only because it opens an interactive tower
  window. The app API mirrors status, start, and stop under `/api/v1/remote-control` with the new scope
  `remote_control`, and the API version is now 1.1.
- **Agent tool:** `open_claude_remote_control(project, reason)`. It's in `ALWAYS_ASK`, so a project rule can't make
  it automatic. Tower sessions only, and not app sessions.

Sessions opened this way are ordinary Claude Code sessions under the user's own login. They don't use the harness
queue, sandbox, approvals, or transcripts.

## Also fixed

A syntax error from 8c: two `"\n"` literals in `app.js` had been written as real line breaks, which stopped the
whole web app from loading after the 8c restart. It was found while checking this phase's UI and fixed within the
hour. `test_web_app_js_parses` now runs `node --check` on `app.js`.

## Verified

- Tests: 131 pass. The new tests cover log parsing; launch, status and stop across a fresh registry instance, using
  a fake CLI process; the untrusted, URL-project, unknown-project and failed-start paths; and the always-ask policy.
- Live on the tower after the restart:
  - `GET /remote-control` lists the three projects as untrusted git folders.
  - `POST /remote-control/invoice-tools` answers 400 with the trust instructions.
  - A phone-size screenshot of the Settings card was checked.
- Earlier manual runs confirmed that `claude remote-control` connects and prints the links when started without a
  terminal from a trusted folder. That was the Documents folder in `same-dir` mode.

## Exit test (user)

1. From the phone: Settings → Claude Remote Control → invoice-tools → Trust in Claude. On the tower, review the
   folder in the new Claude window and accept the one-time workspace trust prompt, then exit Claude.
2. From the phone: tap Start. The "Remote Control ready" notification
   should arrive. Tap it or "Open in Claude", start a session in the Claude app, and confirm that it works in a
   worktree and asks before editing.
3. Stop it from the card.
