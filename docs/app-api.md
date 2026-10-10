# Agent Harness App API (v1)

## Private operational audit (1.20, #471)

`GET /api/v1/audit` returns `{items, next_before_id}` for the authenticated App's store, or for the authenticated
member alone in Web's store. It requires the `sessions` scope. Owner and device tokens cannot use this private
reader. `sessions:all` grants no additional audit visibility. There is no HTTP audit edit/delete operation.
Filters are `target_id`, `actor_id`, `key_id`, `action`, `outcome`, `since` (inclusive server epoch seconds), `until` (exclusive),
`limit` (1–500), and `before_id`. Results sort by immutable id descending; pass `next_before_id` to continue.
Foreign target ids return an empty page. Owner review reads aggregate lifecycle metadata in the main store only;
it never opens private stores to answer an audit query.

Use `Harness.audit(limit=100, action="session.message")` in the Python SDK. Use `harness audit private` with an
App token or the existing authenticated member transport for CLI review; `--before-id`, `--target-id`, `--actor-id`, `--key-id`, `--action`,
`--outcome`, `--since` and `--until` have the same meanings. `harness audit list` remains the owner's main-store
reader. No new Web review page is included.

Rows record server time, the authenticated actor/key and entry point, scoped opaque target, action and outcome,
plus field names/counts and safe enums. Session create/message/context/rename/rerun/cancel, approval decisions and
App tool-result submissions and automatic approval decisions are recorded. Automatic decisions identify the system
with source `agent`. Session mutations and rows share the private writer transaction where
possible. System work has an explicit system identity. An App's `end_user` is a separate `subject` marked
`subject_trust: caller_asserted`; provider sign-in does not verify that App-supplied human label. Rows never store
prompts, context, titles, tool arguments/results, arbitrary App metadata, credentials, codes, URLs/claims, provider
conversation ids, paths or exception text. Private metadata can still reveal activity; it is not owner-visible.

Login lifecycle is private too: `login.start` records launch, `login.code_submit` records code submission (never
the code), `login.finish` records the process's actual success or failure (including expiry/cancellation), and `login.unlink`
records unlink. A successful launch/submission does not imply successful provider authentication. The server stamps
intent before launch/submission/unlink. A restart may leave an intent or launched attempt without terminal evidence;
inspect it rather than infer success. A terminal audit failure appears as `audit_record_incomplete`, an operation id,
`may_have_completed: true`, and `retryable: false` in the error or polled attempt. The SDK preserves these fields.

Session rows live and die with their session, including explicit erasure and retention. Non-session login rows
expire after at most 30 days, or a shorter App retention policy, and are excluded from reads as soon as expired;
insertion and maintenance physically prune them. This limit does not change the 365-day admin/credential policy.
The approved erasure exception is a 365-day, content-free main-store aggregate receipt: App/account id, category,
count, server timestamp, authenticated actor/key, reason (`manual`, `retention`, `revoked_app`, `account_erasure`)
and outcome. Its fresh correlation id identifies the receipt operation only; it is never a copied session, tool,
end-user or login-attempt id. Erasing an App removes its store, including its detailed trail.

Provider login and erasure require a durable `started` intent before effects. Failed intent returns 503
`audit_unavailable` and prevents the effect. Failed settlement preserves intent and reports 503
`audit_record_incomplete`, operation id and `may_have_completed`; do not automatically repeat the action. An
unresolved aggregate erasure blocks subsequent erasures in that namespace, including maintenance, because retaining
the affected private session id in main would violate the erasure contract. The machine owner must inspect local
and provider state before resolving such evidence offline. Ordinary actions keep working on an audit gap, with
`X-Agent-Harness-Audit-Warning: audit_gap` and a content-free server warning; the SDK exposes the last response's
warning as `Harness.audit_warning`. The missing evidence is not invented.

Audit is append-only through application writes except lifecycle erasure and retention; per-row SHA-256 checksums
in private stores help offline inspection. The machine owner remains trusted: these are not tamper-proof storage.
Live erasure removes the detailed trail from live stores. Old backups retain earlier rows until backup rotation;
restoring an older backup rolls the trail back too, with no independent surviving journal. Hosted providers may
retain their own copies under their policies; local erasure and CLI-history removal do not promise deletion of
provider-controlled copies. Session-create idempotency tombstones (#462) are separate.

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
| `memory_library` | the memory library tools (`memory_*`) and guidance in the app's sessions, on projects that enable them |
| `homelab` | the homelab tools (logs, service config, metrics, restart and rebuild requests) in homelab projects |

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
  (`backup.keep_days`, 14 days by default). Transcripts are included: the owner's in `transcripts.zip`, each known
  member's in `transcripts/users/<user_id>.zip`, and each App's in `transcripts/apps/<app_id>.zip`. Member and App
  archives contain relative file paths and are omitted when missing or empty. Links and Windows reparse points
  are skipped with warnings. Deleted or erased transcripts remain in older backups until those backups rotate
  out too. Working directories, checkpoints and artifacts are not in the backups. Backups are owner-only machine
  files and owner-only maintenance operations; they do not add an API for reading members' transcripts. The
  OS-level machine owner is outside the household member privacy guarantee.
- **Hosted Claude reads.** A Claude Code session reads files inside its own `/workspace` without asking. A `Read`,
  `Glob`, `Grep` or `LS` of any other path, or of a path that passes through a symlink or junction in the workspace,
  needs an approval (#370), as does a path with `..` or a `Glob` with a literal folder after a wildcard
  (`**/name/x`), which could pass through a link the harness can't see. The provider login volume at
  `/home/agent/.claude` is shared by every Claude session and holds their history. Project rules can still allow or
  deny specific paths. Codex and Cursor read the shared volume through their own sandboxes, which this approval
  can't reach (#371).
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
their own tower sessions (the local model, or Claude and Codex on their own API key), search only their own transcripts, and receive only their own live events.

When the owner has turned on member GitHub sign-in ([`member-github-auth.md`](member-github-auth.md)), an
ambient same-origin member (never a bearer token) manages **their own** connection:
`GET /api/v1/me/github-connection` returns `status` (`disconnected | connecting | connected | reconnect_required |
disabled`), the device-flow `deadline`/`seconds_left`, `last_used_at`, and a sanitized `error`, plus `prompt`
(`verification_uri`, `user_code`) only while that member's own attempt is live. `POST .../connect` starts or
resumes the attempt, `POST .../cancel` cancels it, and `DELETE /api/v1/me/github-connection` disconnects (erases).
`POST /api/v1/projects` with `"github": true` clones a `https://github.com/<owner>/<repo>` URL with the member's
own connection. It answers `not_connected` or `reconnect_required` (409) when the member must connect first.

### Members' own API keys

A member runs hosted **Claude Code** and **Codex** on their **own Anthropic / OpenAI API key** (#393), billed to their
own provider account. Cursor is not available to members. The routes take the ambient same-origin member only (never a
bearer token, the owner, an App or a guest) and carry no user id, so nobody manages another member's key:

| Call | Does |
| --- | --- |
| `GET /api/v1/me/api-keys` | `{keys: [{backend, provider, env, configured, last4, updated_at}], billing_warning, usage}`: `last4` is all that is ever shown; `usage` is the member's session and token counts per backend |
| `PUT /api/v1/me/api-keys/{claude\|codex}` | Body `{"key": "..."}`: store or replace. `400 invalid_key` when it doesn't look like that provider's key (the body is read by hand, so no error repeats it) |
| `POST /api/v1/me/api-keys/{backend}/test` | One cheap provider call (list models) with the stored key: `{ok, checked, message}`. `409 member_api_key_required` without a key |
| `DELETE /api/v1/me/api-keys/{backend}` | Deletes the key, stops the member's running sessions on that backend and removes their CLI state volume |

Each set, replace, delete and test writes one owner-reviewable audit row (`member_key.*`: member id, backend, configured/replaced and the outcome only; never the key, ciphertext, last four or provider response) in the same transaction as the stored change. See [admin-api.md](admin-api.md#credential-pairing-and-provider-grant-history-468).

A member's `POST /api/v1/sessions` with `backend` `claude` or `codex` runs on their key. Without one it is refused with
`403 member_api_key_required` ("add your API key in your settings"); it never falls back to the owner's login,
`CLAUDE_CODE_OAUTH_TOKEN` (#390) or keys. `GET /api/v1/backends` for a member still lists the local model only; the
Web New task page adds Claude and Codex once a key is saved.

**Storage.** The key is sealed with AES-GCM under a master key in `<data_dir>/member-keys.key` (created on first use).
The database holds the ciphertext, bound to (member, backend), and the last four characters. The master key is not in
nightly backups: after a restore members add their keys again. The key is never in config, logs, events, transcripts,
backups in clear or any response.

**Session wiring.** A member is treated like an App's end user of the Web domain ([End users' own
logins](#end-users-own-logins)): the session's `end_user` is `member:<user_id>` (the prefix is reserved; an App naming
it gets `400 invalid_end_user`), which gives the member their own hashed CLI state volume and the same read-only
config. The container gets only `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (by name; the value rides the docker client's
environment, not the command line), no login volume and no OAuth token. Member A's session mounts only A's volume.
This is a credential source (`harness/credential_sources.py`), so per-member subscription logins could register beside
it later without reworking storage or wiring.

**Removal.** Deleting a key, or disabling the member's account, deletes it and stops their hosted sessions; new ones are
refused. Claude Code keeps a short fingerprint of an API key it has used in its own state, so deleting the key also
deletes the member's CLI state volume.

**Terms.** Anthropic permits hosted Claude Code when each end user authenticates with their own API key, and forbids
collecting or intermediating Claude.ai credentials, so per-member *subscription* tokens stay blocked ([`per-user-subscriptions-study.md`](per-user-subscriptions-study.md),
#377; subscription logins for members: #365). API keys are the permitted route.

Members cannot use the owner's hosted-provider subscriptions, owner/app/device tokens, Mac runners, homelab or memory-library
tools, image generation, Remote Control, inference keys, scheduled jobs, app management, notifications, backups,
or another user's data. Those capabilities are forced off in service/tool construction, not only in the UI.

App tokens remain owner-managed: they cannot act as a member, mint member credentials, or attach sessions to a
member. Device and runner tokens gain no member authority.

## Capability matrix

| Capability | owner | member | guest | app token | device/runner |
| --- | --- | --- | --- | --- | --- |
| Own sessions (create/list/steer/cancel/review) | yes | yes (tower only; hosted Claude/Codex on their own API key) | read-only look around | own sessions, plus the owner's with `sessions:all` | inference only; no member sessions |
| `/api/v1/me`, scoped projects/search/events | yes | own account | no | owner scope | no |
| Create projects | yes | empty or public HTTPS allowlist | no | no | no |
| `/api/admin/v1`, `ho-` owner tokens | yes | 403 | 403 | 403 | 403 |
| App-tools-only sessions (`tools_only`) | no | no | no | own only, never `sessions:all` | no |
| Hosted backends, images, jobs, runners, Remote Control | yes | Claude and Codex on their own API key only; no images, jobs, runners or Remote Control | no | scopes for images/remote_control only | no |
| Homelab, memory library | yes | no | no | only with the `homelab` / `memory_library` scope | no |
| Notifications, backups, keys | yes | no | no | no | no |
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

The response's `app` object describes the key the code minted (`id`, `name`, `prefix`, `scopes`, `kind`, `origins`,
`catalog_app_id`), never its secret beyond the one-time `token`.

### Catalog app id

A key or pairing code can carry an optional `catalog_app_id` (#518): a stable label naming the listing or Hub entry
the App belongs to, so the Hub matches on it instead of on the free-text name. It is a lowercase reverse-DNS id
(`docs/marketplace-design.md` section 4.2, for example `com.example.shopping`), at most 120 characters. Set it on
`POST /pairing-codes` or `POST /keys` (`--catalog-app-id` on `harness pairing-codes create` and `harness keys create`);
a pairing code copies it to the key it mints. Absent means empty, and `GET /keys`, `GET /pairing-codes` and the pairing
response report `""`. It grants nothing and is never part of a token: a minted token has the same shape with or
without it. Uppercase, spaces, more than 120 characters or a token-looking `ha-`, `ho-`, `hp-`, `hk-` or `hrp-`
prefix is refused with 400.

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

## Zero-touch pairing

With a pairing request (#519, App API 1.23) the owner never copies, sees or stores the App's token: the daemon hands
it straight to the App. One model serves browser and native Apps. A browser App is one whose requests carry an
`Origin`; the request, and the key it mints, are bound to that exact origin. A native App sends no `Origin`, and its
request can never be claimed or redeemed by a browser. The existing pairing codes, `POST /api/v1/pair` and `POST /keys`
keep working unchanged.

The App makes a PKCE pair (RFC 7636, S256): a random `code_verifier` of 43 to 128 unreserved characters, and
`code_challenge = base64url(sha256(code_verifier))` without padding. It keeps the verifier to itself until it redeems.

**App-initiated.**

1. `POST /api/v1/pair/requests` with `{"name", "scopes", "catalog_app_id"?, "code_challenge",
   "code_challenge_method": "S256"}`. No token is needed. The answer (201) is `{id, state: "pending", match_code,
   expires_at, browser}`. Show the 6-digit `match_code` to the user.
2. The owner approves it with that match code from the standalone Hub or `harness pairing-requests approve <id>
   --match <code>` (see [admin-api.md](admin-api.md#zero-touch-pairing-requests)), or denies it.
3. `POST /api/v1/pair/requests/{id}/token` with `{"code_verifier"}`. Until the owner approves it answers 409 with
   error code `pairing_pending`, so poll every few seconds. Once approved it mints the `ha-` key and answers (201)
   `{token, app, api_version}`, the same shape as `POST /api/v1/pair`, with `Cache-Control: no-store`. It does so
   exactly once.

**Hub-initiated.** The owner chooses Set up on a Hub entry, sees the consent screen and arms a pre-approved slot for the
entry's `catalog_app_id`, scopes and (for a browser App) origin. The Hub gives the App only the slot's id and the daemon
URL, as a deep link or text to paste. The App claims the slot with `POST /api/v1/pair/requests/{id}/claim`
`{"code_challenge", "code_challenge_method": "S256"}`:

- a browser claim must come from the armed origin and is approved at once (`state: "approved"`): the exact-origin
  binding is the check;
- a native claim answers `state: "claimed"` with a `match_code` to show. The owner confirms it (`harness
  pairing-requests confirm <id> --match <code>`) before the token is released.

The App then redeems as in step 3.

**Lifetimes and refusals.** A request has 10 minutes to be approved (or claimed, or confirmed) and 5 minutes after
approval to be redeemed. A redeemed, denied or expired request is never reused. The redeem refuses a wrong verifier
(400), the wrong origin or a browser redeeming a native request (403), a denied request (403), and an expired or
already redeemed one (400). None of these mint anything. One caller (its tailnet login, or its address for a request
straight to the daemon, whatever Origin it sends) may make 10 requests in 10 minutes and keep 3 waiting for the owner,
and the daemon holds at most 50 waiting requests from all callers; past that it answers 429 (`rate_limited` or
`too_many_pending`). `admin` is never an App scope and is refused when the request is made. Scopes are only what the
owner approved, never what a listing claims. Nothing secret goes in a URL: the request id is not a credential, and the
token is released only to the holder of the verifier.

These routes take no token. They also answer an App on the daemon's own machine that has no local owner token, since
the owner's approval and the verifier are the checks. The Python SDK holds the verifier for you:

```python
from harness_client import Harness

pending = Harness.request_pairing("https://tower.example.ts.net", "Shopping list", scopes=["sessions"],
                                  catalog_app_id="com.example.shopping")
print("Approve this App in the Hub. Match code:", pending.match_code)
h = Harness.redeem_pairing(pending)   # waits up to 10 minutes for the approval; h.token is the App's ha- token
```

`Harness.claim_pairing(url, request_id, origin=...)` claims a Hub-armed slot instead. A browser App passes its
`origin` to both. Both check `features.pairing_requests` in `GET /api/v1` first and raise `feature_unsupported`
against an older Server.

## Hub claim

The standalone Hub claims the daemon with the same request (#543, App API 1.24): `POST /api/v1/pair/requests` with
`"kind": "hub"` and no `scopes`. Everything else is as above (PKCE, match code, 10 and 5 minutes, one redemption),
except:

- while the daemon already has a Hub, the request is refused at once with 409 `hub_claimed`;
- the owner approves or denies it only on the daemon host (`harness hub approve <id> --match <code>`; see
  [admin-api.md](admin-api.md#hub-claim)), never from the Hub, Web or an owner token alone;
- the redeem returns an owner token (`ho-`) with the `admin` scope and `role: "hub"` in `app`, bound to the Hub's
  origin for a browser Hub. If another Hub claimed the daemon after the approval, it answers 409 `hub_claimed`.

`GET /api/v1` reports `features.hub_claim: true` (the Server supports it) and `features.hub_claimed`, a boolean saying
whether a Hub is claimed now. In Python:

```python
pending = Harness.request_hub_claim("https://tower.example.ts.net", "My Hub", origin="https://hub.example")
print("On the daemon host run: harness hub approve", pending.id, "--match", pending.match_code)
hub = Harness.redeem_pairing(pending)   # hub.token is the Hub's ho- token
```

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

### Attach to an existing session

A worker that restarts, or a second process that knows a session id, can pick up the current run instead of creating a
new session:

```python
sid = h.create_session("Triage the inbox", tools=[save_note])["id"]   # save sid durably

# later, possibly in another process
result = Harness("http://127.0.0.1:8100", token="ha-...").attach(sid, tools=[save_note])
print(result.status, result.answer)
```

`attach(sid, tools=None, on_event=None)` sends and creates nothing. It reads `last_event_seq` as its cursor, answers
the calls still pending, streams events after that cursor and stops at the current run's `run_finished`, so an earlier
run's finish is ignored. A session already `done`, `failed` or `cancelled` is returned immediately without running tools;
call `send()` first for a follow-up. Replayed calls that were already answered are not executed. Supplying functions does
not change the tool definitions the session froze at creation.

Limits: only one App tool driver should own a session at a time. If a worker dies after an external side effect but
before its result reaches the server, the call is still pending and runs again after attachment, so tools should be
read-only or idempotent, or the App must reconcile its own side effects.

`Harness.pair(url, code, origin)` redeems an owner-approved browser pairing code. `capabilities()` and `backends()`
discover what Agent Harness Server can run; pass `backend="claude"`, `"codex"`, or `"cursor"` to `run()` / `create_session()`
instead of the default `"local"`. `pending_approvals()` / `decide_approval()` expose native provider permission
requests, while `RunResult.usage`, `.limits`, `.billing_notices`, `.errors`, and `.failure` normalize run outcomes.

## Endpoints

### Endpoint index

Generated from the route registrations (`scripts/docs/build.py`; do not edit between the markers). Auth is what the handler itself asks for; `see source` means a helper decides. `TODO` marks a handler with no docstring. The sections below carry the contracts.

<!-- generated:begin app-api-endpoints -->
| Method | Path | Auth | Summary | Source |
| --- | --- | --- | --- | --- |
| GET | `/api/v1` | see source | TODO | `harness/apps.py` `api_root` |
| GET | `/api/v1/audit` | scope `sessions` | TODO | `harness/apps.py` `scoped_audit` |
| POST | `/api/v1/auth/google/start` | see source | TODO | `harness/google_signin_api.py` `google_start` |
| POST | `/api/v1/auth/logout` | see source | TODO | `harness/google_signin_api.py` `logout` |
| GET | `/api/v1/auth/session` | see source | TODO | `harness/google_signin_api.py` `auth_session` |
| GET | `/api/v1/backends` | scope `sessions` | TODO | `harness/apps.py` `backends` |
| GET | `/api/v1/config` | see source | TODO | `harness/config_api.py` `app_config` |
| PATCH | `/api/v1/config` | see source | TODO | `harness/config_api.py` `app_patch` |
| GET | `/api/v1/config/schema` | see source | TODO | `harness/config_api.py` `app_schema` |
| DELETE | `/api/v1/end-users/{end_user}/logins/{backend}` | scope `sessions` | TODO | `harness/apps.py` `unlink_end_user_login` |
| GET | `/api/v1/end-users/{end_user}/logins/{backend}` | see source | TODO | `harness/apps.py` `end_user_login_status` |
| POST | `/api/v1/end-users/{end_user}/logins/{backend}` | scope `sessions` | Start the CLI's own sign-in for one of the App's end users (#365): `{verification_url, user_code?, needs_code, attempt_id, ...}`. The App shows the URL (and the user code) in a popup. | `harness/apps.py` `start_end_user_login` |
| POST | `/api/v1/end-users/{end_user}/logins/{backend}/{attempt_id}/code` | scope `sessions` | Pass the one-time code the person pasted into the popup (Claude) straight to the waiting login. The body is read by hand, so a validation error can never echo the code back. | `harness/apps.py` `submit_end_user_login_code` |
| GET | `/api/v1/events` | scope `sessions` | TODO | `harness/apps.py` `api_events` |
| POST | `/api/v1/images` | scope `images` | TODO | `harness_modules/images/routes.py` `app_image` |
| GET | `/api/v1/images/{iid}` | scope `images` | TODO | `harness_modules/images/routes.py` `app_image_status` |
| POST | `/api/v1/images/{iid}/upscale` | scope `images` | TODO | `harness_modules/images/routes.py` `app_image_upscale` |
| GET | `/api/v1/me` | scope `sessions` | Authenticated principal. Unauthenticated callers receive 401 rather than a project list. | `harness/apps.py` `api_me` |
| GET | `/api/v1/me/api-keys` | see source | #393: a member's own provider API keys: what is set (last four characters only), the billing note and usage. | `harness/apps.py` `api_member_keys` |
| DELETE | `/api/v1/me/api-keys/{backend}` | see source | TODO | `harness/apps.py` `api_member_key_delete` |
| PUT | `/api/v1/me/api-keys/{backend}` | see source | Store or replace the key. The body is read by hand, so a validation error can never echo the key back. | `harness/apps.py` `api_member_key_set` |
| POST | `/api/v1/me/api-keys/{backend}/test` | see source | TODO | `harness/apps.py` `api_member_key_test` |
| DELETE | `/api/v1/me/github-connection` | see source | TODO | `harness/apps.py` `api_github_disconnect` |
| GET | `/api/v1/me/github-connection` | see source | TODO | `harness/apps.py` `api_github_connection` |
| POST | `/api/v1/me/github-connection/cancel` | see source | TODO | `harness/apps.py` `api_github_cancel` |
| POST | `/api/v1/me/github-connection/connect` | see source | TODO | `harness/apps.py` `api_github_connect` |
| DELETE | `/api/v1/me/google` | see source | TODO | `harness/google_signin_api.py` `my_google_unlink` |
| GET | `/api/v1/me/google` | see source | TODO | `harness/google_signin_api.py` `my_google` |
| GET | `/api/v1/models` | scope `sessions` | TODO | `harness/apps.py` `api_models` |
| GET | `/api/v1/models/status` | scope `sessions` | TODO | `harness_modules/local_model/routes.py` `api_models_status` |
| POST | `/api/v1/models/warm` | scope `sessions` | TODO | `harness_modules/local_model/routes.py` `api_models_warm` |
| POST | `/api/v1/pair` | see source | TODO | `harness/apps.py` `pair_browser` |
| POST | `/api/v1/pair/requests` | see source | Ask the owner to pair this App (#519). Returns the request id and a match code to show the user. | `harness/pairing_requests.py` `create_pairing_request` |
| POST | `/api/v1/pair/requests/{rid}/claim` | see source | Attach this App's code_challenge to a slot the owner armed from the Hub (#519). | `harness/pairing_requests.py` `claim_pairing_request` |
| POST | `/api/v1/pair/requests/{rid}/token` | see source | Fetch the App's token once the owner approved (#519): minted now, returned once, to the verifier's holder. Until then it answers 409 `pairing_pending`. The body is read by hand, so a validation error can never echo the verifier back. | `harness/pairing_requests.py` `redeem_pairing_request` |
| GET | `/api/v1/profile` | scope `sessions` | TODO | `harness/apps.py` `api_profile` |
| GET | `/api/v1/projects` | scope `sessions` | TODO | `harness/apps.py` `api_projects` |
| POST | `/api/v1/projects` | scope `sessions` | TODO | `harness/apps.py` `api_create_project` |
| GET | `/api/v1/queue` | scope `sessions` | TODO | `harness/apps.py` `api_queue` |
| GET | `/api/v1/remote-control` | scope `remote_control` | TODO | `harness_modules/remote_control/routes.py` `app_rc_status` |
| POST | `/api/v1/remote-control/{project}` | scope `remote_control` | TODO | `harness_modules/remote_control/routes.py` `app_rc_launch` |
| POST | `/api/v1/remote-control/{project}/stop` | scope `remote_control` | TODO | `harness_modules/remote_control/routes.py` `app_rc_stop` |
| POST | `/api/v1/runner-pair` | see source | Redeem an owner-approved native Mac code without browser-origin authority. | `harness_modules/runners/routes.py` `pair_runner` |
| GET | `/api/v1/search` | scope `sessions` | TODO | `harness_modules/search/routes.py` `api_search` |
| GET | `/api/v1/sessions` | scope `sessions` | TODO | `harness/apps.py` `list_sessions` |
| POST | `/api/v1/sessions` | scope `sessions` | TODO | `harness/apps.py` `create_session` |
| DELETE | `/api/v1/sessions/{ref}` | scope `sessions` | Erase one of the calling App's sessions and everything tied to it (#330 decision 5). Only the App that started it may: the owner, members and other Apps get a 404. Erasing a session that is already gone succeeds again. | `harness/apps.py` `delete_session` |
| GET | `/api/v1/sessions/{ref}` | scope `sessions` | TODO | `harness/apps.py` `get_session` |
| PATCH | `/api/v1/sessions/{ref}` | scope `sessions` | TODO | `harness/apps.py` `patch_session` |
| PUT | `/api/v1/sessions/{ref}` | scope `sessions` | TODO | `harness/apps.py` `patch_session` |
| GET | `/api/v1/sessions/{ref}/approvals` | scope `sessions` | TODO | `harness/apps.py` `approvals` |
| POST | `/api/v1/sessions/{ref}/approvals/{approval_id}` | scope `approvals` | TODO | `harness/apps.py` `decide` |
| POST | `/api/v1/sessions/{ref}/cancel` | scope `sessions` | TODO | `harness/apps.py` `cancel` |
| GET | `/api/v1/sessions/{ref}/changes` | scope `sessions` | TODO | `harness/apps.py` `api_changes` |
| POST | `/api/v1/sessions/{ref}/context` | scope `sessions` | TODO | `harness/apps.py` `add_context` |
| GET | `/api/v1/sessions/{ref}/events` | scope `sessions` | TODO | `harness/apps.py` `events` |
| POST | `/api/v1/sessions/{ref}/events/ticket` | scope `sessions` | Mint a short-lived query credential so native EventSource need not receive a bearer token in its URL. | `harness/apps.py` `event_ticket` |
| POST | `/api/v1/sessions/{ref}/messages` | scope `sessions` | TODO | `harness/apps.py` `send` |
| POST | `/api/v1/sessions/{ref}/rerun` | scope `sessions` | TODO | `harness/apps.py` `rerun_session` |
| GET | `/api/v1/sessions/{ref}/review-comments` | see source | TODO | `harness/apps.py` `api_review_comments` |
| POST | `/api/v1/sessions/{ref}/review-comments` | see source | TODO | `harness/apps.py` `api_add_review_comment` |
| POST | `/api/v1/sessions/{ref}/review-comments/send` | see source | TODO | `harness/apps.py` `api_send_review_comments` |
| DELETE | `/api/v1/sessions/{ref}/review-comments/{comment_id}` | see source | TODO | `harness/apps.py` `api_delete_review_comment` |
| POST | `/api/v1/sessions/{ref}/review/{action}` | scope `sessions` | TODO | `harness/apps.py` `api_review` |
| POST | `/api/v1/sessions/{ref}/secret-findings/fix` | see source | TODO | `harness/apps.py` `api_secret_findings_fix` |
| POST | `/api/v1/sessions/{ref}/secret-findings/{fingerprint}/dismiss` | scope `sessions` | Owner-only: members and app tokens can ask the agent to fix a finding but never dismiss one. | `harness/apps.py` `api_dismiss_secret_finding` |
| GET | `/api/v1/sessions/{ref}/tool_calls` | scope `sessions` | TODO | `harness/apps.py` `tool_calls` |
| POST | `/api/v1/sessions/{ref}/tool_calls/{call_id}` | scope `sessions` | TODO | `harness/apps.py` `tool_result` |
| GET | `/api/v1/sessions/{ref}/transcript` | scope `sessions` | TODO | `harness/apps.py` `api_transcript` |
<!-- generated:end app-api-endpoints -->

### `GET /api/v1`
Server info: API version, scopes, projects, models, hosted backends, enabled features, `capabilities`, and
`image_modes` (labels, availability, and setup text for optional Lightning `quality-fast` and FLUX `flux-fast`;
only while the images module is present, [`modules.md`](modules.md)). The capability object
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

Returns the session (`id`, `status`, `app_tools`, `metadata`, `answer`, token totals, ...) with `201`, or with `200`
for a retry under the same [`Idempotency-Key`](#retrying-a-create-safely-idempotency-key).

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
- `end_user` (optional, App tokens only, with `backend` `claude` or `codex`) runs the session on **that person's own
  subscription**: your App's own opaque id for them, 1-128 letters, digits and `_.@:-`. The person must have signed
  in first ([End users' own logins](#end-users-own-logins)). Without a login the request is refused with `409
  end_user_login_required`; it never falls back to the owner's login or token, or to an App key. A request without
  `end_user` behaves as before. The session response repeats `end_user`, and the owner's usage tally is kept per
  end user.

#### Retrying a create safely (`Idempotency-Key`)

A create whose response never arrives (a timeout, a dropped connection, a crash on your side) may still have started
a session. Retrying it blindly starts a second run, workspace and provider spend. Send an `Idempotency-Key` header
instead, and send the same one on the retry:

- **Opt in, App tokens only.** The key is 1-128 ASCII letters, digits, `-` or `_`; anything else is `400
  invalid_idempotency_key`. Choose one per logical request (an order id, a UUID) and keep it with the request until you
  have the session id. Owner, device and member tokens get `400 idempotency_unsupported`. Without the header a create
  behaves as before; neither the daemon nor the SDK ever retries a create for you.
- **Replay.** The first successful create returns `201`. A retry with the same key and the same body returns `200`
  with the session as it is now (its status may have moved on) and the header `Idempotency-Replayed: true`. Nothing new
  starts: no second session, `session_created` event, run or workspace. A retry while the first request is still in
  flight gets the same session once that request commits.
  Approved browser Apps can include `Idempotency-Key` in CORS preflight; `Idempotency-Replayed` is exposed to JavaScript.
- **Same request only.** The key is bound to every body field (`prompt`, `end_user`, `tools`, `context`, `metadata`,
  `retention_days`, ...; key order in JSON doesn't matter). Another body under that key is `409
  idempotency_conflict`, which names neither request's contents nor the session.
- **Your App's keys alone.** Keys are scoped to the calling App: the same key from another App is unrelated and
  creates that App's own session. Authentication, origin checks and revocation apply first, so a revoked token gets
  `401` on a retry like on anything else.
- **Failures don't use the key.** A request refused before the session exists (`400`, `403`, `409
  end_user_login_required`, `413`, `422`, ...) records nothing; fix it and retry with the same key.
- **24 hours.** A key is protected for 24 hours from the first successful create. After that it is free again: the
  same key starts a new session.
- **Erased sessions stay erased.** If you `DELETE` the session (or retention erases it) within those 24 hours, a retry
  with its key is `410 idempotency_session_erased`; the session is never created again from it. After the 24 hours the
  key is free again.
- **What is kept.** Your App's store holds one row per key: a SHA-256 hash of your App id and the key, a digest of
  the request body, the session id, and the times. Never the key itself, the prompt, context, tools or metadata.
  Erasing the session clears the session id from the row; the rest goes when the key expires (the next keyed create
  removes expired rows) or when your App's store is erased. The row is written in the same transaction as the
  session, so a daemon that crashes after creating the session but before answering still replays it after a restart.

```python
import httpx
from harness_client import Harness

h = Harness("https://tower.your-tailnet.ts.net", token="ha-...")
key = f"order-{order.id}"  # or str(uuid.uuid4()), saved with the order before the first attempt
for attempt in range(3):
    try:
        s = h.create_session("When does order A-17 ship?", idempotency_key=key)
        break
    except httpx.TransportError:  # the response was lost: the session may exist; the same key finds it
        continue
else:
    raise RuntimeError("the daemon is unreachable; retry later with the same key")
```

`h.run(prompt, tools=..., idempotency_key=key)` forwards the key the same way and attaches to the session's current
run. It checks that replayed tool calls are still pending before serving them, so completed tool calls are not
executed again. Only one driver should own a session; the key protects session creation, not a tool's external side
effects before its result reaches the daemon.

### End users' own logins

An App can let each person use their own Claude or Codex plan (`harness/end_users.py`, #365). Everything below needs
an App token with the `sessions` scope; end users belong to the calling App alone. Cursor is not supported.

| Call | Does |
| --- | --- |
| `POST /api/v1/end-users/{id}/logins/{backend}` | Starts the CLI's own login in a throwaway container on that person's volume. `201` with `{attempt_id, verification_url, user_code?, needs_code, state, expires_in}`. Starting again replaces the person's last attempt |
| `POST /api/v1/end-users/{id}/logins/{backend}/{attempt_id}/code` | Claude only. Body `{"code": "..."}`: the one-time code the person pasted. `200`. Once per attempt (`409 code_already_submitted` after) |
| `GET /api/v1/end-users/{id}/logins/{backend}` | `{backend, linked, attempt}`: `linked` is whether the person is signed in now; `attempt` is `null` or `{attempt_id, state, needs_code, expires_in}` (`waiting`, `submitted`, `completed`, `failed`, `expired`, `cancelled`), for the popup to poll |
| `DELETE /api/v1/end-users/{id}/logins/{backend}` | Unlink: stops the person's running sessions on that backend, runs the CLI's own logout and deletes their volume. `204`; harmless to repeat |

**The popup contract.**

1. The App calls the first endpoint and shows `verification_url` in a popup (Codex: also `user_code`, which is meant
   to be displayed and is not a credential).
2. The person signs in on the provider's own site. Codex finishes there and nothing comes back; poll `GET` until
   `linked` is true. Claude shows the person a one-time code: the popup passes it to the code endpoint, which writes
   it straight to the waiting `claude auth login`, then poll `GET`.
3. A request with `end_user` now runs on that person's login.

**What happens to the code.** It goes from the request body to the login process's stdin and nowhere else: not to
disk, logs, events, the database, transcripts or any response, and no error repeats it. It works once, expires with
the attempt (10 minutes) and only the App that started the attempt can submit it. The credential stays in the
person's volume; the daemon never reads it host-side and no API returns it.

**Isolation.** One volume per (App, end user, backend), named from a hash of the ids, holds the person's login and
their CLI's history and config (read-only config and managed settings as for any session). A session mounts only its
own end user's volume: never another person's, the owner's login or the owner's token. The harness guarantees a
credential serves only requests naming its end user; separating the end users' *data* inside your App stays your job.

**Concurrency.** One person's sessions on one backend share one login, whose refresh token rotates on use (#390). The
harness therefore runs at most one session per (App, end user, backend) at a time and queues the next until it ends.
This applies to Codex too, whose refresh could race alike.

**Errors** carry `error.code`: `end_user_login_required` (409), `invalid_end_user`, `end_user_backend_unsupported`
(400), `no_such_attempt` (404), `code_not_needed`, `code_already_submitted`, `attempt_not_waiting` (409),
`invalid_code` (400), `login_failed` (502), `too_many_logins` (429).

**Revocation.** Unlinking and erasing the App delete the person's volumes. A logout inside the CLI does not
necessarily revoke the grant at the provider (unconfirmed for both CLIs, `docs/per-user-subscriptions-study.md`
section 4), so tell people they can remove the CLI's authorisation in their provider account settings.

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
  WebFetch, Task, ...), slash commands and skills off, and only your tools on its harness MCP server. On `codex`,
  Codex starts with no environment (no shell, `apply_patch` or image viewing), web search, image generation,
  sub-agents, apps and plugins switched off, and only your tools on its harness MCP server. If Codex reports a
  built-in tool anyway, the run stops and fails.
- **Everything else is denied, never asked about.** The session's policy allows exactly your tool names (natively,
  or as `mcp__harness__<name>` from Claude Code or Codex) and denies every other call outright. A model that tries a built-in
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

Backends: `local`, `claude` and `codex` (each hosted CLI with its MCP server on, the default). `GET /api/v1`
lists them in `features.app_tools_only_backends`, and each `GET /api/v1/backends` entry has `app_tools_only:
true|false`, so your App can show which backends can run its bot. Any other backend (`cursor`, or a hosted CLI whose
MCP server the owner switched off) refuses at create time with a 400
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

`GET /api/v1/sessions/{id}/changes` (and the owner-surface equivalent) includes `secret_scan` for tower and Mac Runner
sessions. The tower scans the Mac's input; no scanner is installed on the Mac. A runner on protocol 2 returns
`{"status": "unsupported", "message": "...", "findings": []}` and must be updated before Merge or Push.
The pinned gitleaks release and rules in `harness/gitleaks/` scan only the lines the session added (`base..HEAD` plus
uncommitted and untracked files), and the lines each commit in `base..HEAD` added. A value that a later commit removed
is still in the commit a push sends, so it is reported with that commit's short SHA in `"commit"` (and in its
fingerprint). No workspace `.gitleaks.toml`, `.gitleaksignore`, baseline, or `gitleaks:allow`
comment changes the result.

Viewing Changes reads uncommitted and untracked work without committing it (`scan_input` with `snapshot: false`).
Merge and Push use the default snapshotting mode before applying the gate.

```json
"secret_scan": {"status": "ok", "message": "", "scanner": "gitleaks 8.30.1", "cached": false, "elapsed_ms": 140.2,
  "open": 1, "findings": [{"repo": ".", "file": "app/settings.py", "line": 12, "rule": "aws-access-token",
  "fingerprint": "64e5b1561387016aa53e", "preview": "AK…7Q", "dismissed": false}]}
```

`status` is `ok`, `unavailable` (the pinned binary is missing or the wrong version), `error` (it failed to run), or
`unsupported` (a runner older than protocol 3). Remote input is bounded to 400,000 UTF-8 bytes including metadata
and commit diffs, and 1,000 commits. Exceeding either cap returns `unavailable`; partial input never passes.
`preview` shows at most the first and last two characters. The value is never returned, logged, or stored, and the
diff in the same response shows each flagged value as `[secret AK…7Q]`. A dismissed finding has
`"dismissed": true` and `dismissal: {reason, actor_id, at}`. Dismissals apply to the same fingerprint at later heads of
that session. A repeated scan of an unchanged head, commit range and working tree comes from a cache
(`"cached": true`).

Review `merge` and `push` on tower and Mac Runner sessions scan after committing uncommitted work.
`push` sends every commit, so it counts every
finding; `merge` squashes, so it ignores findings with a `"commit"` (values no longer in the net diff). They return
**409** `secret_findings` (`details: {findings, rules: {rule: count}}`) while any such finding is not dismissed, and
**503** `secret_scan_unavailable` if the scanner cannot run. They fail closed, so a broken install blocks them until it is
fixed (`python -m harness.doctor` reports it; the daemon fetches the pinned release at start).
An offline, timed-out or older runner also returns **503** `secret_scan_unavailable`: update or reconnect it.
The runner checks `expect_head` after its own snapshot and publishes that immutable scanned commit. A changed
branch returns **409** `secret_scan_head_changed` (review again), with nothing pushed or merged. Discard is never gated.

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
and transcript, so nothing of it is written after the erase. Then its sandbox container, working directory, checkpoint
snapshots, transcript and its `agent/<session id>` branch in a local project's repository are removed, and last its rows:
the session, its events, tool calls and results, approvals, artifacts, checkpoints, review drafts and search entries.
It is idempotent: deleting a session that is already gone returns `204` again. A create retried with the
`Idempotency-Key` that made the session gets `410 idempotency_session_erased` until the key expires. Usage counters (tokens, cost) stay
with the owner as metadata. Older nightly backups keep the session until they rotate out (see Backups above). A
session that ran on a runner (the Mac) has its branch and working directory there removed too when the runner is
awake; otherwise they stay until that runner's own cleanup.

### `GET /api/v1/sessions/{id}/approvals`, `POST /api/v1/sessions/{id}/approvals/{approval_id}`  (scope `approvals` to decide)
`{"decision": "approve" | "deny", "note": "..."}`. The note is recorded with your app's name. The owner's Web doesn't
list your sessions' approvals (#330), so an App whose sessions can ask for approval needs this scope.

### `POST /api/v1/images`, `GET /api/v1/images/{id}`, `GET /api/v1/images/{id}.png`, `POST /api/v1/images/{id}/upscale`  (scope `images`)
`{"prompt": "...", "model": "fast" | "quality" | "quality-fast" | "flux-fast", "aspect_ratio": "1:1", "upscale": "none" | "2x" | "4x"}`
queues a job; `upscale` defaults to `none` and must stay that way unless the caller asks. These routes, the
`images` scope and `features.images` come from the images module and don't exist while it is absent. Poll until `status` is
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
| 1.25 | 2026-10-10 | Idempotent session create (#462): an optional `Idempotency-Key` header on `POST /api/v1/sessions` (App tokens) replays the first request's session with `200` and `Idempotency-Replayed: true` for 24 hours; `409 idempotency_conflict` for another body, `410 idempotency_session_erased` after an erase. `features.idempotent_create` is `true`. SDK `create_session(..., idempotency_key=...)` and `run(..., idempotency_key=...)` |
| 1.24 | 2026-10-09 | Exclusive Hub claim (#543, see [Hub claim](#hub-claim)): `kind: "hub"` on `POST /api/v1/pair/requests` (no scopes; refused with 409 `hub_claimed` while a Hub is recorded), redeemed to the Hub's owner token with `role: "hub"`. `features.hub_claim` and `features.hub_claimed`. SDK `Harness.request_hub_claim`; `request_pairing` and `claim_pairing` check `features.pairing_requests` first |
| 1.23 | 2026-10-09 | Zero-touch pairing requests (#519): `POST /api/v1/pair/requests`, `POST /api/v1/pair/requests/{id}/claim` and `POST /api/v1/pair/requests/{id}/token` (PKCE S256; the daemon mints the `ha-` key at redemption and returns it once). `features.pairing_requests` is `true`. SDK `Harness.request_pairing`, `claim_pairing` and `redeem_pairing`. Existing pairing codes and keys are unchanged |
| 1.22 | 2026-10-09 | Optional `catalog_app_id` on keys and pairing codes (#518): set on `POST /pairing-codes` and `POST /keys`, copied to the key a pairing code mints, and reported by `GET /keys`, `GET /pairing-codes` and the `app` object of `POST /api/v1/pair` (now a typed `PairedAppResponse`). A label only; it grants nothing and is never in a token |
| 1.21 | 2026-10-09 | `memory_library` and `homelab` scopes: an App session gets those tools only when its token holds the scope, and an unset `app.capabilities` means what the token's scopes allow. App sessions on a local project clone only its base branch, and erasing one deletes its `agent/<session id>` branch from a local project |
| 1.19 | 2026-10-05 | End users' own subscription logins (#365): `end_user` on session create and `/api/v1/end-users/{id}/logins/{backend}` (start, code, status, unlink), for `claude` and `codex`. Members' own API keys (#393): `/api/v1/me/api-keys`, and a member's `backend` `claude` or `codex` runs on their own key; `member:` is reserved in `end_user` |
| 1.18 | 2026-10-05 | `codex` runs App-tools-only sessions and is listed in `app_tools_only_backends`; hosted Codex sessions get the harness tools over MCP (#373) |
| 1.16 | 2026-10-03 | `DELETE /api/v1/sessions/{id}` erases a session and everything tied to it; `retention_days` on create and an App default retention erase idle sessions; a revoked App's store and folder are erased after 7 days unless the owner undoes the revoke; App session files live in the App's folder (#330) |
