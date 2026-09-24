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
- owner operations such as profile, projects, search, jobs, review, GPU, maintenance, images, keys, household
  accounts, and the global event stream use `/api/admin/v1`;
- household members stay on `/api/v1` after `/me` (including their own projects, search, events, and usage);
- unversioned routes remain Server compatibility routes. Agent Harness Web uses them only for the existing read-only
  Tailscale guest experience, whose ambient guest identity is intentionally not an API credential.

Bundled Agent Harness Web can use same-origin localhost/Tailscale owner or household-member identity. Owner sessions
it creates through `/api/v1` remain ordinary owner sessions, not App-owned sessions. Members are recognized after
`GET /api/v1/me` and use only the account-scoped `/api/v1` surface; they do not construct `/api/admin/v1` requests.
A separately hosted copy sends an owner bearer token and is owner-only. Native `EventSource` uses short-lived `/api/v1` stream tickets; the owner-only global stream uses
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

## Chat snippet runner

The owner can run a short Python, JavaScript, Java, C#, or C++ program from Chat. Chat still only reads code by
default: sending or pasting a message never runs it, and the model has no tool that can start a run.

- **Starting a run.** A fenced block tagged with a supported language (for example `python`, `js`, `java`, `cs`, or
  `cpp`) gets a **Run Python**-style button that names the language it uses. **Run code** opens a small editor where
  you pick the language yourself; there's no default. Blocks with other tags, or no tag, have no Run button.
- **What runs.** Python and JavaScript run as scripts. Java, C#, and C++ take one complete program and use fixed
  compile and run commands. Compiler diagnostics are shown apart from the program's stdout and stderr. There are no
  compiler or runtime flags, arguments, or package installs; standard libraries only.
- **Toolchains.** `python:3.12-slim`, `node:24-slim`, `eclipse-temurin:25-jdk`, `mcr.microsoft.com/dotnet/sdk:10.0`,
  and `gcc:15`, each pinned by digest in `harness/snippets.py`. Every result reports the exact toolchain version.
  Runs never pull an image. Download the pinned images once with `python -m harness.snippets pull`;
  `python -m harness.doctor` warns when any are missing. To upgrade a toolchain, change its digest in a reviewed
  commit.
- **Isolation.** Each run gets a new container that has no mounts (no project, repository, host path, provider login,
  secret, or Docker socket) and no network. The container runs as an unprivileged user with every capability dropped
  and a read-only root filesystem. Source goes in on stdin. The container is removed when the run ends, so no file,
  binary, or cache carries over to the next run.
- **Limits per run.** 30 seconds for compile plus run, 1 vCPU, 1 GiB memory (no swap), 64 processes, 128 MiB of
  temporary storage, and 1 MiB of combined output. On a timeout, cancellation, or output overflow the container is
  removed, which kills every process in it. The result names the limit that was hit and says when output was
  truncated. At most two runs happen at once, and each chat has one at a time.
- **Results.** Chat stores the source and the bounded result in its transcript, so both survive a reload. It shows the
  result as plain text, never HTML. With your next message, the model gets the runs you did since your last message,
  labeled as untrusted program output. If the server restarts mid-run, the run is marked interrupted and its
  container is removed.
- **Access.** Only the owner can run snippets (`POST /api/admin/v1/chats/{id}/snippets`, and `…/snippets/{run}/cancel`).
  Guests, household members, and app or device tokens can't.

## Install on iPhone or iPad

In Safari, choose **Share → Add to Home Screen**. The installed icon is labeled **Harness** and opens Agent Harness
Web in standalone mode. iOS may retain an older label on an already installed icon; to refresh only that label, remove
the icon and add Agent Harness Web to the Home Screen again. No application data is cleared or migrated merely to
force a label update.
