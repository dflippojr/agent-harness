# Agent Harness Server owner API (admin v1)

Machine-owner operations for Agent Harness Web and other first-party operator surfaces. Agent Harness Apps keep
using [`/api/v1`](app-api.md); they cannot call this surface, even with every App scope.

Base path: `/api/admin/v1`. Versioning is in the path (`/api/admin/v2` for breaking changes). FastAPI
serves the machine-readable schema at `/openapi.json`.

Bundled and separately hosted Agent Harness Web use this contract. Unversioned Agent Harness Server operator routes
remain a compatibility surface and keep Tailscale owner/guest rules.

## Credentials

Two owner credentials are accepted:

1. **Tailscale/localhost owner identity**, with no `Authorization` header. Same trust as bundled Agent Harness Web:
   a missing `Tailscale-User-Login` is localhost; a listed `allowed_logins` entry is
   the owner. Guests (`guests:` in `harness.local.yaml`) are refused for every `/api/admin` path.
2. **Owner bearer token.** `POST /keys` or `POST /api/admin/v1/keys` with
   `{"name": "agent-harness-web", "kind": "owner", "scopes": ["admin"]}`. The secret is shown once,
   starts with `ho-`, and is sent as `Authorization: Bearer ho-...`.

App tokens (`ha-…`, kind `app`) and device/inference tokens (`hk-…`, kind `device`) receive **403**
`app tokens cannot use the owner API`. The `admin` scope cannot be granted to those kinds.

An owner token may include an exact browser-origin allowlist for separately hosted Agent Harness Web. Create it from
the bundled Web UI's **Settings → Connection** page (or `POST /api/admin/v1/keys` with `kind: "owner"`, the `admin`
scope, and an `origins` array). Versioned API CORS is allowlisted by those live keys, and each actual owner request
also checks that the presented token was approved for the request's origin. See [`web.md`](web.md).

Existing owner records named `control-center` remain valid and visible as legacy Web connections. Agent Harness
Server does not rename or revoke credentials for this terminology change.

## Discovery

### `GET /api/admin/v1`

Requires owner credentials. Returns `api_version`, the `admin` scope description, accepted `auth`
methods, the Agent Harness Server `capabilities`, and the versioned `operations` list (`method` + `path`).
It also publishes the first-party protocol ranges and update hints described in [`compatibility.md`](compatibility.md).

## Operations

Handlers match the unversioned operator routes. Bodies, query strings and response shapes are the
same; only the prefix and the owner credential check are new.

| Area | Paths |
| --- | --- |
| Identity | `/me`, `/profile` |
| Household accounts | `/accounts`, `/accounts/{user_id}`, `/accounts/audit` |
| Sessions | `/sessions`, `/sessions/{ref}`, messages, cancel, rerun, approvals, transcript, events, **metrics** |
| Review | `/sessions/{ref}/changes`, `/sessions/{ref}/review/{action}` (`merge` \| `push` \| `discard`) Line comments: `GET/POST /sessions/{ref}/review-comments`, `DELETE .../{comment_id}`, `POST .../send` (one follow-up; owner and members in their own sessions, never app tokens; the same paths under `/api/v1`). Secret scan (tower sessions only): `changes` carries `secret_scan`; `POST /sessions/{ref}/secret-findings/fix` drafts one comment per open finding in the diff and asks the agent to rewrite the branch's unpushed commits for a finding only in history (already-pushed ones: dismiss); `POST /sessions/{ref}/secret-findings/{fingerprint}/dismiss` `{reason}` (owner only, audited). See [app-api.md](app-api.md#secret-scan-before-push-and-merge) |
| Checkpoints | `GET /sessions/{ref}/checkpoints` lists the visible per-turn snapshots (one after each turn in which a mutating tool ran, even when an error, a cancel or the disk-quota stop ended the turn; tower agent sessions; a hidden ref `refs/harness/checkpoints/<session>/<turn>` in a host-side store outside the sandbox, never pushed). `POST /sessions/{ref}/checkpoints/{turn}/rewind` restores the workspace (tracked, untracked and deleted files; ignored files are left alone; a nested clone as its plain files, without its `.git`, and a clone made after the checkpoint is removed), resets the session branch to the HEAD recorded with the checkpoint (a detached HEAD is detached again at that commit), and puts back the model context and the agent's saved state and notes as they were at that turn (a checkpoint from before those were kept clears them); the listing then shows exactly the checkpoints up to that turn, so rewinding forward again to a later retained turn lists it again; local-model sessions only. A rewind is all or nothing: when a file it must remove or replace is held open by another program (Windows), or a step fails midway, the branch, index and files are put back, the context is untouched, and it answers 409 naming the files. `POST /sessions/{ref}/checkpoints/{turn}/fork` `{prompt}` (201) starts a new session on its own branch from that checkpoint, with the agent's state and notes of that turn; a hosted CLI session forks with a transcript digest instead of its CLI state. Both need an idle session (409 otherwise). Up to 50 per session, oldest pruned; over a member's quota the oldest are pruned, and when even that would not fit the turn is marked not checkpointed and every existing checkpoint, rewound-past ones included, is kept. A snapshot that fails (disk full, git or database error) is likewise marked not checkpointed and leaves every existing checkpoint as it was; once a snapshot is recorded, a later failure cleaning up after it only logs a warning (#261) |
| Search | `/search`, `/events`, `/queue` |
| Projects and jobs | `/projects`, `/templates`, `/jobs` |
| Tokens | `/keys`, `/keys/{kid}`, `/pairing-codes`, `/pairing-codes/{pid}` |
| App provider policy | `/provider-credentials`, `/provider-credentials/{credential_id}` |
| Mac pairing | `/runner-pairing-codes`, `/runner-pairing-codes/{pid}` |
| Maintenance | `/maintenance`, `/maintenance/cleanup`, `/maintenance/backup`, image-archive retention preview/apply |
| Configuration | `/config`, `/config/schema`, `/config/validate`, `/config/rollback`, `/config/restart` |
| GPU and models | `/gpu`, `/gpu/{pause\|resume}`, `/models`, `/models/status`, `/models/warm`, `/backends` |
| Smart approvals | `/smart-approvals` (`GET` status, `PUT` `{mode: off\|shadow\|auto}`; last writer with Settings `smart_approvals.mode`; `off` calls no reviewer) |
| Images | `/images`, `/images/uploads`, `/images/warmup`, `/images/cooldown`, `/images/{iid}`, `/images/{iid}/edit`, `/images/{iid}/upscale`, `/images/{iid}/cancel` |
| Runners | `GET /runners` (status only; poll/results stay on the runner token) |
| Memory | `/memory`, `/memory/profile` |
| Skills | `/skills`, `/skills/enabled`, `/skills/proposals/{pid}`, install/reject/reopen/review, `/skills/{slug}/enable`, disable, rollback, uninstall, projects, export |
| Notifications | `/notify/test` |
| Remote Control | `/remote-control`, launch/stop, `/remote-control/{project}/trust` |

Not on this surface: `/api/v1` app sessions, `/v1` inference, runner `POST /runners/{name}/poll|results`,
and ntfy `POST /a/{token}/{decision}`. Household members receive **403** `members cannot use the owner API` on
every `/api/admin/v1` path and learn no admin data from the error.

## Household accounts

`POST /api/admin/v1/accounts` with `login` (exact Tailscale login), `display_name`, and optional `disk_quota_bytes`,
`max_running`, and `max_queued` creates a member immediately with an opaque `user_id`. First login does not create
identity. `PATCH /api/admin/v1/accounts/{user_id}` can rename, rebind the login (same `user_id`, old login invalid
immediately), disable/re-enable, or change quota and concurrency. Disable cancels that member's running and queued
work and revokes their live streams; data stays. There is no Delete in v1.

Member GitHub sign-in ([`member-github-auth.md`](member-github-auth.md)): `GET /api/admin/v1/github-member-auth`
returns `configured`, `enabled`, the last preflight result, and each member's coarse `status` and `last_used_at`
only (no URLs, usernames, or codes; `?refresh=true` reruns preflight). `PUT /api/admin/v1/github-member-auth`
with `{"enabled": true|false}` switches the feature (default off; disabling stops attempts and in-flight
credentialed Git without erasing). `POST /api/admin/v1/accounts/{user_id}/github-connection/reset` with
`{"confirm": true}` erases that member's stored GitHub credential. The owner cannot connect, test, or use it.

Member Google sign-in ([`google-signin.md`](google-signin.md)): `GET /api/admin/v1/google-signin` returns readiness,
the last preflight, and the exact redirect URI (`?refresh=true` reruns preflight). Per member,
`POST`/`DELETE /api/admin/v1/accounts/{user_id}/google/invitation` creates (shown once) or cancels a one-time link
code, `POST .../google/revoke-sessions` ends every Google Web session, and `DELETE .../google` with
`{"confirm": true}` unlinks. Account rows carry a coarse `google` object; never the `sub`, claims, or tokens. The
owner cannot start a Google authorization as a member.

`GET /api/admin/v1/accounts` returns aggregate metadata only: display name, login, account-id hint, enabled flag,
disk used/quota, running/queued counts, last activity, and limits. It never includes prompts, answers, filenames,
repo URLs, diffs, or transcript excerpts. `GET /api/admin/v1/accounts/audit` is owner-only (365-day retention) and
stores actor/target opaque ids, action, outcome, and timestamp — not prompts, diffs, tokens, or headers.

The durable owner scope remains `user_id = owner`. SQLite stores non-secret account metadata only: never Tailscale
session material, provider credentials, GitHub tokens, or Google tokens.

## Per-app provider credentials

The owner can give an Agent Harness App its own hosted-provider billing policy without giving either the Server
database or the App a plaintext provider key. First put the key in an owner-readable file and map an opaque name to it in
`harness.local.yaml`:

```yaml
provider_secret_files:
  invoice-automation: D:/Agents/harness/secrets/invoice-automation.key
```

Then assign the reference to the app token's `id`:

```http
POST /api/admin/v1/provider-credentials HTTP/1.1
Content-Type: application/json

{
  "app_id": "app-...",
  "backend": "claude",
  "secret_ref": "invoice-automation",
  "policy": "subscription_then_api_key",
  "models": ["claude-opus-5"]
}
```

Policies are `subscription`, `api_key`, and `subscription_then_api_key`. A subscription assignment must use an
empty `secret_ref`; the other policies require a configured reference. An empty `models` list allows every model,
while a nonempty list is an allowlist. Only one assignment is active for an app/backend pair; posting a replacement
revokes the prior assignment.

`GET /api/admin/v1/provider-credentials` returns assignments, opaque references, revocation times, and whether each
referenced file is available. It never returns a file path or key value. Revoke with
`DELETE /api/admin/v1/provider-credentials/{credential_id}`. Revocation stops an active provider process and blocks
new sessions.

Creating the first assignment puts that app into hosted-provider allowlist mode. Every unassigned hosted backend is
denied, and revoking the last assignment keeps the app managed and denied; it never falls back to a machine-wide
subscription or key. Local-model sessions are unaffected. Credential-store integration is intentionally outside
this file-based contract; protect the files with OS permissions and rotate them by replacing the file.

## Examples

Tailscale/localhost owner (bundled Agent Harness Web, no bearer token):

```http
GET /api/admin/v1/sessions HTTP/1.1
```

Owner token:

```http
GET /api/admin/v1/gpu HTTP/1.1
Authorization: Bearer ho-...
```

Mint an owner token from the PC:

```bash
curl -s http://127.0.0.1:8100/api/admin/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"name":"agent-harness-web","kind":"owner","scopes":["admin"]}'
```

For a separately hosted Agent Harness Web copy, add `"origins":["https://harness-web.example"]`. Browser origins must be HTTPS except for
loopback development and contain no path, query, fragment, or credentials.

## Agent Harness for Mac pairing

`POST /api/admin/v1/runner-pairing-codes` with `{"name":"My Mac","runner":"macbook"}` creates a code that
expires after 10 minutes and works once. The owner response shows the code once; list responses contain only its
metadata, and `DELETE /api/admin/v1/runner-pairing-codes/{id}` cancels it. Settings uses this operation to produce the
Agent Harness for Mac install command documented in [`mac-client.md`](mac-client.md).

The Mac redeems the code at `POST /api/v1/runner-pair`. That one response contains a new non-browser owner token and
the selected runner's existing token. It is marked `Cache-Control: no-store`. The code is stored only as a hash, the
runner token stays in its configured owner file and never enters SQLite, and neither token is printed by the CLI.

## Context-efficiency metrics

Owner-only. `GET /api/admin/v1/sessions/{ref}/metrics` (`require_owner`; 404 `no session matches that id` for an
unknown agent session, same as the other session routes) returns per-turn rows plus a session aggregate. The rows
come from persisted `turn_metrics` events, kept as long as the session's events.

```json
{
  "session_id": "s-…",
  "turns": [
    {
      "turn": 1,
      "prompt_tokens": 1200,
      "completion_tokens": 80,
      "composition": {
        "system_state": 200,
        "tool_outputs": 400,
        "file_contents": 500,
        "reasoning_other": 100
      },
      "estimated": false,
      "cache_tokens": 800,
      "recomputed_tokens": 400
    }
  ],
  "aggregate": {
    "dead_end_retries": 2,
    "compaction_correlated_retries": {"elide": 1, "summary": 0, "round_reset": 0},
    "largest_tool_output_chars": 48000,
    "largest_tool_output_by_tool": {"read_file": 48000, "run_shell": 1200}
  }
}
```

Nulls: Claude, Codex and Cursor sessions report null composition, cache, recomputed, and retry fields (native loop
only). Older sessions without `turn_metrics` / `output_chars` also report null rather than a guess. `estimated` is
true when the server omitted `prompt_tokens` and the four buckets are unscaled char estimates; when `prompt_tokens`
is present the buckets are scaled to sum to it. Codex/Claude cache fields are deliberately unused. Delegate calls
are ignored until #157.

`tool_result.output_chars` is Unicode code points of the result before the 20,000-character event truncation.
Failed outputs count. `largest_tool_output_by_tool` is session-API only (never a Prometheus label).

### Prometheus (`GET /metrics`)

Counters, bounded labels, no session id or tool name. Aggregates sum precomputed `turn_metrics` fields (sessions
without those fields contribute nothing). `harness_round_resets_total` is unchanged and is not duplicated here.

| Metric | Type | Unit | Labels | PromQL |
| --- | --- | --- | --- | --- |
| `harness_dead_end_retries_total` | counter | retries | none | `sum(harness_dead_end_retries_total)` |
| `harness_compaction_correlated_retries_total` | counter | retries | `tier=elide\|summary\|round_reset` | `sum by (tier) (harness_compaction_correlated_retries_total)` |
| `harness_prompt_cache_tokens_total` | counter | tokens | `kind=cached\|recomputed` | `sum by (kind) (harness_prompt_cache_tokens_total)` |

Cache series stay 0 until a native llama-server session records `cache_tokens` (first prompt-progress chunk of the
generate call; `-1` maps to null; the UI `processed` fallback is not used). Hosted backends never increment them.

Compaction correlation counts a repeat of a pre-compaction failure in the 5 model turns after `elide`, `summary`,
or `round_reset`. `mask` is a size/composition signal only. The generate immediately after compaction is turn 1;
a matching repeat at turn 5 counts and at turn 6 does not.

## Configuration registry

Owner operational settings live on `/api/admin/v1/config` (schema, GET, validate, PATCH, rollback,
restart). The typed allowlist, persistence, recovery, and error codes are documented in
[`config-registry.md`](config-registry.md). App tokens, device tokens, and guests receive 403.

## Changelog

| Version | Date | Changes |
| --- | --- | --- |
| 1.15 | 2026-10-01 | Secret scan of a session's added lines on `changes`; Review `merge`/`push` on tower sessions return 409 `secret_findings` (or 503 `secret_scan_unavailable`) until findings are fixed or dismissed; fix and dismiss endpoints |
| 1.14 | 2026-09-28 | Owner session context-efficiency metrics and Prometheus retry/cache counters |
| 1.12 | 2026-09-19 | Owner masked inpainting: upload, edit, cancel, and delete |
| 1.11 | 2026-09-19 | First-party client protocol ranges, version-skew enforcement, and update discovery metadata |
| 1.10 | 2026-09-19 | Smart-approval effective mode: last writer among PUT and Settings; `off` is truly off |
| 1.9 | 2026-09-18 | Owner smart-approvals status and live mode (`off`/`shadow`/`auto`) |
| 1.8 | 2026-09-18 | Owner-approved instruction skills: proposals, hash-bound install, enable/allowlist/rollback. `POST /sessions` `skills`: omit the field to inject the project's allowlisted enabled skills; send an explicit list (including `[]`) as the include set so an unchecked box is excluded |
| 1.7 | 2026-09-18 | Typed configuration registry, managed overlay, supervised restart/rollback |
| 1.6 | 2026-09-17 | Image archive health and explicit retention preview/apply operations |
| 1.5 | 2026-09-17 | Owner-provisioned household members: accounts, audit, aggregate metadata, no member content |
| 1.4 | 2026-09-16 | One-time native Mac client and runner pairing |
| 1.3 | 2026-09-16 | Owner-managed per-app provider policy, opaque key-file references, and revocation |
| 1.2 | 2026-09-16 | Daemon profile and optional-module capability discovery |
| 1.1 | 2026-09-16 | Origin-bound Control Center owner tokens and cross-origin browser access |
| 1.0 | 2026-09-16 | First release: versioned owner operations, `admin` scope, owner tokens (`ho-`) |
