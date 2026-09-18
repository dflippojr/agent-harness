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
scoping: App tokens still see only their own sessions unless granted read-only `sessions:all`.

A one-file Agent Harness SDK lives in [`sdk/harness_client.py`](../sdk/harness_client.py) (requires `httpx`), with an
example in [`sdk/examples/shopping_list_app.py`](../sdk/examples/shopping_list_app.py).

Agent Harness SDK is the supported Python surface. It covers discovery and pairing, every session backend, initial and
incremental context, app tools, approvals, cancellation, resumable events, provider status, and images. Call
`Harness.validate_openapi()` during an integration check to verify that its operation and request types still match
Agent Harness Server's live `/openapi.json`; the repository test suite performs the same check against every change.

`GET /api/v1/backends` is authenticated and app-specific. Its `today`, `week`, and `usage_by_source` fields contain
only the calling app's usage. `provider_policy` reports whether that app may use the backend, its billing policy and
model allowlist, and whether the assigned credential source is available. It never contains a provider key, key-file
path, or owner-only opaque reference. The unauthenticated discovery document at `GET /api/v1` likewise does not
report key-file availability or usage.

Machine-wide cached provider-limit data is also hidden from managed apps because it could belong to a different
assignment. A managed app receives limits reported by its own provider process in that session's `rate_limit` events.

## Tokens and scopes

Create an Agent Harness App token in **Settings → Apps** (or `POST /keys` from the PC:
`{"name": "my-app", "kind": "app", "scopes": ["sessions"]}`). It's shown once; only its hash is stored. Send it as
`Authorization: Bearer ha-...`.

| Scope | Allows |
| --- | --- |
| `sessions` | create sessions, send messages and context, answer tool calls, cancel; read the app's own sessions and events |
| `sessions:all` | read every session (not only the app's own) |
| `approvals` | approve or deny tool calls in the app's own sessions (normally the user approves from the phone) |
| `images` | generate and download images |
| `inference` | use the OpenAI/Anthropic-compatible endpoint under `/v1` |
| `remote_control` | start and stop Claude Code Remote Control servers in project folders |

Apps see only the sessions they created, unless they hold `sessions:all`. Errors are `{"detail": "..."}`, with 401
(bad token), 403 (missing scope), 404 (not found or not yours), 400/409/413 as usual. Harness-generated errors also
include `error: {code, message, retryable}`; `detail` remains for compatibility. The SDK exposes these as
`HarnessError.code`, `.detail`, and `.retryable`.

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
`image_modes` (labels, availability, and setup text for optional Lightning `quality-fast`). The capability object
identifies the `full` or `service` profile, always-on Server facilities, and effective optional modules. It contains
no credentials and doesn't need a token. `GET /health` exposes the same capability object for lightweight discovery.

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
- `project` must exist in the harness's `projects.yaml` (`GET /api/v1` lists them).
- `backend` is `local` (the tower model) or a hosted CLI id such as `claude`, `codex`, or `cursor`
  (`GET /api/v1` lists enabled backends). Hosted sessions use the user's own subscription login.

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

### `POST /api/v1/sessions/{id}/context`
`{"context": [{"title": "...", "content": "..."}]}`. Same as a message, but marked as context from the app.

### `POST /api/v1/sessions/{id}/cancel`

### `GET /api/v1/sessions/{id}/approvals`, `POST /api/v1/sessions/{id}/approvals/{approval_id}`  (scope `approvals` to decide)
`{"decision": "approve" | "deny", "note": "..."}`. The note is recorded with your app's name.

### `POST /api/v1/images`, `GET /api/v1/images/{id}`, `GET /api/v1/images/{id}.png`, `POST /api/v1/images/{id}/upscale`  (scope `images`)
`{"prompt": "...", "model": "fast" | "quality" | "quality-fast", "aspect_ratio": "1:1", "upscale": "none" | "2x" | "4x"}`
queues a job; `upscale` defaults to `none`. Poll until `status` is `done`, then download the PNG. `quality-fast` is
the Qwen-Image-2512 Lightning 4-step LoRA; it is opt-in and listed on `GET /api/v1` as `image_modes["quality-fast"]`.
If that LoRA is missing, `available` is false and `setup` has the pinned filename, size, SHA-256, and destination;
requesting the mode fails instead of falling back to 50-step `quality`. When `upscale` is `2x` or `4x`, the original
is kept and a linked derived image is generated in the same GPU occupancy. `POST /api/v1/images/{id}/upscale` with
`{"upscale": "2x" | "4x"}` does the same from a completed gallery image and is idempotent per parent and scale.
While images generate or upscale, the language model is unloaded for a few minutes. Missing Real-ESRGAN weights do
not break ordinary generation.

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
| 1.9 | 2026-09-17 | Image mode discovery (`image_modes`) including optional `quality-fast` Lightning LoRA |
