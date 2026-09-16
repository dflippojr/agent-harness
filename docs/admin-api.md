# Owner API (admin v1)

Machine-owner operations for the Control Center and other first-party operator clients. Ordinary
apps keep using [`/api/v1`](app-api.md); they cannot call this surface, even with every app scope.

Base path: `/api/admin/v1`. Versioning is in the path (`/api/admin/v2` for breaking changes). FastAPI
serves the machine-readable schema at `/openapi.json`.

The bundled PWA still uses the unversioned operator routes (`/sessions`, `/jobs`, `/gpu`, …) until
issue #23 migrates it. Those routes stay as a compatibility surface and keep Tailscale owner/guest
rules. This document is the versioned owner contract.

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

Browser pairing, CORS allowlists, and authenticated SSE for a separately hosted Control Center are
issue #25. Until then this API is for same-origin/Tailscale owner calls and for owner tokens used by
non-browser clients.

## Discovery

### `GET /api/admin/v1`

Requires owner credentials. Returns `api_version`, the `admin` scope description, accepted `auth`
methods, and the versioned `operations` list (`method` + `path`).

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
| Tokens | `/keys`, `/keys/{kid}` |
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

## Changelog

| Version | Date | Changes |
| --- | --- | --- |
| 1.0 | 2026-09-16 | First release: versioned owner operations, `admin` scope, owner tokens (`ho-`) |
