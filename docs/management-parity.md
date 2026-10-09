# Management parity: Agent Harness Web, the owner API and the CLI

**Every owner API route, and so every Hub action, has a CLI command.** The standalone Hub and Agent Harness Web
administer the daemon only through the [owner API](admin-api.md) (`/api/admin/v1`), and installing either is
optional, so an owner who declines them can still do everything from the Agent Harness CLI (`harness`, or
`python -m harness.cli`). A new owner route ships with its row below and its CLI command (#544).

Agent Harness Web is an optional client (#334): every setting and management action it offers is also an owner API
operation with a CLI command. This page is the inventory. `tests/test_management_parity.py` reads it and fails when:

- a route mounted under `/api/admin/v1` (core or an add-on module, including `Module.admin_paths`) has no row below,
  or none of its rows has a CLI command that calls it, unless the test's short allowlist names it with a reason;
- Web calls an endpoint (`api("…")` in `harness/web`) that no row below lists;
- a row's endpoint is on the owner API but has no CLI command, or its CLI command calls something else;
- a row with no CLI command names an owner API endpoint (only App API and browser sign-in routes may);
- Web stores a `harness.*` browser key that the client-only list doesn't name.

Endpoints are relative to `/api/admin/v1` (`/` is the discovery root). `<x>` is a required argument; most commands
take more options than shown (`harness <command> --help`), and `--set key=value` or `--json '{…}'` add any body
field. The CLI authenticates like the rest of it: the paired client config's owner token, `HARNESS_TOKEN`, or the
Tailscale/localhost owner identity. Commands print the API's JSON.

The allowlist holds only the live streams Web uses to refresh and the older Web builds' `PUT` rename aliases: a chat
reply can be read with `harness chats show <ref>` once it ends (Web streams `/chats/{ref}/events`), Web's global
`/events` feed only refreshes its lists, and `PUT /sessions/{ref}` and `PUT /chats/{ref}` do what the `PATCH` the
rename commands call does. `harness watch <ref>` follows a session's own event stream.

## Sessions and review

Pages: Sessions (`pages/sessions.mjs`), New task (`pages/new-task.mjs`), Session (`pages/session.mjs`,
`lib/session-ui.mjs`).

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Session list | GET | `/sessions` | `harness list` |
| Search sessions | GET | `/search` | `harness search <q>` |
| GPU queue | GET | `/queue` | `harness queue` |
| Start a task | POST | `/sessions` | `harness new <prompt>` |
| Start a task on a GitHub issue or PR | POST | `/github/sessions` | `harness github start <project> <number> <prompt>` |
| GitHub issue and PR picker | GET | `/github/projects/{project}/items` | `harness github items <project>` |
| Picked GitHub issue or PR | GET | `/github/projects/{project}/items/{number}` | `harness github item <project> <number>` |
| Task templates | GET | `/templates` | `harness templates list` |
| Save a template | POST | `/templates` | `harness templates create <name> <prompt>` |
| Delete a template | DELETE | `/templates/{tid}` | `harness templates delete <tid>` |
| Skills to include | GET | `/skills/enabled` | `harness skills enabled` |
| Session view | GET | `/sessions/{ref}` | `harness show <ref>` |
| Follow a session live | GET | `/sessions/{ref}/events` | `harness watch <ref>` |
| Rename a session (PUT is the older servers' fallback) | PATCH, PUT | `/sessions/{ref}` | `harness sessions rename <ref> <title>` |
| Send a follow-up | POST | `/sessions/{ref}/messages` | `harness send <ref> <message>` |
| Cancel | POST | `/sessions/{ref}/cancel` | `harness cancel <ref>` |
| Rerun | POST | `/sessions/{ref}/rerun` | `harness sessions rerun <ref>` |
| Clear taint | POST | `/sessions/{ref}/taint/clear` | `harness sessions clear-taint <ref>` |
| Approve or deny a tool call (`harness deny` too) | POST | `/sessions/{ref}/approvals/{approval_id}` | `harness approve <ref> <approval_id>` |
| Rewind to a checkpoint | POST | `/sessions/{ref}/checkpoints/{turn}/rewind` | `harness sessions rewind <ref> <turn>` |
| Fork from a checkpoint | POST | `/sessions/{ref}/checkpoints/{turn}/fork` | `harness sessions fork <ref> <turn> <prompt>` |
| Changes tab (diff and secret scan) | GET | `/sessions/{ref}/changes` | `harness sessions changes <ref>` |
| Merge | POST | `/sessions/{ref}/review/merge` | `harness sessions merge <ref>` |
| Push | POST | `/sessions/{ref}/review/push` | `harness sessions push <ref>` |
| Discard | POST | `/sessions/{ref}/review/discard` | `harness sessions discard <ref>` |
| Line comments | GET | `/sessions/{ref}/review-comments` | `harness sessions comments <ref>` |
| Draft a line comment | POST | `/sessions/{ref}/review-comments` | `harness sessions comment <ref> <path> <side> <start_line> <comment>` |
| Delete a drafted comment | DELETE | `/sessions/{ref}/review-comments/{comment_id}` | `harness sessions comment-delete <ref> <comment_id>` |
| Send the drafted comments | POST | `/sessions/{ref}/review-comments/send` | `harness sessions comments-send <ref>` |
| Fix secret findings | POST | `/sessions/{ref}/secret-findings/fix` | `harness sessions secrets-fix <ref>` |
| Dismiss a secret finding | POST | `/sessions/{ref}/secret-findings/{fingerprint}/dismiss` | `harness sessions secret-dismiss <ref> <fingerprint> <reason>` |

## Chat

Pages: Chat (`pages/chat.mjs`), including its recent chats list.

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Recent chats | GET | `/chats` | `harness chats list` |
| Backend and model choices | GET | `/chats/options` | `harness chats options` |
| Open a chat | GET | `/chats/{ref}` | `harness chats show <ref>` |
| Start a chat | POST | `/chats` | `harness chats new <prompt>` |
| Send a message | POST | `/chats/{ref}/messages` | `harness chats send <ref> <content>` |
| Stop a reply | POST | `/chats/{ref}/cancel` | `harness chats cancel <ref>` |
| Run a code snippet | POST | `/chats/{ref}/snippets` | `harness chats run <ref> <language> <source>` |
| Stop a snippet | POST | `/chats/{ref}/snippets/{run_id}/cancel` | `harness chats run-cancel <ref> <run_id>` |
| Rename a chat | PATCH | `/chats/{ref}` | `harness chats rename <ref> <title>` |
| Delete a chat | DELETE | `/chats/{ref}` | `harness chats delete <ref>` |

## Images

Pages: Images (`pages/images.mjs`); leaving the page cools the model down (`lib/router.mjs`).

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Gallery | GET | `/images` | `harness images list` |
| Image job | GET | `/images/{iid}` | `harness images show <iid>` |
| Generate | POST | `/images` | `harness images create <prompt>` |
| Upload a source image | POST | `/images/uploads` | `harness images upload <file>` |
| Repaint inside a mask | POST | `/images/{iid}/edit` | `harness images edit <iid> <prompt> <mask>` |
| Upscale | POST | `/images/{iid}/upscale` | `harness images upscale <iid>` |
| Cancel | POST | `/images/{iid}/cancel` | `harness images cancel <iid>` |
| Delete | DELETE | `/images/{iid}` | `harness images delete <iid>` |
| Warm the image model | POST | `/images/warmup` | `harness images warmup` |
| Cool the image model down | POST | `/images/cooldown` | `harness images cooldown` |

## Actions: resources, GPU and models

Pages: Actions → Resources (`pages/actions.mjs`), the model warm-up on New task and Chat (`lib/warm-model.mjs`).

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Resource guard and GPU hold | GET | `/resources` | `harness resources status` |
| Diagnostics | GET | `/resources/diagnostics` | `harness resources diagnostics` |
| Load the local model now | POST | `/resources/load` | `harness resources load` |
| Unload the local model | POST | `/resources/unload` | `harness resources unload` |
| GPU hold on | POST | `/resources/pause` | `harness resources pause` |
| GPU hold off | POST | `/resources/resume` | `harness resources resume` |
| GPU state (several pages) | GET | `/gpu` | `harness gpu status` |
| Warm the local model | POST | `/models/warm` | `harness models warm` |
| Local model state | GET | `/models/status` | `harness models status` |
| Local models | GET | `/models` | `harness models list` |

## Actions: Remote Control

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Folders and sessions | GET | `/remote-control` | `harness remote-control list` |
| Start | POST | `/remote-control/{project}` | `harness remote-control launch <project>` |
| Stop | POST | `/remote-control/{project}/stop` | `harness remote-control stop <project>` |
| Trust a folder | POST | `/remote-control/{project}/trust` | `harness remote-control trust <project>` |
| Remove a discovered folder | DELETE | `/remote-control/folders/{slug}` | `harness remote-control forget <slug>` |
| Scan for folders | POST | `/remote-control/discovery/scans` | `harness remote-control scan` |
| Scan progress | GET | `/remote-control/discovery/scans/{scan_id}` | `harness remote-control scan-show <scan_id>` |
| Cancel a scan | DELETE | `/remote-control/discovery/scans/{scan_id}` | `harness remote-control scan-cancel <scan_id>` |
| Add a scanned folder | POST | `/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote` | `harness remote-control promote <scan_id> <candidate_id> <slug> <confirmed_path> <marker>` |

## Actions: maintenance and runners

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Disk usage and maintenance state | GET | `/maintenance` | `harness maintenance status` |
| Clean up | POST | `/maintenance/cleanup` | `harness maintenance cleanup` |
| Image-archive retention preview | POST | `/maintenance/image-archive/retention/preview` | `harness maintenance image-retention-preview` |
| Apply image-archive retention | POST | `/maintenance/image-archive/retention/apply` | `harness maintenance image-retention-apply <confirmation>` |
| Runners (also on New task and Settings) | GET | `/runners` | `harness runner list` |
| Update a runner | POST | `/runners/{name}/update` | `harness runner update <name>` |
| Projects (several pages) | GET | `/projects` | `harness projects list` |
| Add a project | POST | `/projects` | `harness projects create <name>` |

## Actions: household members

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Members | GET | `/accounts` | `harness accounts list` |
| Add a member | POST | `/accounts` | `harness accounts add <login> <display_name>` |
| Rename, rebind, disable or re-enable, limits | PATCH | `/accounts/{user_id}` | `harness accounts update <user_id>` |
| Erase a member's GitHub credential | POST | `/accounts/{user_id}/github-connection/reset` | `harness accounts github-reset <user_id> --confirm` |
| Members may connect GitHub | GET | `/github-member-auth` | `harness github-member-auth show` |
| Allow or stop members connecting GitHub | PUT | `/github-member-auth` | `harness github-member-auth set <enabled>` |
| Google sign-in state | GET | `/google-signin` | `harness google-signin status` |
| Google link code | POST | `/accounts/{user_id}/google/invitation` | `harness accounts google-invite <user_id>` |
| Cancel a link code | DELETE | `/accounts/{user_id}/google/invitation` | `harness accounts google-cancel-invite <user_id>` |
| Revoke a member's Google Web sessions | POST | `/accounts/{user_id}/google/revoke-sessions` | `harness accounts google-revoke-sessions <user_id>` |
| Unlink Google | DELETE | `/accounts/{user_id}/google` | `harness accounts google-unlink <user_id> --confirm` |

## Settings and profile

Pages: Settings (`pages/profile.mjs` and its subpages), `app.js`.

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Identity | GET | `/me` | `harness me` |
| Profile | GET | `/profile` | `harness profile show` |
| Profile icon | PUT | `/profile` | `harness profile set-icon <emoji>` |
| Connection: API version and capabilities | GET | `/` | `harness capabilities` |
| Test notification | POST | `/notify/test` | `harness notify test` |
| Smart approvals | GET | `/smart-approvals` | `harness smart-approvals show` |
| Smart approvals mode | PUT | `/smart-approvals` | `harness smart-approvals set <mode>` |
| Backends (`?auth=skip` skips sign-in checks) | GET | `/backends` | `harness backends list` |
| Backend default model and effort | PUT | `/backends/{name}` | `harness backends set <name>` |

## Settings: daemon settings

Page: `pages/daemon-settings.mjs`.

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Settings and revision | GET | `/config` | `harness config show` |
| Save or dry-run changes | PATCH | `/config` | `harness config set <key>=<value>` |
| Roll back | POST | `/config/rollback` | `harness config rollback --confirm` |
| Restart to apply | POST | `/config/restart` | `harness config restart --confirm` |

## Settings: skills and memory library

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Skills and proposals | GET | `/skills` | `harness skills list` |
| Proposal | GET | `/skills/proposals/{pid}` | `harness skills proposal <pid>` |
| Install at the reviewed hash | POST | `/skills/proposals/{pid}/install` | `harness skills install <pid> <content_hash>` |
| Reject | POST | `/skills/proposals/{pid}/reject` | `harness skills reject <pid>` |
| Run the hosted review | POST | `/skills/proposals/{pid}/review` | `harness skills review <pid>` |
| Delete a proposal | DELETE | `/skills/proposals/{pid}` | `harness skills delete-proposal <pid>` |
| Enable | POST | `/skills/{slug}/enable` | `harness skills enable <slug>` |
| Disable | POST | `/skills/{slug}/disable` | `harness skills disable <slug>` |
| Project allowlist | PUT | `/skills/{slug}/projects` | `harness skills projects <slug> <project>` |
| Roll back | POST | `/skills/{slug}/rollback` | `harness skills rollback <slug>` |
| Uninstall | POST | `/skills/{slug}/uninstall` | `harness skills uninstall <slug>` |
| Memory library | GET | `/memory` | `harness memory show` |
| Agent profile | PUT | `/memory/profile` | `harness memory set-profile <content>` |

## Settings: Apps, keys and pairing

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Apps and keys | GET | `/keys` | `harness keys list` |
| Create a key or Web connection token | POST | `/keys` | `harness keys create <name>` |
| Revoke a key | DELETE | `/keys/{kid}` | `harness keys revoke <kid>` |
| Restore a revoked App before erasure | POST | `/apps/{app_id}/restore` | `harness apps restore <app_id>` |
| App pairing codes | GET | `/pairing-codes` | `harness pairing-codes list` |
| Make an App pairing code | POST | `/pairing-codes` | `harness pairing-codes create <name> <origin>` |
| Revoke an App pairing code | DELETE | `/pairing-codes/{pid}` | `harness pairing-codes revoke <pid>` |
| Mac pairing codes | GET | `/runner-pairing-codes` | `harness runner-pairing-codes list` |
| Make a Mac pairing code | POST | `/runner-pairing-codes` | `harness runner-pairing-codes create` |
| Revoke a Mac pairing code | DELETE | `/runner-pairing-codes/{pid}` | `harness runner-pairing-codes revoke <pid>` |

## Jobs

Page: `pages/jobs.mjs`.

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Jobs | GET | `/jobs` | `harness jobs list` |
| Job | GET | `/jobs/{jid}` | `harness jobs show <jid>` |
| Create | POST | `/jobs` | `harness jobs create <name> <prompt> <cron>` |
| Save | PUT | `/jobs/{jid}` | `harness jobs update <jid> <name> <prompt> <cron>` |
| Delete | DELETE | `/jobs/{jid}` | `harness jobs delete <jid>` |
| Run now | POST | `/jobs/{jid}/run` | `harness jobs run <jid>` |
| Cron preview | GET | `/jobs/preview` | `harness jobs preview <cron>` |

## Owner API operations (Hub and CLI)

Web has no page for these; like every owner route, each has a CLI command.

| Action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Household audit log | GET | `/accounts/audit` | `harness accounts audit` |
| Retained audit log (cursor, filters) | GET | `/audit` | `harness audit list` |
| One member | GET | `/accounts/{user_id}` | `harness accounts show <user_id>` |
| Apps waiting to be erased | GET | `/apps/erasures` | `harness apps erasures` |
| App retention | PUT | `/apps/{app_id}/retention` | `harness apps retention <app_id>` |
| Per-App provider credentials | GET | `/provider-credentials` | `harness provider-credentials list` |
| Set a provider credential | POST | `/provider-credentials` | `harness provider-credentials set <app_id> <backend>` |
| Revoke a provider credential | DELETE | `/provider-credentials/{credential_id}` | `harness provider-credentials revoke <credential_id>` |
| Settings registry | GET | `/config/schema` | `harness config schema` |
| Validate settings changes | POST | `/config/validate` | `harness config validate <key>=<value>` |
| Back up | POST | `/maintenance/backup` | `harness maintenance backup` |
| GPU hold (older paths) | POST | `/gpu/pause` | `harness gpu pause` |
| GPU release (older paths) | POST | `/gpu/resume` | `harness gpu resume` |
| Reopen a rejected proposal | POST | `/skills/proposals/{pid}/reopen` | `harness skills reopen <pid>` |
| Export a skill | GET | `/skills/{slug}/export` | `harness skills export <slug>` |
| Replace a template | PUT | `/templates/{tid}` | `harness templates update <tid> <name> <prompt>` |
| Session approvals | GET | `/sessions/{ref}/approvals` | `harness sessions approvals <ref>` |
| Session checkpoints | GET | `/sessions/{ref}/checkpoints` | `harness sessions checkpoints <ref>` |
| Session context-efficiency metrics | GET | `/sessions/{ref}/metrics` | `harness sessions metrics <ref>` |
| Session transcript | GET | `/sessions/{ref}/transcript` | `harness transcript <ref>` |
| Snippet languages | GET | `/chats/snippet-languages` | `harness chats snippet-languages` |

## Not on the owner API: a member's own account and browser sign-in

Web shows these to household members and to Google-signed-in browsers. They act on the caller's own account or
browser cookie session over the App API (`/api/v1`), so they aren't server management and have no owner command.
The owner manages members' Google links and GitHub credentials with `harness accounts google-*` and
`harness accounts github-reset`.

| Web action | Method | Endpoint | CLI |
| --- | --- | --- | --- |
| Member's Google sign-in card | GET, DELETE | `/me/google` | — the member's own Google link |
| Member's GitHub connection | GET, DELETE | `/me/github-connection` | — the member's own GitHub credential |
| Member connects GitHub | POST | `/me/github-connection/connect` | — the member's own GitHub credential |
| Member cancels connecting GitHub | POST | `/me/github-connection/cancel` | — the member's own GitHub credential |
| Member's API keys | GET | `/me/api-keys` | — the member's own provider keys (last four characters only) |
| Member saves, replaces or deletes an API key | PUT, DELETE | `/me/api-keys/{backend}` | — the member's own provider key |
| Member tests an API key | POST | `/me/api-keys/{backend}/test` | — the member's own provider key |
| Sign-in state | GET | `/auth/session` | — browser cookie session |
| Sign in with Google | POST | `/auth/google/start` | — browser cookie session |
| Sign out | POST | `/auth/logout` | — browser cookie session |

## Client-only: browser preferences

Stored in the browser's `localStorage` and read only by that browser. None of them is server state.

| Key | What |
| --- | --- |
| `harness.theme` | Light, dark or system theme |
| `harness.themeHues` | Theme colors |
| `harness.textSize` | Text size |
| `harness.appIcon` | Home-screen icon choice |
| `harness.sessionTarget` | Sessions list filter (all, tower, Mac) |
| `harness.target` | New task's last target |
| `harness.chatChoice` | Chat's last backend and model |
| `harness.draft` | Unsent New task draft |
| `harness.imageDraft` | Unsent image prompt draft |
| `harness.daemonUrl` | Separately hosted Web: the server's URL |
| `harness.ownerToken` | Separately hosted Web: its owner token (made with `harness keys create`) |
| `harness.webUpdateAttempt` | Web update reload guard |
| `harness.webUpdatePrompt` | Web update prompt dismissal |
| `harness.lastRole` | Last signed-in role (owner or member only), so an offline launch keeps the app shell |
