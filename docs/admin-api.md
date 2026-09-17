# Owner API (admin v1)

Machine-owner operations for the Control Center and other first-party operator clients. Ordinary
apps keep using [`/api/v1`](app-api.md); they cannot call this surface, even with every app scope.

Base path: `/api/admin/v1`. Versioning is in the path (`/api/admin/v2` for breaking changes). FastAPI
serves the machine-readable schema at `/openapi.json`.

The bundled and separately hosted Control Center use this contract. Unversioned operator routes remain a
compatibility surface and keep Tailscale owner/guest rules.

## Credentials

Two owner credentials are accepted:

1. **Tailscale/localhost owner identity**, with no `Authorization` header. Same trust as today's
   Control Center: a missing `Tailscale-User-Login` is localhost; a listed `allowed_logins` entry is
   the owner. Guests (`guests:` in `harness.local.yaml`) are refused for every `/api/admin` path.
2. **Owner bearer token.** `POST /keys` or `POST /api/admin/v1/keys` with
   `{"name": "control-center", "kind": "owner", "scopes": ["admin"]}`. The secret is shown once,
   starts with `ho-`, and is sent as `Authorization: Bearer ho-...`.

App tokens (`ha-…`, kind `app`) and device/inference tokens (`hk-…`, kind `device`) receive **403**
`app tokens cannot use the owner API`. The `admin` scope cannot be granted to those kinds.

An owner token may include an exact browser-origin allowlist for a separately hosted Control Center. Create it from
the bundled client's **Settings → Connection** page (or `POST /api/admin/v1/keys` with `kind: "owner"`, the `admin`
scope, and an `origins` array). Versioned API CORS is allowlisted by those live keys, and each actual owner request
also checks that the presented token was approved for the request's origin. See [`control-center.md`](control-center.md).

## Discovery

### `GET /api/admin/v1`

Requires owner credentials. Returns `api_version`, the `admin` scope description, accepted `auth`
methods, the daemon `capabilities`, and the versioned `operations` list (`method` + `path`).

## Operations

Handlers match the unversioned operator routes. Bodies, query strings and response shapes are the
same; only the prefix and the owner credential check are new.

| Area | Paths |
| --- | --- |
| Identity | `/me`, `/profile` |
| Sessions | `/sessions`, `/sessions/{ref}`, messages, cancel, rerun, approvals, transcript, events |
| Review | `/sessions/{ref}/changes`, `/sessions/{ref}/review/{action}` (`merge` \| `push` \| `discard`) |
| Search | `/search`, `/events`, `/queue` |
| Projects and jobs | `/projects`, `/templates`, `/jobs` |
| Tokens | `/keys`, `/keys/{kid}`, `/pairing-codes`, `/pairing-codes/{pid}` |
| Maintenance | `/maintenance`, `/maintenance/cleanup`, `/maintenance/backup` |
| GPU and models | `/gpu`, `/gpu/{pause\|resume}`, `/models`, `/models/status`, `/models/warm`, `/backends` |
| Images | `/images`, `/images/warmup`, `/images/cooldown` |
| Runners | `GET /runners` (status only; poll/results stay on the runner token) |
| Memory | `/memory`, `/memory/profile` |
| Notifications | `/notify/test` |
| Remote Control | `/remote-control`, launch/stop, `/remote-control/{project}/trust` |

Not on this surface: `/api/v1` app sessions, `/v1` inference, runner `POST /runners/{name}/poll|results`,
and ntfy `POST /a/{token}/{decision}`.

## Examples

Tailscale/localhost owner (Control Center today, no bearer token):

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
  -d '{"name":"control-center","kind":"owner","scopes":["admin"]}'
```

For a browser client, add `"origins":["https://control.example"]`. Browser origins must be HTTPS except for
loopback development and contain no path, query, fragment, or credentials.

## Changelog

| Version | Date | Changes |
| --- | --- | --- |
| 1.2 | 2026-09-16 | Daemon profile and optional-module capability discovery |
| 1.1 | 2026-09-16 | Origin-bound Control Center owner tokens and cross-origin browser access |
| 1.0 | 2026-09-16 | First release: versioned owner operations, `admin` scope, owner tokens (`ho-`) |
