# Control Center client

Control Center is the first-party browser client for the agent-harness daemon. Its static files live in
`harness/web` and have no build step. The daemon still serves that directory by default, so existing Tailscale PWA
installs continue to work. The same directory can also be hosted as a separate HTTPS site.

## API boundary

The client does not construct unversioned daemon URLs for owner use:

- ordinary session list/create/read, messages, cancellation, approval decisions, and per-session events use
  `/api/v1`;
- owner operations such as profile, projects, search, jobs, review, GPU, maintenance, images, keys, household
  accounts, and the global event stream use `/api/admin/v1`;
- household members stay on `/api/v1` after `/me` (including their own projects, search, events, and usage);
- unversioned routes remain daemon compatibility routes. Control Center uses them only for the existing read-only
  Tailscale guest experience, whose ambient guest identity is intentionally not an API credential.

The bundled client can use same-origin localhost/Tailscale owner or household-member identity. Owner sessions it
creates through `/api/v1` remain ordinary owner sessions, not app-owned sessions. Members are recognized after
`GET /api/v1/me` and use only the account-scoped `/api/v1` surface; they do not construct `/api/admin/v1`
requests. An independently hosted client sends an owner bearer token and is owner-only.
Native `EventSource` uses short-lived `/api/v1` stream tickets; the owner-only global stream uses authenticated
streaming `fetch`. Images and transcript downloads are also fetched with authorization rather than leaking a token
in their URLs.

## Use a separately hosted copy

1. Serve the contents of `harness/web` at the root of an HTTPS origin (with `index.html` as the root document).
   Loopback HTTP is also accepted for local development. The asset URLs are root-relative, so deploy this directory
   at the origin root rather than below a path prefix.
2. In the daemon-bundled Control Center, open **Settings → Connection**.
3. Under **Authorize another Control Center**, enter the separate site's exact origin (scheme, host, and optional
   port) and create a token. Copy the `ho-…` token when shown; only its hash is stored by the daemon.
4. In the separate copy, open `#/profile/connection`, enter the daemon URL and token, then choose **Save and test**.

The daemon URL and token are stored in that browser's local storage. The owner token is limited to its approved
browser origin for browser requests. Revoke it under **Settings → Apps → Control Centers**. CORS preflight and actual
requests are allowed only for an origin present on a live key, and the owner API still verifies that the presented
token is an owner token approved for that same origin.

The service worker caches only the static shell. API responses, streams, images, and transcripts are never cached.
