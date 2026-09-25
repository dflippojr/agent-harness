# Phase 4: MacBook target

Built 2026-09-14. The MacBook is a 2021 M1 Pro (16 GB) on macOS 27.0, with the Command Line Tools (git, Python
3.9.6, Swift 6.4, clang), Homebrew (python3 is Homebrew's 3.13) and OpenJDK 23, but no full Xcode. Its disk was 98% full
(26 GB free). (An early SSH check reported no Homebrew because a non-login SSH shell's PATH lacks /opt/homebrew/bin;
the runner's PATH puts it first.)

## Decisions (user, 2026-09-14)

- **D6 transport:** an outbound runner on the Mac. The model and agent loop stay on the tower.
- **D7 sandbox:** commands run natively (real toolchains) under `sandbox-exec`, file tools limited to the session
  workspace. A Linux container was ruled out because the user wants iOS/Swift/Xcode work too.
- Reads: most of the disk, minus credentials and personal folders. Writes: workspace, temp, build caches.
- Network: none by default; a command with `network: true` asks for approval, as on the tower.
- Changes land on `agent/<session>` in a separate checkout, reviewed from the phone.
- Offline Mac: the session waits, notifies, resumes on wake; the runner holds `caffeinate -i` while a task runs.
- Setup over SSH from the tower (Remote Login on the Mac); one demo repo to start.

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| Runner hub (daemon) | `harness/remote.py` | long-poll queue, presence, redelivery, restart detection, `RemoteWorkspace` |
| Runner API | `harness/api.py` | `POST /runners/{name}/poll`, `POST /runners/{name}/results` (bearer token), `GET /runners` |
| Shared modules | `harness/fileops.py`, `harness/projects.py`, `harness/changes.py` | stdlib only, Python 3.9 compatible; copied to the Mac |
| Mac runner | `macrunner/harness_runner.py` | stdlib only; executor for file, shell, git and review ops |
| Sandbox profile | `macrunner/sandbox.sb` | SBPL; network rules appended by the runner per command |
| Install | `macrunner/install.sh`, `macrunner/dev.agent-harness.runner.plist`, `ops/macbook/deploy.ps1` | launchd agent `dev.agent-harness.runner` |
| Config | `runners:` in `config/harness.yaml`; `target:` per project in `config/projects.yaml` | token in `D:/Agents/harness/secrets/runner-macbook.token` |
| Web app | `harness/web/app.js` | "waiting for Mac" badge, target on cards and in the header, runner state on New task and Settings |
| Tests | `tests/test_phase4.py` | 9 tests (48 total); a fake runner drives the real executor, no Mac needed |

## How it works

- **Transport.** The runner long-polls the daemon through `tailscale serve` (HTTPS with the tailnet cert; the Mac's
  stock Python verifies it). A poll is held up to 25 s and returns queued requests plus `keep_awake`. Results are
  posted separately and retried until accepted. A WebSocket was the plan; long-polling was chosen instead because
  it needs no third-party package on the Mac's Python 3.9 and needs no reconnect logic across sleep.
- **Presence.** A runner that hasn't polled for 45 s is offline. For 45 s after a daemon restart, sessions wait
  quietly for the runner to reconnect before announcing a wait.
- **At-least-once delivery.** Each poll lists the request ids still in flight on the Mac. A request that was handed
  out but isn't listed is resent after 20 s, and the runner answers a resend from a cache of recent results. A new
  runner instance id means the runner process restarted: requests the old instance took fail as "effects unknown",
  like an interrupted tool call on the tower.
- **Timeouts ignore offline time.** A command's budget only runs while the Mac is online, and the runner's own
  timeout uses the monotonic clock, which stops while the Mac sleeps. A command running when the lid closes
  continues after wake.
- **Waiting.** A Mac session whose runner is offline goes to `waiting_target`, gives up the GPU slot, emits
  `target_waiting` (ntfy notification) and continues on `target_online` (the notification is replaced through the
  same ntfy sequence id). This happens before the workspace is prepared, before each tool call, and during a call
  whose runner drops off.
- **Keeping the Mac awake.** Polls return `keep_awake` while any of the runner's sessions is `running` (not while
  queued or waiting for an approval). The runner then holds `caffeinate -i -w <runner pid>`. Lid close still sleeps.

## Workspaces and review on the Mac

- Workspaces live in `~/.agent-harness/workspaces/<session>`. A project's `repo` is a path on the Mac and must be
  under one of the runner's `repo_roots` (default `~/Projects`), checked on the Mac, so the daemon can't point the
  runner at arbitrary directories.
- The clone is `git clone --shared` from the source: it borrows the source's objects through git alternates
  instead of copying them, which matters on a full disk. The source repo is outside the sandbox's writable paths,
  so the agent can't touch it. The session branch is fetched back into the source after every run.
- The user picked "branch in a separate worktree". A real `git worktree` would put the session's refs, index and
  objects inside the source repo's `.git`, which the sandbox would then have to make writable (hooks and config
  included). A shared clone gives the same experience (own branch, user's checkout untouched, review from the
  phone) without that.
- Save, merge, push, discard and cleanup run in the runner **outside** the sandbox with the user's git, reusing
  `harness/projects.py`. Because host-side git runs in the workspace, the sandbox denies writes to the workspace's
  `.git/config`, `.git/hooks`, `.git/info`, `.git/objects/info/alternates` and the `.git` directory entry itself
  (so it can't be renamed and replaced). Otherwise the agent could make the runner execute code unsandboxed
  through a hook, `core.fsmonitor` or a filter driver.
- File tools run in the runner, unsandboxed, confined to the workspace by path resolution. Symlinks that point out
  of the workspace are refused (read/write) or skipped (list/search); verified on the Mac. `put_file` copies a
  tower-generated binary (base64, 32 MB cap) into the session workspace the same way, used by `generate_image`.
- Changes and review requests fail fast with 503 while the Mac is offline instead of queueing.
- Cleanup asks an online runner to delete finished workspaces after the usual 14 days, saving local branches first.
  Workspace quota on the Mac: 3 GB per session; new sessions are refused below 10 GB free.

## Sandbox profile

Base `(allow default)`, then:

- `(deny file-write*)` except the workspace, `/private/tmp`, `/private/var/folders` (per-user TMPDIR), device
  files, `~/Library/Caches`, `~/Library/Developer/Xcode/DerivedData`, `~/.cache`, `~/.npm`, `~/.m2`, `~/.gradle`.
- Neither read nor write: `~/.agent-harness/runner` (token), `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.docker`, `~/.kube`,
  `~/.config/gh`, `~/.netrc`, `~/.git-credentials`, `~/.npmrc`, `~/.pypirc`, shell histories, Documents, Desktop,
  Downloads, Pictures, Keychains, Messages, Mail, Safari, Cookies, iCloud Drive, Group Containers, Containers, and
  Chrome/Edge/Firefox/Brave profiles.
- `mach-lookup` of `com.apple.SecurityServer` / `com.apple.securityd.xpc` denied, so Keychain items (including git's
  osxkeychain credentials) can't be fetched.
- Without approval: `(deny network*)` except binding/accepting on localhost and connecting to localhost. DNS is
  denied too (the mDNSResponder socket), so nothing leaks through lookups.
- Gotcha found while testing: `(allow network* (local ip "localhost:*"))` also matched ordinary outbound
  connections (their local address), letting `curl https://example.com` through. The rules now use
  `network-bind`/`network-inbound` for the local side and `network-outbound (remote ip ...)` for the remote side.
- Commands get a clean environment (PATH with Homebrew locations first, HOME, LANG, `GIT_TERMINAL_PROMPT=0`, and a
  TMPDIR private to the session, created with mode 0700 and named by session id under a private per-user base, so it survives runner restarts, and removed with the workspace) and run in their own process
  group; timeout and cancel kill the group.

### Verified on the Mac (real profile, runner executor, 2026-09-14)

| Check | Result |
| --- | --- |
| write in workspace, `python3 -m unittest`, `git commit` | work |
| `swiftc` compile + run, `java H.java` | work |
| `git config user.name x` in the workspace | Operation not permitted |
| write `.git/hooks/post-commit`, `mv .git .git2` | Operation not permitted |
| write `~/`, write the source repo | Operation not permitted |
| read `~/.ssh`, `~/Documents`, runner `config.json` | Operation not permitted |
| `security find-generic-password` | fails (securityd unreachable) |
| `curl https://example.com`, Python DNS lookup | could not resolve |
| `getaddrinfo("localhost")`, local TCP server + client | work |
| `curl` with `network: true` | 200 |
| `sleep 10` with a 2 s timeout; `kill_session` mid-`sleep 30` | exit 124; killed by SIGTERM |
| prepare → commit → save_branch → changes → discard | branch published, then removed; source clean |

## Tests

`tests/test_phase4.py` (Windows, no Mac): end-to-end Mac session (write, shell, search, read, branch saved, shared
clone, changes, squash-merge) through a fake runner; waiting for an offline Mac then resuming, with notification
payload and a 503 for changes; target/project validation and the free-space refusal; hub redelivery and
restart-failure semantics; timeouts that ignore offline time and send a cancel; runner endpoint auth; executor
refusing repos outside `repo_roots` and bad session ids; shell timeout and absolute workspace paths; symlink
containment (skipped on Windows without developer mode, run by hand on the Mac instead).

## Live run (2026-09-14)

After the daemon restart the runner connected on its own over the tailnet (`GET /runners`: online, 26.9 GB free,
sandbox on). From the API, project `invoice-tools-mac` ("the test suite is failing…"): Qwen ran the tests on the
Mac (3 failures), fixed `>` → `>=`, re-ran them (4 pass) and committed; the branch `agent/94510bc970` landed in
`~/Projects/invoice-tools-mac` with `main` untouched. 37 s end to end, 9 model turns; the runner log shows
`caffeinate` held during the run and released after. The phone exit test (lid-closed wait) is still for the user.

## Web app additions (same day, user requests)

- **Compaction progress.** A summary now emits a persisted `compaction_started` event and ephemeral `compacting`
  progress: step 1 reads the old context (a real bar from llama-server's `return_progress` chunks), step 2 writes
  the summary (token count). The note shows elapsed time and turns into "Context condensed: ~X → ~Y tokens
  (took N s)" with the summary behind a disclosure. Small elide-only trims no longer add a note every turn.
- **Prompt reading progress** for any model turn with at least 4K uncached prompt tokens ("Reading context 16K of
  18K new tokens (12K cached)"). Verified against llama-server b10950: `prompt_progress` is `{total, cache,
  processed, time_ms}`, one chunk per 2048-token batch, and `processed` includes the cached prefix, so the bar
  covers only the uncached part.
- **Tokens and context** under the session header: cumulative tokens in/out (compaction summaries now count toward
  the session totals) and a context meter (turns amber at 55%, where trimming starts).
- **Thinking timer.** The "Thinking…" bubble appears when a model turn starts, not at the first token, with an
  elapsed timer; finished turns say "Thought for N s".
- **Scrolling.** The transcript used to treat anything within 160 px of the bottom as "at the bottom" and snapped
  there on every streamed chunk, so a slow upward scroll was pulled back. It now stops following on any upward
  scroll (wheel, touch, momentum), never scrolls while a finger is down, resumes at the very bottom, and shows a
  "↓ Latest" button. Checked in headless Edge: an 80 px scroll in 8 px steps during streaming held, and the button
  returned to the bottom.
- Homelab projects without a repo now tell the agent it can't change files on the server and should report the fix
  and which project to run it in. A real session (`efcb152521`) spent 34 turns trying to get around the sandbox
  to edit plex-webhook.

## Known gaps

- **No full Xcode on the Mac**: `xcodebuild` fails (Command Line Tools only), so iOS app builds need Xcode
  installed. The DerivedData cache is already writable in the profile.
- `sandbox-exec` is deprecated. It works on macOS 27.0; re-test the profile after macOS updates (the probe script
  approach in this doc takes a minute over SSH).
- The Mac runs under launchd only while the user is logged in (a LaunchAgent). After a reboot it starts at login.
- Runner log rotation is crude (install.sh restarts the log past 5 MB).
- Package installs with `network: true` write to caches (npm, pip) but global installs (e.g. `brew install`) are
  outside the writable paths and fail; ask the user instead.
