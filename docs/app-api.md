# Agent Harness App API (v1)

An **Agent Harness App** is a third-party integration that starts agent sessions, gives them context, lends them
tools, and follows their progress. Base path: `/api/v1`, on Agent Harness Server's address
(`http://127.0.0.1:8100` locally, `https://<pc>.<tailnet>.ts.net` on a tailnet). FastAPI also serves the
machine-readable schema at `/openapi.json`. Machine-owner operations (schedules,
GPU, review/push, token management, maintenance, Remote Control trust) live on [`/api/admin/v1`](admin-api.md) and
are not part of this App contract. App tokens cannot call them.

First-party [Agent Harness Web](web.md) also uses this surface for ordinary session operations. A same-origin bundled
Web UI may use its Tailscale/localhost owner identity; a separately hosted copy uses an origin-bound owner token.
Owner-created sessions are not assigned to an Agent Harness App. This first-party privilege does not change App-token
scoping: App tokens still see only their own sessions unless granted read-only `sessions:all`, which adds the owner's
sessions. The owner never sees an App's sessions, and no App sees another App's (see
[Where an App's data lives](#where-an-apps-data-lives)).

A one-file Agent Harness SDK lives in [`sdk/harness_client.py`](../sdk/harness_client.py) (requires `httpx`), with an
example in [`sdk/examples/shopping_list_app.py`](../sdk/examples/shopping_list_app.py).

Agent Harness SDK is the supported Python surface. It covers discovery and pairing, every session backend, initial and
incremental context, app tools, approvals, cancellation, resumable events, provider status, and images. Call
`Harness.validate_openapi()` during an integration check to verify that its operation and request types still match
Agent Harness Server's live `/openapi.json`; the repository test suite performs the same check against every change.

`GET /api/v1/backends` is authenticated and app-specific. Its `today`, `week`, and `usage_by_source` fields contain
only the calling app's usage. `provider_policy` reports whether that app may use the backend, its billing policy and
model allowlist, and whether the assigned credential source is available. It never contains a provider key, key-file
path, or owner-only opaque reference. The unauthenticated discovery document at `GET /api/v1` does not list project
names (`projects` is always `[]`); an authenticated principal enumerates usable projects at `GET /api/v1/projects`.
It likewise does not report key-file availability or usage.

Machine-wide cached provider-limit data is also hidden from managed apps because it could belong to a different
assignment. A managed app receives limits reported by its own provider process in that session's `rate_limit` events.

## Tokens and scopes

Create an Agent Harness App token in **Settings → Apps** (or `POST /keys` from the PC:
`{"name": "my-app", "kind": "app", "scopes": ["sessions"]}`). It's shown once; only its hash is stored. Send it as
`Authorization: Bearer ha-...`.

| Scope | Allows |
| --- | --- |
| `sessions` | create sessions, send messages and context, answer tool calls, cancel; read the app's own sessions and events |
| `sessions:all` | also read the owner's sessions (never another App's, nor other household accounts') |
| `approvals` | approve or deny tool calls in the app's own sessions (normally the user approves from the phone) |
| `images` | generate and download images |
| `inference` | use the OpenAI/Anthropic-compatible endpoint under `/v1` |
| `remote_control` | start and stop Claude Code Remote Control servers in project folders |
| `models:warm` | start loading the local model ahead of a chat (`POST /api/v1/models/warm`); request it at pairing |

Apps see only the sessions they created, unless they hold `sessions:all`. That scope expands reads only:
sending messages, adding context, cancelling, and answering tool calls still require owning the session.
`sessions:all` means the owner's own sessions (`user_id = owner`, started by the owner rather than by an App), never
another App's sessions and never household member accounts. Cross-user and cross-App object ids return an
indistinguishable 404. Errors are `{"detail": "..."}`, with 401
(bad token), 403 (missing scope), 404 (not found or not yours), 400/409/413 as usual. Harness-generated errors also
include `error: {code, message, retryable}`; `detail` remains for compatibility. The SDK exposes these as
`HarnessError.code`, `.detail`, and `.retryable`.

## App configuration

`GET /api/v1/config/schema`, `GET /api/v1/config`, and `PATCH /api/v1/config` require a live app token. They return
only that app's registered settings and effective caps. Owner, device, runner, guest, and anonymous credentials
cannot impersonate this surface. The allowlist and cap rules are in [`config-registry.md`](config-registry.md).

## Where an App's data lives

Each App has its own SQLite store and data folder, `<data_dir>/apps/<app_id>/harness.sqlite3`, created the first time
the App's token starts or reads a session. An App's sessions (agent and App-tools-only), their events, tool calls and
results, approvals, artifacts, checkpoints, review drafts and search index are written only to that store. The main
store (`<data_dir>/harness.sqlite3`) has no row of them. Another App's store has none either. The main store keeps
the App registry (ids, token hashes, scopes), the per-App usage counters, provider credentials, settings and
everything global (jobs, schedules, skills, the memory library, accounts, the audit log), and no sessions at all.
Session ids stay unique across all stores. The `/api/v1` responses are the same as before.

- **Agent Harness Web is an App too** (#330 decision 4). Its store, `<data_dir>/apps/app-web/harness.sqlite3`, holds
  the owner's and members' sessions (every session no App started), and the owner reads them through Web, their own
  App. Web's id `app-web` is reserved: it is registered in the App registry without a token (the owner's Tailscale
  and Google identity maps to it), it isn't listed by `GET /keys`, and it can't be revoked, erased or given a
  retention. Your App never reads Web's store unless the owner grants it the read-only `sessions:all`; a query about
  your own sessions never touches it. Members stay apart from the owner and from each other inside it, as before.

- **Same schema.** Every store runs the same versioned migrations as the main store
  ([`migrations.md`](migrations.md)) and has its own writer thread and read pool (#294).
- **Opened lazily, closed when idle.** A store opens on first use and closes after 5 minutes without one
  (`APP_STORE_IDLE_SECONDS` in `harness/app_stores.py`): its writer thread stops and its read connections close. The
  next call opens it again. At startup the daemon reads only the session ids from each store, without opening it.
- **Moving older App sessions.** App sessions written before the per-App stores existed are moved at the first start
  after the upgrade. The daemon first copies the main store to
  `<data_dir>/pre-migration/harness-app-stores-<time>.sqlite3`. It then copies each App's sessions, with everything
  tied to them, into that App's store and deletes them from the main store. Later starts find nothing to move and
  make no backup.
- **Files.** An App session's files live in the App's folder too: its working directory in
  `<data_dir>/apps/<app_id>/workspaces/<session id>/`, its transcript in `transcripts/`, its checkpoint snapshots in
  `checkpoints/`, with `artifacts/` alongside. They never count toward the owner's or a member's disk use. Files of
  App sessions made before this moved there at the first start after the upgrade, after a backup of the App's store
  to `<data_dir>/apps/<app_id>/pre-migration/harness-app-files-<time>.sqlite3` (the stored working-directory paths
  change with them). Usage rows and short-lived event-stream tickets stay in the main store: they are metadata.
- **Your App alone.** Only your App's token reaches your sessions. The owner's session lists, session pages
  (a 404, even by id or id prefix), transcript and session search, `session_search` and `session_read`, pending
  approvals, the queue and the live session list never read your store. Neither do other Apps, including ones with
  `sessions:all`, nor memory or skill extraction. Decide approvals in your sessions with the `approvals` scope: the
  owner's Web no longer shows them.
- **What the owner sees instead.** Metadata only, on the Apps card (`GET /keys`, a `store` object per App): your
  sessions counted by status, your hosted-backend usage (requests, tokens, cost) and how many of your sessions
  failed, with the last failure's kind (the stop reason up to its first colon, without its details) and time.
- **Owner totals.** `/metrics`, the smart-approval stats and Control Center's counts cover the owner's and members'
  sessions only (Web's store); App sessions are counted only in the per-App metadata above.
- **Backups.** The nightly backup copies every App's store into the dated backup folder as `apps/<app_id>.sqlite3`,
  one file per App (Web's as `apps/app-web.sqlite3`), next to the main store's `harness.sqlite3`. A session you
  delete, or that retention or a revoke erases, stays in the older backups that hold it until they rotate out
  (`backup.keep_days`, 14 days by default). Session files (working directories, checkpoints) are not in the backups.
- **Logs and telemetry.** Your tools' arguments and results stay in your store: logs, traces and the audit log get
  only tool names, call ids, sizes and timings.
- **Retention.** A session is erased, exactly as [`DELETE`](#delete-apiv1sessionsid) erases it, once it has been
  idle for its `retention_days` (set at [create](#post-apiv1sessions--scope-sessions)), or else your App's default
  retention, which the owner sets. Idle means since its last event, or since it was created. Without either it is
  kept until you delete it. The daemon's maintenance sweep (every `cleanup.interval_minutes`, hourly by default)
  does this whether or not your App is online.
- **Revoking an App.** The owner's revoke kills your token at once and schedules the erasure of your whole store and
  folder 7 days later. During those 7 days the owner can undo the revoke: your store and files are kept, and the
  owner gives you a new token (the old one stays dead). After them the sweep erases the store and folder; only a
  tombstone (your App's id, name and dates, no scopes) stays in the registry, with your settings, provider
  credentials and error counts gone. An undo doesn't bring back your App's own settings (`/api/v1/config`): set them
  again.

## Household members on `/api/v1`

An enabled Tailscale member is a human principal on the same-origin `/api/v1` surface. `GET /api/v1/me` returns
`role`, opaque `user_id`, and usage. Members may create empty tower projects or clone credential-free public HTTPS
repositories from github.com, gitlab.com, or codeberg.org into their own managed area (`POST /api/v1/projects`,
ambient Tailscale human only — never an app token). They create, list, steer, cancel, approve, and review only
their own local-model tower sessions, search only their own transcripts, and receive only their own live events.

When the owner has turned on member GitHub sign-in ([`member-github-auth.md`](member-github-auth.md)), an
ambient same-origin member (never a bearer token) manages **their own** connection:
`GET /api/v1/me/github-connection` returns `status` (`disconnected | connecting | connected | reconnect_required |
disabled`), the device-flow `deadline`/`seconds_left`, `last_used_at`, and a sanitized `error`, plus `prompt`
(`verification_uri`, `user_code`) only while that member's own attempt is live. `POST .../connect` starts or
resumes the attempt, `POST .../cancel` cancels it, and `DELETE /api/v1/me/github-connection` disconnects (erases).
`POST /api/v1/projects` with `"github": true` clones a `https://github.com/<owner>/<repo>` URL with the member's
own connection. It answers `not_connected` or `reconnect_required` (409) when the member must connect first.

Members cannot use hosted-provider subscriptions, owner/app/device tokens, Mac runners, homelab or memory-library
tools, image generation, Remote Control, inference keys, scheduled jobs, app management, notifications, backups,
or another user's data. Those capabilities are forced off in service/tool construction, not only in the UI.

App tokens remain owner-managed: they cannot act as a member, mint member credentials, or attach sessions to a
member. Device and runner tokens gain no member authority.

## Capability matrix

| Capability | owner | member | guest | app token | device/runner |
| --- | --- | --- | --- | --- | --- |
| Own sessions (create/list/steer/cancel/review) | yes | yes (local tower only) | read-only look around | own sessions, plus the owner's with `sessions:all` | inference only; no member sessions |
| `/api/v1/me`, scoped projects/search/events | yes | own account | no | owner scope | no |
| Create projects | yes | empty or public HTTPS allowlist | no | no | no |
| `/api/admin/v1`, `ho-` owner tokens | yes | 403 | 403 | 403 | 403 |
| App-tools-only sessions (`tools_only`) | no | no | no | own only, never `sessions:all` | no |
| Hosted backends, images, jobs, runners, Remote Control | yes | no | no | scopes for images/remote_control only | no |
| Homelab, memory library, notifications, backups, keys | yes | no | no | no | no |
| Member prompts, transcripts, diffs, repo contents | no (aggregate metadata only) | own only | no | no | no |

The machine owner remains inside the host/OS trust boundary and can read local storage. This matrix is about
accidental or API/UI cross-account access, not a hostile administrator.

## Pair a separately hosted browser app

Do not paste a long-lived App token into a browser URL. In **Settings → Apps → Pair browser app**, the owner enters
the Agent Harness App's exact origin (for example `https://app.example`) and scopes, then approves a bootstrap code. The
code expires after 10 minutes, works once, and can only be redeemed from that exact `Origin`. Browser origins must
use HTTPS; HTTP is accepted only for loopback development (`localhost`, `127.0.0.1`, or `[::1]`):

```js
const paired = await fetch(`${daemon}/api/v1/pair`, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({code}),
}).then((r) => r.json());
const token = paired.token;
```

The resulting `ha-...` credential is scoped, revocable, and bound to the approved origin. It is an Agent Harness App
credential, never a Claude, Codex, Cursor, or other provider credential. Keep it out of URLs and logs; send it in
`Authorization: Bearer ...`. Agent Harness Server emits `Access-Control-Allow-Origin` only for the exact paired origin, never
uses wildcard CORS, and does not allow credentials/cookies. This means ordinary browser mutations cannot ride ambient
cookies as CSRF. A non-browser Agent Harness App can continue to use the same bearer API without an `Origin` header.

For native `EventSource`, first mint a short-lived stream ticket with the bearer credential:

```js
const stream = await fetch(`${daemon}/api/v1/sessions/${sid}/events/ticket`, {
  method: "POST",
  headers: {Authorization: `Bearer ${token}`},
}).then((r) => r.json());
const events = new EventSource(`${daemon}${stream.events_url}`);
```

The ticket is valid for 60 seconds to establish or briefly reconnect the stream, and is bound to the app, session,
and origin. It contains no bearer/provider credential, is stored only as a hash, and stops working immediately if the
app is revoked. Track the latest event `seq`. After a long iOS suspension, close the old `EventSource`, mint a fresh
ticket, and reconnect with `&after=<last-seq>`; normal short reconnects also resume via `Last-Event-ID`.

## Quick start (Python)

```python
from harness_client import Harness, tool

notes = []

@tool("Save a note for the user", text={"type": "string"})
def save_note(text: str) -> str:
    notes.append(text)
    return "saved"

h = Harness("http://127.0.0.1:8100", token="ha-...")
result = h.run("Read the context and save the three most important follow-ups as notes.",
               context={"Meeting transcript": open("meeting.txt").read()},
               tools=[save_note])
print(result.status, result.answer, notes)
```

`Harness.pair(url, code, origin)` redeems an owner-approved browser pairing code. `capabilities()` and `backends()`
discover what Agent Harness Server can run; pass `backend="claude"`, `"codex"`, or `"cursor"` to `run()` / `create_session()`
instead of the default `"local"`. `pending_approvals()` / `decide_approval()` expose native provider permission
requests, while `RunResult.usage`, `.limits`, `.billing_notices`, `.errors`, and `.failure` normalize run outcomes.

## Endpoints

### `GET /api/v1`
Server info: API version, scopes, projects, models, hosted backends, enabled features, `capabilities`, and
`image_modes` (labels, availability, and setup text for optional Lightning `quality-fast` and FLUX `flux-fast`). The capability object
identifies the `full` or `service` profile, always-on Server facilities, and effective optional modules. It contains
no credentials and doesn't need a token. `GET /health` exposes the same capability object for lightweight discovery
plus first-party release/protocol compatibility and update hints; see [`compatibility.md`](compatibility.md).

### `POST /api/v1/sessions`  (scope `sessions`)

```json
{
  "prompt": "When does order A-17 ship?",
  "project": "scratch",
  "backend": "local",
  "model": null,
  "title": null,
  "context": [{"title": "Customer", "content": "Dana, premium plan"}],
  "tools": [{
    "name": "lookup_order",
    "description": "Look up an order by id",
    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    "timeout_seconds": 600
  }],
  "metadata": {"ticket": 991}
}
```

Returns the session (`id`, `status`, `app_tools`, `metadata`, `answer`, token totals, ...).

- **Context** blocks are added to the session's system prompt under a note that they come from your app and are
  information, not instructions from the user. At most 60,000 characters in total.
- **Tools:** up to 16. Names match `^[a-zA-Z][a-zA-Z0-9_]{2,48}$` and can't reuse a built-in name. `parameters` is
  a JSON Schema object; the agent's arguments are validated against its property types before your app sees them.
  App tools don't need approval.
  A tool field the API doesn't know (a typo, or Anthropic's `input_schema`) is a `422` at session create naming the
  field, with a hint to use `parameters`; the tool is never registered as a no-argument tool.
- `project` must exist in the harness's `projects.yaml` (`GET /api/v1` lists them). Omitted, it is `scratch`.
- `tools_only: true` starts an [App-tools-only session](#app-tools-only-sessions) instead of an agent session.
- `retention_days` (optional, a positive number of days, fractions allowed) erases the session once it has been idle
  that long, as `DELETE` does; without it your App's default retention applies (see
  [Where an App's data lives](#where-an-apps-data-lives)).
- `backend` is `local` (the tower model) or a hosted CLI id such as `claude`, `codex`, or `cursor`
  (`GET /api/v1` lists enabled backends). Hosted sessions use the user's own subscription login.

### App-tools-only sessions

An App-tools-only session is a conversation on the chosen backend whose only tools are the ones your App sent with
it: a read-only Q&A bot over your App's data, not a coding agent. Start one with `"tools_only": true`:

```json
{"prompt": "How much is in checking?", "backend": "local", "tools_only": true,
 "tools": [{"name": "get_balance", "description": "Balance of one account",
            "parameters": {"type": "object", "properties": {"account": {"type": "string"}}}}]}
```

Requirements: an App token, at least one tool, and no `project` (sending one is a 400). Nothing in `projects.yaml`
is needed or used: no project instructions, rules, skills, or repository.

What the server guarantees, enforced rather than prompted:

- **Only your tools.** The model is offered your tools and nothing else. On `local` the schemas sent to the model are
  exactly your tools; the harness's file, shell, edit, web, memory, image, session-search, skill and loop-control
  tools are absent. On `claude`, Claude Code starts with `--tools ""` (no built-in tools: no Bash, Read, Edit,
  WebFetch, Task, ...), slash commands and skills off, and only your tools on its harness MCP server.
- **Everything else is denied, never asked about.** The session's policy allows exactly your tool names (natively,
  or as `mcp__harness__<name>` from Claude Code) and denies every other call outright. A model that tries a built-in
  tool anyway gets an error result, the call shows in the events as a `tool_call` with `decision: "deny"`, and it
  never runs. There are no approval prompts in these sessions.
- **No workspace.** There is no repository. Where a hosted CLI insists on a working directory it gets an empty one
  under the harness's workspaces folder, removed when each run ends (made again, empty, for a follow-up message).
- **Visible to your App alone.** Only the App that started the session can read it, list it, stream its events or
  send to it. It is hidden from the owner's session lists, from session search, and from other Apps (including ones
  with `sessions:all`). Like your other sessions it lives in your App's own store.
- **Untrusted results.** Your tools' results count as untrusted content for the session's taint (they may carry
  free text such as bank transaction descriptions). It changes nothing today, since the session has no risky tools.
- It can't be rerun (`POST .../rerun` is a 409); start a new session with its tools instead.

Backends: `local` and `claude` (with its MCP server on, the default). `GET /api/v1` lists them in
`features.app_tools_only_backends`, and each `GET /api/v1/backends` entry has `app_tools_only: true|false`, so your
App can show which backends can run its bot. Any other backend (`codex`, `cursor`) refuses at create time with a 400
whose `error.code` is `app_tools_only_unsupported`; it never runs with its built-in tools. A session that can't get
its tools to the CLI at run time fails with `failure.code` `app_tools_only_unsupported`.

Local model state:

- `GET /api/v1/models/status` (scope `sessions`) returns each model's `state` (`ready`, `sleeping`, `waking`,
  `unloaded`, `paused`, `unreachable`) and `waking_seconds`. Use it to show "loading" during a live chat.
- `POST /api/v1/models/warm` (scope `models:warm`) starts loading the local model, for example when your user starts
  typing. It respects the server's guards and is refused, not queued: `409 gpu_held` while the GPU guard has the GPU,
  `409 low_memory` when RAM is short. Otherwise it returns `{"name", "state"}` with the state before warming.
- **Background work:** check `/models/status` first and run only when the model is already `ready` (or in your own
  quiet window). Don't call `/models/warm` or start a local session from a background job: that wakes the model and
  takes RAM and GPU from whatever the owner is doing.

### `GET /api/v1/sessions`, `GET /api/v1/sessions/{id}`
List (newest first, `?limit=`) or read. Statuses: `queued`, `running`, `waiting_approval`, `waiting_target`,
`waiting_app`, `done`, `failed`, `cancelled`.

Every provider uses the same session totals (`turns`, `prompt_tokens`, `completion_tokens`, `total_cost_usd`). A
failed session has `failure: {code, provider, message, retryable}`. Provider startup/transport failures use
`provider_unavailable`, missing provider credentials use `provider_auth_required`, and a rejected provider turn uses
`provider_error`. Local model, workspace, quota, and internal failures use the same object with their corresponding
codes. The original `stop_reason` remains for compatibility. `rate_limit`, `billing_warning`, and `error` events use
provider-neutral envelopes; provider-specific raw limit fields may be included additively in `data`.

### `GET /api/v1/sessions/{id}/events?after=0&follow=true`
Server-sent events. Each event has `seq` (resume with `after=` or `Last-Event-ID`), `type`, `ts`, and `data`. Token
deltas (`delta`) and queue moves have `seq: null` and aren't replayed. Types an app usually handles:

| Type | Data |
| --- | --- |
| `app_tool_call` | `call_id`, `name`, `args`, `timeout_seconds`: run your tool and post the result |
| `assistant` | `content`, `reasoning`, `tool_calls`, token counts |
| `tool_result` | `name`, `ok`, `output` (built-in and app tools) |
| `approval_requested` | `id`, `tool`, `args`, `reason`: the user (or an app with `approvals`) must decide |
| `status` | `status`, and for the end `stop_reason`, `answer` |
| `run_finished` | the run ended; the session may continue if you send a message |

Persisted events are committed and delivered in increasing `seq` order. A stream subscribes before replaying the
database and suppresses duplicate sequence numbers, so events committed during reconnect are not lost. `after=N`
replays exactly persisted events with `seq > N`; reconnect with the largest sequence actually processed. Ephemeral
events (`seq: null`) are best-effort UI hints and may be missed or repeated across reconnects. Within one run the
usual durable order is `session_created` / `user_message`, status changes, assistant/tool/approval events, a terminal
`status`, then `run_finished`. A follow-up begins another status-to-`run_finished` run in the same session.

### `GET /api/v1/sessions/{id}/tool_calls?status=pending`
Calls waiting for your app. Use it after a reconnect instead of relying on events alone.

### `POST /api/v1/sessions/{id}/tool_calls/{call_id}`  (only the app that registered the tool)

```json
{"output": "ships Friday", "ok": true}
```

`ok: false` reports an error to the agent. Output up to 200,000 characters. A second answer gets 409. An app that
answers within 3 s keeps the session on the GPU; slower answers let other sessions run meanwhile (`waiting_app`). A
call not answered within its `timeout_seconds` fails with an error the agent sees.

### `POST /api/v1/sessions/{id}/messages`
`{"content": "..."}`. Delivered before the agent's next step, or starts a new run if the session had finished.
Requires owning the session (or an owner token, for the owner's own sessions); `sessions:all` does not authorize this.

### `POST /api/v1/sessions/{id}/context`
`{"context": [{"title": "...", "content": "..."}]}`. Same as a message, but marked as context from the app.
Requires owning the session (or an owner token, for the owner's own sessions); `sessions:all` does not authorize this.

### Secret scan before push and merge

`GET /api/v1/sessions/{id}/changes` (and the owner-surface equivalent) includes `secret_scan` for tower sessions. For a
session on another target (such as `macbook`) it is `{"status": "unsupported", "message": "...", "findings": []}`:
those sessions are not scanned. The pinned gitleaks release and rules in `harness/gitleaks/` scan only the lines the session added (`base..HEAD` plus
uncommitted and untracked files), and the lines each commit in `base..HEAD` added. A value that a later commit removed
is still in the commit a push sends, so it is reported with that commit's short SHA in `"commit"` (and in its
fingerprint). No workspace `.gitleaks.toml`, `.gitleaksignore`, baseline, or `gitleaks:allow`
comment changes the result.

```json
"secret_scan": {"status": "ok", "message": "", "scanner": "gitleaks 8.30.1", "cached": false, "elapsed_ms": 140.2,
  "open": 1, "findings": [{"repo": ".", "file": "app/settings.py", "line": 12, "rule": "aws-access-token",
  "fingerprint": "64e5b1561387016aa53e", "preview": "AK…7Q", "dismissed": false}]}
```

`status` is `ok`, `unavailable` (the pinned binary is missing or the wrong version), `error` (it failed to run), or
`unsupported` (not a tower session).
`preview` shows at most the first and last two characters. The value is never returned, logged, or stored, and the
diff in the same response shows each flagged value as `[secret AK…7Q]`. A dismissed finding has
`"dismissed": true` and `dismissal: {reason, actor_id, at}`. Dismissals apply to the same fingerprint at later heads of
that session. A repeated scan of an unchanged head, commit range and working tree comes from a cache
(`"cached": true`).

The gate below applies to **tower sessions only**. Review `merge` and `push` on other targets are not scanned or
blocked. On tower sessions, Review `merge` and `push` scan after committing uncommitted work. `push` sends every commit, so it counts every
finding; `merge` squashes, so it ignores findings with a `"commit"` (values no longer in the net diff). They return
**409** `secret_findings` (`details: {findings, rules: {rule: count}}`) while any such finding is not dismissed, and
**503** `secret_scan_unavailable` if the scanner cannot run. They fail closed, so a broken install blocks them until it is
fixed (`python -m harness.doctor` reports it; the daemon fetches the pinned release at start).

- `POST /api/v1/sessions/{id}/secret-findings/fix` adds one draft review comment per open finding in the diff, naming
  the rule and line and never the value. Send them with `review-comments/send`. A finding with a `"commit"` has no
  diff line: instead the session gets a message at once (a follow-up turn, or the inbox of a running one) naming each
  such commit's short SHA, file, line and rule, asking the agent to rewrite only the branch's own commits
  (`base..HEAD`, non-interactively, keeping the rest of each commit's changes) so that no commit contains the value,
  and not to push. The next push or merge scans again as usual, so Push stays blocked until no commit in the range
  has it. A commit at or before the remote branch's tip (its remote-tracking ref, or a head the harness pushed) is
  never rewritten, since that would need a force-push: dismiss that finding instead. The response is
  `{"drafts": [...], "already_drafted": n, "rewrite": [findings], "pushed": [findings], "message": "..."}`, where
  `rewrite` lists the findings the agent was asked to remove from history, `pushed` the ones only a dismissal can
  clear, and `message` says which of these happened. Same access as line comments: the owner, or a member in their
  own session, never an app token.
- `POST /api/v1/sessions/{id}/secret-findings/{fingerprint}/dismiss` takes `{"reason": "..."}`. Only the owner can
  call it, and the reason is required. It writes an audit row (`secret_finding_dismiss`: session, rule, file, line,
  fingerprint, reason).

### `POST /api/v1/sessions/{id}/cancel`
Requires owning the session (or an owner token, for the owner's own sessions); `sessions:all` does not authorize this.

### `DELETE /api/v1/sessions/{id}`
Erases one of your App's sessions; `204` with no body. Only the App that started the session may: another App (with
`sessions:all` too), the owner and members get a `404`, as if it didn't exist. A running session is cancelled first,
and the response waits for its run to end, including a run that just reached `done` and is still saving its branch
and transcript, so nothing of it is written after the erase. Then its sandbox container, working directory, checkpoint snapshots and transcript are removed, and last its rows:
the session, its events, tool calls and results, approvals, artifacts, checkpoints, review drafts and search entries.
It is idempotent: deleting a session that is already gone returns `204` again. Usage counters (tokens, cost) stay
with the owner as metadata. Older nightly backups keep the session until they rotate out (see Backups above). A
session that ran on a runner (the Mac) keeps its working directory there until that runner's own cleanup.

### `GET /api/v1/sessions/{id}/approvals`, `POST /api/v1/sessions/{id}/approvals/{approval_id}`  (scope `approvals` to decide)
`{"decision": "approve" | "deny", "note": "..."}`. The note is recorded with your app's name. The owner's Web doesn't
list your sessions' approvals (#330), so an App whose sessions can ask for approval needs this scope.

### `POST /api/v1/images`, `GET /api/v1/images/{id}`, `GET /api/v1/images/{id}.png`, `POST /api/v1/images/{id}/upscale`  (scope `images`)
`{"prompt": "...", "model": "fast" | "quality" | "quality-fast" | "flux-fast", "aspect_ratio": "1:1", "upscale": "none" | "2x" | "4x"}`
queues a job; `upscale` defaults to `none` and must stay that way unless the caller asks. Poll until `status` is
`done`, then download the PNG. `quality-fast` is
the Qwen-Image-2512 Lightning 4-step LoRA; it is opt-in and listed on `GET /api/v1` as `image_modes["quality-fast"]`.
If that LoRA is missing, `available` is false and `setup` has the pinned filename, size, SHA-256, and destination;
requesting the mode fails instead of falling back to 50-step `quality`. `flux-fast` is likewise optional: if its
pinned files or ComfyUI nodes are unavailable, the request is refused (HTTP 400) rather than falling back to `fast`.
When `upscale` is `2x` or `4x`, the original is kept and a linked derived image is generated in the same GPU occupancy
(Real-ESRGAN general-image weights, optional). `POST /api/v1/images/{id}/upscale` with `{"upscale": "2x" | "4x"}` does
the same from a completed gallery image and is idempotent per parent and scale. While images generate or upscale, the
language model is unloaded for a few minutes. Missing Real-ESRGAN weights do not break ordinary generation; the
upscale routes return a clear install error. `GET /images` (phone) and job JSON include mode availability and setup
details. Each finished job stores `provenance` (mode, steps, sampler, hashes, seed, timing); older rows without that
column still load.
App tokens can only create and read **generated** images and their upscaled derivatives. Uploaded photos, masks, and
masked edits are owner-only Agent Harness Web data and return 404 on this surface.

### `GET /api/v1/remote-control`, `POST /api/v1/remote-control/{project}`, `POST /api/v1/remote-control/{project}/stop`  (scope `remote_control`)
Starts the unmodified `claude remote-control --spawn worktree` in a tower project's folder, so the user can work there
from the Claude mobile app or claude.ai/code under their own Claude login. Those sessions don't go through the
harness: no queue, sandbox or harness approvals, and Claude Code asks for permission in the Claude app. The list
shows each eligible project with `trusted`, `running`, `pairing_url` (open it to pair), `session_urls` and
`active_sessions`. Starting returns the same object once the server is connected (up to 30 s), with
`already_running: true` if it was. It fails with 400 when the folder hasn't been trusted in Claude Code yet (the
user runs `claude` there once) or isn't a git repository. Remote Control servers keep running until stopped,
including across Agent Harness Server restarts.

## Subscription backends (Claude Code, Codex, Cursor): terms and billing

> **Status: built in Phase 8a ([issue #20](https://github.com/dflippojr/agent-harness/issues/20)).** Claude Code,
> Codex CLI and Cursor Agent CLI are available as sandboxed session backends with usage/limit reporting and
> user-level API-key fallback. This section describes the rules the backends follow so apps can integrate safely.
> It's the harness author's reading of the providers' published terms as of 2026-09-15, not legal advice. Terms and
> billing change, so check the sources at the end.

A harness will be able to run a session on the user's own Claude, ChatGPT or Cursor subscription instead of a local
model. Agent Harness Server runs the unmodified `claude`, `codex` or Cursor `agent` CLI inside the session sandbox. The user
signs in once, on their own machine, through the provider's own login flow.

### What Agent Harness Server guarantees

- **Only Agent Harness Server talks to the provider CLI.** Agent Harness Apps never launch it, see its credentials,
  or reach its login. An App that needs particular agent context sends it to the Server (`context`, `tools`), which
  sets up the session.
- **The CLI is never modified.** It runs as published by Anthropic, OpenAI or Cursor.
- **Programmatic use is labeled as programmatic.** Sessions run in the CLI's non-interactive mode (`claude -p`,
  `codex exec`, `agent -p`). Agent Harness Server never drives the interactive terminal UI to look like a person typing.
- **Each user's usage is billed to that user.** A harness serves its owner. It must not route other people's apps or
  users through one person's subscription.
- **Usage is visible.** Agent Harness Server reports rate-limit state, and a running tally of programmatic usage,
  through this API and Agent Harness Web. It warns when that usage is billed from separate credits instead of
  subscription limits.
- **API keys are optional and configurable.** An Anthropic, OpenAI or Cursor API key can be the machine default, or
  the owner can assign an isolated key file and billing policy to one app. Apps never submit or retrieve those keys.

### Why this is a grey area

- **Allowed:** Anthropic's terms permit an end user signing in to the unmodified Claude Code binary with their own
  subscription, including where a platform hosts it, as long as each user authenticates and is billed themselves.
  OpenAI supports ChatGPT-plan sign-in for Codex on headless machines and has said it wants subscriptions used widely.
  Cursor is the most explicit: it documents a user API key (`CURSOR_API_KEY`) for the headless CLI in scripts and CI,
  and offers an SDK for its agents. Its terms still forbid renting, lending or selling the service, and hold the
  account owner responsible for all activity under the account.
- **Restricted:** Anthropic says subscription (OAuth) login is designed for "ordinary use of Claude Code and other
  native Anthropic applications". Developers building products on Claude "should use API key authentication" and may
  not offer Claude.ai login in their own apps, "route requests through Free, Pro, or Max plan credentials on behalf
  of their users", or collect or store Claude.ai credentials or session tokens. An app that sends prompts through a
  user's Agent Harness Server can be read either way.
- **Billing:** Anthropic announced that Agent SDK, `claude -p` and third-party app usage would draw from a separate
  monthly credit, with overage at API rates, instead of subscription limits. As of 2026-09-15 that change is
  **paused**, but it may return. Usage limits also assume "ordinary, individual usage".
- **Enforcement** is at the provider's discretion, may happen without notice, and lands on the **user's account**,
  not on the app.

### What app builders should do

The harness can't control how apps present this, so these are strong recommendations:

- **Tell users before their subscription is used.** Say that the app runs agent sessions on their own subscription
  through their harness, that this counts as programmatic usage, and that the provider may meter or limit it. Suggested
  wording:

  > This app runs AI tasks through your Agent Harness, using the Claude, ChatGPT or Cursor subscription you signed in to
  > there. That usage counts against your plan's limits, and your provider may bill it separately or restrict it.
  > You can switch to your own API key in the harness settings.

- **Show Agent Harness Server's usage and rate-limit information** instead of hiding it, and pass on its credit warnings.
- **Offer an API-key path** for users who don't want to risk their subscription, and use it for anything that
  runs unattended at volume.
- **Don't** ask users for provider credentials or tokens, bundle or patch the CLIs, or run one harness for many users.
- If you plan to distribute an app widely, **ask the provider** about your use case first. Anthropic's terms
  page points to its sales team for questions about permitted authentication.

### Sources

- Anthropic, [Claude Code: Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
- Anthropic, [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- Cursor, [Headless CLI](https://cursor.com/docs/cli/headless), [CLI authentication](https://cursor.com/docs/cli/reference/authentication) and [Terms of Service](https://cursor.com/terms-of-service)
- OpenAI, [Codex authentication](https://learn.chatgpt.com/docs/auth) and [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)

## GPU sharing

The harness has one GPU. Sessions queue for it (`queue_position` in the session). Requests to the inference endpoint
go ahead of the next agent turn, and image generation takes the GPU exclusively for a batch. A game or Plex transcode
pauses everything (see `docs/phase5-results.md`). Design apps for tasks that take minutes, not milliseconds.

## Versioning

The path carries the major version. Additive changes (new fields, event types, endpoints) happen within v1, so ignore
fields you don't know. Breaking changes will get `/api/v2`, with v1 kept for a transition period.

| Version | Date | Changes |
| --- | --- | --- |
| 1.0 | 2026-09-15 | First release: sessions, context, app tools, events, approvals, images, scoped tokens |
| 1.1 | 2026-09-15 | `remote_control` scope and endpoints; `ungrounded_quotes` in `run_finished` and as an event |
| 1.2 | 2026-09-16 | Exact-origin browser pairing/CORS and short-lived authenticated SSE stream tickets |
| 1.3 | 2026-09-16 | First-party Control Center owner identity/token support for ordinary session operations |
| 1.4 | 2026-09-16 | Daemon profile and optional-module capability discovery |
| 1.5 | 2026-09-16 | Typed OpenAPI responses, supported SDK lifecycle, replay guarantees, and normalized failures |
| 1.6 | 2026-09-16 | Per-app provider allowlists, billing policy, isolated usage attribution, and sanitized status |
| 1.8 | 2026-09-17 | Opt-in Real-ESRGAN 2×/4× upscaling (`upscale` on create; `POST /api/v1/images/{id}/upscale`) |
| 1.9 | 2026-09-17 | Household members: scoped `/me`, `/projects`, search, events, local-only backends; discovery hides project names. `sessions:all` expands owner-scope reads only; messages, context, and cancel require owning the session |
| 1.10 | 2026-09-18 | Per-app configuration registry (`/api/v1/config`); values may only narrow owner/token authority |
| 1.11 | 2026-09-18 | Image mode discovery (`image_modes`) including optional `quality-fast` Lightning LoRA |
| 1.12 | 2026-09-19 | Optional `flux-fast` FLUX.2 klein 4B mode discovery, pinned-asset preflight, and provenance |
| 1.13 | 2026-09-19 | First-party client protocol ranges, version-skew enforcement, and update discovery metadata |
| 1.14 | 2026-10-03 | App-tools-only sessions (`tools_only`), `app_tools_only` discovery, `models:warm` scope for Apps |
| 1.15 | 2026-10-03 | Per-App stores (#330): an App's sessions are its alone. `sessions:all` adds only the owner's sessions, and owner tokens no longer reach an App's sessions (404); nightly backups hold one file per App |
| 1.17 | 2026-10-03 | Agent Harness Web's store (#330 decision 4): the owner's and members' sessions live in `<data_dir>/apps/app-web/harness.sqlite3`. An App without `sessions:all` never reads it: `/api/v1/queue` and the live session list no longer include the owner's sessions for it, and its id lookups cover its own sessions only |
| 1.16 | 2026-10-03 | `DELETE /api/v1/sessions/{id}` erases a session and everything tied to it; `retention_days` on create and an App default retention erase idle sessions; a revoked App's store and folder are erased after 7 days unless the owner undoes the revoke; App session files live in the App's folder (#330) |
