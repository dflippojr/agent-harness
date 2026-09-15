# App API (v1)

Other applications can start agent sessions on a harness, give them context, lend them tools, and follow their progress.
Base path: `/api/v1`, on the daemon's address (`http://127.0.0.1:8100` locally, `https://<pc>.<tailnet>.ts.net` on
a tailnet). FastAPI also serves the machine-readable schema at `/openapi.json`.

A Python client lives in [`sdk/harness_client.py`](../sdk/harness_client.py) (one file, needs `httpx`), with an
example in [`sdk/examples/shopping_list_app.py`](../sdk/examples/shopping_list_app.py).

## Tokens and scopes

Create a token in **Settings → Apps** (or `POST /keys` from the PC:
`{"name": "my-app", "kind": "app", "scopes": ["sessions"]}`). It's shown once; only its hash is stored. Send it as
`Authorization: Bearer ha-...`.

| Scope | Allows |
| --- | --- |
| `sessions` | create sessions, send messages and context, answer tool calls, cancel; read the app's own sessions and events |
| `sessions:all` | read every session (not only the app's own) |
| `approvals` | approve or deny tool calls in the app's own sessions (normally the user approves from the phone) |
| `images` | generate and download images |
| `inference` | use the OpenAI/Anthropic-compatible endpoint under `/v1` |

Apps see only the sessions they created, unless they hold `sessions:all`. Errors are `{"detail": "..."}`, with 401
(bad token), 403 (missing scope), 404 (not found or not yours), 400/409/413 as usual.

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

## Endpoints

### `GET /api/v1`
Server info: API version, scopes, projects, models, and enabled features. Doesn't need a token.

### `POST /api/v1/sessions`  (scope `sessions`)

```json
{
  "prompt": "When does order A-17 ship?",
  "project": "scratch",
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

### `GET /api/v1/sessions`, `GET /api/v1/sessions/{id}`
List (newest first, `?limit=`) or read. Statuses: `queued`, `running`, `waiting_approval`, `waiting_target`,
`waiting_app`, `done`, `failed`, `cancelled`.

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

### `POST /api/v1/images`, `GET /api/v1/images/{id}`, `GET /api/v1/images/{id}.png`  (scope `images`)
`{"prompt": "...", "model": "fast" | "quality", "aspect_ratio": "1:1"}` queues a job; poll the job until `status` is
`done`, then download the PNG. While images generate, the language model is unloaded for a few minutes.

## Subscription backends (Claude Code, Codex, Cursor): terms and billing

> **Status: planned (Phase 8a, [issue #20](https://github.com/dflippojr/agent-harness/issues/20)).** Nothing below is
> built yet. This section describes the rules the backends will follow, so apps can be designed with them in mind.
> It's the harness author's reading of the providers' published terms as of 2026-09-15, not legal advice. Terms and
> billing change, so check the sources at the end.

A harness will be able to run a session on the user's own Claude, ChatGPT or Cursor subscription instead of a local
model. The daemon runs the unmodified `claude`, `codex` or Cursor `agent` CLI inside the session sandbox. The user signs in once, on their
own machine, through the provider's own login flow.

### What the daemon guarantees

- **Only the daemon talks to the CLI.** Apps never launch it, never see its credentials, and can't reach its login.
  An app that needs particular agent context sends it to the daemon (`context`, `tools`), which sets up the session.
- **The CLI is never modified.** It runs as published by Anthropic or OpenAI.
- **Programmatic use is labeled as programmatic.** Sessions run in the CLI's non-interactive mode (`claude -p`,
  `codex exec`, `agent -p`). The daemon never drives the interactive terminal UI to look like a person typing.
- **Each user's usage is billed to that user.** A harness serves its owner. It must not route other people's apps or
  users through one person's subscription.
- **Usage is visible.** The daemon reports rate-limit state, and a running tally of programmatic usage, through this
  API and the web app. It warns when that usage is billed from separate credits instead of subscription limits.
- **API keys are optional and configurable.** An Anthropic or OpenAI API key, supplied by the user or by the app
  builder, can be the default backend or the fallback when subscription limits are hit.

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
  user's daemon can be read either way.
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

- **Show the daemon's usage and rate-limit information** instead of hiding it, and pass on its credit warnings.
- **Offer an API-key path** for users who don't want to risk their subscription, and use it for anything that
  runs unattended at volume.
- **Don't** ask users for provider credentials or tokens, bundle or patch the CLIs, or run one harness for many users.
- If you plan to distribute an app widely, **ask Anthropic or OpenAI** about your use case first. Anthropic's terms
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
