# Agent Harness Web

Agent Harness Web is the first-party browser UI and installable PWA for Agent Harness Server. Its static files live
in `harness/web` and have no build step. Agent Harness Server serves that directory by default, so existing Tailscale
PWA installs continue to work. The same directory can also be hosted at a separate HTTPS origin.

The PWA manifest name and browser title are **Agent Harness Web**. Its short name and Apple standalone title are
**Harness**. Functional navigation labels such as Agents, Jobs, Images, Profile, and Settings remain task-oriented.

## API boundary

Agent Harness Web does not construct unversioned Server URLs for owner use:

- ordinary session list/create/read, messages, cancellation, approval decisions, and per-session events use
  `/api/v1`;
- owner operations such as profile, projects, search, jobs, review, GPU, maintenance, images, keys, and the global
  event stream use `/api/admin/v1`;
- unversioned routes remain Server compatibility routes. Agent Harness Web uses them only for the existing read-only
  Tailscale guest experience, whose ambient guest identity is intentionally not an API credential.

Bundled Agent Harness Web can use same-origin localhost/Tailscale owner identity. Sessions it creates through
`/api/v1` remain ordinary owner sessions, not App-owned sessions. A separately hosted copy sends an owner bearer
token. Native `EventSource` uses short-lived `/api/v1` stream tickets; the owner-only global stream uses
authenticated streaming `fetch`. Images and transcript downloads are also fetched with authorization rather than
putting a token in their URLs.

## Use a separately hosted copy

1. Serve the contents of `harness/web` at the root of an HTTPS origin, with `index.html` as the root document.
   Loopback HTTP is also accepted for local development. Asset URLs are root-relative, so deploy the directory at
   the origin root rather than below a path prefix.
2. In the Server-bundled Agent Harness Web, open **Settings → Connection**.
3. Under **Connect another Agent Harness Web**, enter the separate site's exact origin (scheme, host, and optional
   port) and create a token. Copy the `ho-…` token when shown; only its hash is stored by Agent Harness Server.
4. In the separate copy, open `#/profile/connection`, enter the **Agent Harness Server URL** and token, then choose
   **Save and test**.

The Server URL and token stay in that browser's existing `harness.*` local-storage keys. The owner token is limited
to its approved browser origin for browser requests. Revoke it under **Settings → Apps → Web connections**. CORS
preflight and actual requests are allowed only for an origin present on a live key, and the owner API still verifies
that the presented token is an owner token approved for that same origin.

Existing owner credentials whose display name is `control-center` or “Control Center” remain valid and visible; the
naming change does not rewrite or revoke them. Newly created Web credentials use an `agent-harness-web` display
name. Existing routes, API fields, service-worker scope, and storage keys are also unchanged, so bundled and
separately hosted copies do not need to pair again.

The service worker caches only the static shell. API responses, streams, images, and transcripts are never cached.
The shell cache version changes when new presentation assets must replace an installed copy.

## Install on iPhone or iPad

In Safari, choose **Share → Add to Home Screen**. The installed icon is labeled **Harness** and opens Agent Harness
Web in standalone mode. iOS may retain an older label on an already installed icon; to refresh only that label, remove
the icon and add Agent Harness Web to the Home Screen again. No application data is cleared or migrated merely to
force a label update.
