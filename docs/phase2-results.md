# Phase 2: phone control surface

Built 2026-09-14. Decisions (user, 2026-09-14): self-hosted ntfy (D5); daemon and Docker Desktop autostart;
tapping a notification opens the approval card, long-pressing it gives quick Approve / Deny (Duo-style);
no-build vanilla JS.

## What exists

| Piece | Where |
| --- | --- |
| Web app (installable PWA) | `harness/web/` (served by the daemon at `/`); hash routes `#/`, `#/new`, `#/s/<id>[/approval/<aid> \| /changes \| /info]`, `#/settings` |
| Screens | session list (live via `/events`), new task with templates, live transcript (token streaming, collapsible thinking and tool calls), approval cards with diff/command and note, cancel / continue / run again, diff viewer, session info, settings (notification setup, test button, install help) |
| Notifications | `harness/notify.py` → ntfy container (`D:\Docker\ntfy`) |
| Approval buttons | `POST /a/<token>/approve\|deny`: a per-approval secret, so the ntfy app needs no login to the daemon |
| Workspace diffs | `harness/changes.py`, `GET /sessions/<id>/changes`: each git repo in the workspace vs. its upstream merge-base (agent commits + uncommitted + untracked) |
| Templates | `templates` table, `GET/POST /templates`, `PUT/DELETE /templates/<id>` |
| Security guard | `harness/api.py` middleware (below) |
| Autostart | `ops/harness/run-daemon.ps1` + `install-task.ps1` → logon task `AgentHarness-Daemon` (registered). Logs: `D:\Agents\harness\logs\` |
| Tailnet publishing | `ops/tailscale/serve.ps1`: `:443` → daemon, `:8443` → ntfy (**not applied yet**: Serve/HTTPS must be enabled for the tailnet) |
| UI screenshots | `scripts/ui-shot.mjs` drives headless Edge over DevTools (Node 24, no packages) at phone size |
| Tests | `tests/test_api.py` (web app, guard, notification payloads, token buttons, templates, changes, rerun); 27 total |

## Notifications

| Event | Notification |
| --- | --- |
| approval requested | "Approve? <title>", priority 4, click → `#/s/<id>/approval/<aid>`, actions Approve / Deny (http POST from the phone) |
| approval decided (anywhere) | replaces the same notification (ntfy `sequence_id` = approval id) with "✅ Approved" / "🚫 Denied", priority 2 |
| run finished `done` | "Done: <title>" + start of the answer, click → session |
| run `failed` / stopped by budget | "Failed: …" (priority 4) / "Stopped (budget): …" |
| cancelled | none (you did it) |

The replace-on-decision step works around ntfy iOS not dismissing a notification after an action button
(ntfy issue #1728). Whether iOS visibly replaces it still needs checking on the phone.

ntfy iOS facts checked in its source (2026-09-14): per-server username/password (Settings → Users),
action types `view` and `http` supported, `http` actions run from the phone with `URLSession`.

## Security model

- The daemon still binds `127.0.0.1`. `tailscale serve` (tailnet-only, not Funnel) terminates HTTPS with a
  tailnet certificate and adds `Tailscale-User-Login`; the daemon refuses logins not in `allowed_logins`.
- Cross-site requests: non-GET requests with a foreign `Origin` or `Sec-Fetch-Site: cross-site` are refused, so a
  web page open on a tailnet device can't drive the agent. Non-browser clients (CLI, ntfy app) send no Origin.
- Notification buttons authenticate with a 32-byte random token per approval (never returned by the API or
  written to transcripts); pressing twice is harmless; tokens only decide that one approval.
- ntfy: default deny; the daemon has a write-only token; the phone user is read-only; message text stays on the
  tower (ntfy.sh only relays a poll request for iOS).

## Verified on the tower (2026-09-14)

- Daemon under the logon task: `/health` 200; `POST /notify/test` delivered to ntfy (read back as the phone user).
- Real Qwen session "Approval demo": `rm -rf data` raised an approval → ntfy message with the correct deep link,
  Approve/Deny URLs, and sequence id → approved by clicking the card's button in headless Edge → the transcript
  streamed the next reply live, the approval card turned "approved", and the run finished with a rendered
  Markdown report.
- Screenshots at 390×844 (dark): list, approval card, new task, settings, finished session, diff viewer.
- Anonymous publish to ntfy → 403; token publish → 200.
- Bug found and fixed: on Windows, Python 3.10's `Path.resolve()` intermittently returns `\\?\C:\...` while a
  directory in the path is being created, which made `write_file` fail "path escapes the workspace" (reproduced
  3/40 in a stress loop). `tools.resolve_path` strips the prefix.

## Exit test checklist (needs you and the phone)

Prerequisites (one time):
1. Enable Tailscale Serve/HTTPS for the tower: visit the link `tailscale serve` prints (or admin console →
   DNS → HTTPS Certificates), then run `ops\tailscale\serve.ps1`.
2. In Safari on the iPhone (Tailscale on): open `https://tower.your-tailnet.ts.net`, Share → Add to Home Screen.
3. ntfy app: Settings → Users → add `https://tower.your-tailnet.ts.net:8443` with the login in
   `D:\Docker\ntfy\secrets\phone-login.txt`; Default server → same URL; subscribe to `agent-harness`.
   In the web app: Settings → Send test notification.

Exit test (away from home, on cellular):
1. Start a task from the home-screen app that will need an approval, e.g. template "Approval demo":
   *Create a file notes.txt, then delete the directory build-output with rm -rf build-output (create it first). Report.*
2. Lock the phone. An "Approve? …" notification arrives.
3. Long-press → Approve (or tap → approval card → Approve).
4. A "Done: …" notification arrives. Tap it: the session shows the answer.

Things to note while testing: does the approval notification get replaced by "✅ Approved"? Is the delay from
lock to notification acceptable? Does the web app reconnect after being backgrounded?

## Known gaps

- Nothing tested yet over real HTTPS/Tailscale from the phone (blocked on enabling Serve).
- The service worker only registers over HTTPS, so offline shell caching is untested.
- No push through the web app itself (ntfy covers notifications).
- Templates are global, not per project.
