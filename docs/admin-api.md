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

## Operations

Handlers match the unversioned operator routes. Bodies, query strings and response shapes are the
same; only the prefix and the owner credential check are new.

| Area | Paths |
| --- | --- |
| Identity | `/me`, `/profile` |
| Household accounts | `/accounts`, `/accounts/{user_id}`, `/accounts/audit` |
| Sessions | `/sessions`, `/sessions/{ref}`, messages, cancel, rerun, approvals, transcript, events |
| Review | `/sessions/{ref}/changes`, `/sessions/{ref}/review/{action}` (`merge` \| `push` \| `discard`) |
| Search | `/search`, `/events`, `/queue` |
| Projects and jobs | `/projects`, `/templates`, `/jobs` |
| Tokens | `/keys`, `/keys/{kid}`, `/pairing-codes`, `/pairing-codes/{pid}` |
| App provider policy | `/provider-credentials`, `/provider-credentials/{credential_id}` |
| Mac pairing | `/runner-pairing-codes`, `/runner-pairing-codes/{pid}` |
| Maintenance | `/maintenance`, `/maintenance/cleanup`, `/maintenance/backup`, image-archive retention preview/apply |
| GPU and models | `/gpu`, `/gpu/{pause\|resume}`, `/models`, `/models/status`, `/models/warm`, `/backends` |
| Images | `/images`, `/images/warmup`, `/images/cooldown`, `/images/{iid}/upscale` |
| Runners | `GET /runners` (status only; poll/results stay on the runner token) |
| Memory | `/memory`, `/memory/profile` |
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

## Changelog

| Version | Date | Changes |
| --- | --- | --- |
| 1.6 | 2026-09-17 | Image archive health and explicit retention preview/apply operations |
| 1.5 | 2026-09-17 | Owner-provisioned household members: accounts, audit, aggregate metadata, no member content |
| 1.4 | 2026-09-16 | One-time native Mac client and runner pairing |
| 1.3 | 2026-09-16 | Owner-managed per-app provider policy, opaque key-file references, and revocation |
| 1.2 | 2026-09-16 | Daemon profile and optional-module capability discovery |
| 1.1 | 2026-09-16 | Origin-bound Control Center owner tokens and cross-origin browser access |
| 1.0 | 2026-09-16 | First release: versioned owner operations, `admin` scope, owner tokens (`ho-`) |
