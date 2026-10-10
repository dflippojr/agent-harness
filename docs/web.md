# Agent Harness Web

Agent Harness Web is the first-party browser UI and installable PWA for Agent Harness Server. Its static files live
in `harness/web` and have no build step. Agent Harness Server serves that directory by default, so existing Tailscale
PWA installs continue to work. The same directory can also be hosted at a separate HTTPS origin.

The PWA manifest name and browser title are **Agent Harness Web**. Its short name and Apple standalone title are
**Harness**. Functional navigation labels such as Agents, Jobs, Images, Profile, and Settings remain task-oriented.

The bundled Server accepts requests only for `127.0.0.1`, `localhost`, or `[::1]` on its configured listen port,
or the exact authority in `public_url` (including its public port). Other Host headers receive **421** on every
method, including GET; forwarded-host headers do not grant access. Every Server response carries
`Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`, so other sites cannot embed the Web
shell. A separately hosted copy needs the same framing headers from its own static host.

## Navigation

A bottom tab bar holds the primary sections: **Chat** (owner only), **Agents**, **Jobs**, **Images** and **Profile**.
Household members see Agents and Profile only. The header shows the section title, the connection chip and a
**Settings** gear (`#/settings`), which opens the Settings menu from any section. Recent chats are listed on the Chat
home.

The Settings menu is grouped into **This phone** (Appearance, Notifications, Connection, Install), **Agents**
(Backends, Smart approvals, Skills, Memory), **Server** (Resources, Server settings, Accounts, Remote control, Disk) and
**Integrations** (Apps, Inference endpoint). Each row shows its current value, such as `System · Default text` or
`GPU held · 41 min`; a value that can't be read is left blank. Disk shows no value, because measuring it walks every
workspace. The owner's former Actions pages are the Server rows, and only the owner sees them. At the end, a version
row shows this bundle's build and protocol, whether it is up to date with the connected Server, and the Server's
release and supported protocol range. **Check for update** asks the Server again and, when a newer bundle is offered,
becomes **Reload and update** (see [compatibility](compatibility.md)).

On phones, detail and editor pages (a session, a job, an image, New task) show Back and hide the tab bar. In the
installed app the bar sits above the home indicator (`env(safe-area-inset-bottom)`). At 768 px and wider the same links
form a left rail that stays on every page. Scheduled work is called **Jobs** everywhere; the old `#/tasks` links redirect to `#/jobs`.

The Jobs list groups jobs as **Needs attention** (the last run ended ATTENTION, failed or waits on an approval),
**Scheduled** and **Paused**. Each row has an **Enabled** switch that saves at once, through the same
`PUT /api/admin/v1/jobs/{id}` the form uses, and offers Undo in the toast. Tapping the row opens the full form.

Confirmations and short inputs (delete, revoke, cancel, rewind, fork, renames, quotas, the update offer) open an
in-app sheet (`lib/sheet.mjs`) instead of the browser's native dialogs: a bottom sheet on phones and a centred card
on wide screens. The action button names the action and is red when it destroys something; input sheets show
errors under the field. Escape, a tap outside the sheet or leaving the page dismisses it.

The Agents list groups sessions into **Needs you** (a pending approval, or a run that failed in the last 24 hours),
**Running** (running, queued or waiting) and **Recent** (finished). Empty groups are hidden, and each group is sorted
by most recent activity. An approval row shows the pending command or tool on one line and opens straight at the
approval. A session on a machine other than the tower shows a laptop icon and the machine's name.

At 1280 px and wider, Agents is a split view: the list sits in a 392 px pane beside the rail and the open session
fills the rest, so moving from one approval to the next is one click. The URL is still `#/agents` or `#/s/<id>`; it
only says which row is open, and that row is highlighted. With nothing open the session side says **No session
open**. The sidebar button in the session header, or <kbd>[</kbd>, hides the list for reading and brings it back.
Between 768 and 1279 px the list and the session are separate pages with Back. The framework is in `lib/layout.mjs`
(`SPLITS`, `mountSplitView`); its header comment says how another list (Jobs, Settings) joins it.

From 768 px the session header is one row: Back (or the sidebar button), the title over its status, project, backend,
model and context meter, then **Transcript / Changes / Info** and **⋯**. On phones the status line and the tabs stay
under the bar. The transcript is a 760 px column centred in the pane, and the composer is centred on the pane, not
the window. A pending approval is a card docked at the pane's foot: the reviewer's verdict on the left, then **Add a
note**, **Deny** and **Approve** at the right (Cancel task is in **⋯**). Focus never jumps to Approve; it moves to the
card's heading only when it was in the transcript or the composer. On Changes and Info a pending approval shows as a
one-line bar with **Review**, which opens the transcript at that approval. The **⋯** menu opens under its button;
the arrow keys, Home and End move through it, and Escape closes it and returns focus to **⋯**.

## Colours and touch targets

Every colour comes from the CSS custom properties at the top of `harness/web/style.css`. The Light and Dark themes
(and System, which follows the device) use the mobile redesign's palette. Midnight, Forest and Paper keep their own
values. `--accent` colours text-like actions such as links, the current tab and running badges. `--primary` fills
buttons, the New task button and your own chat bubbles, with `--on-primary` text. In Dark the accent is blue, not
grey. The other rules use `var(--…)` only, and `tests/test_web_tokens.py` fails on a hex, `rgb()` or `hsl()` colour
outside the token blocks. The same test checks that the main text, muted, accent, primary and status pairs reach WCAG AA
(4.5:1) in Light and Dark, and that the theme swatches on the Appearance page and the `theme-color` meta tags match
the tokens.

Every button, tab, segmented control, switch, field, picker and disclosure is at least 44 px tall (`--tap`). A switch
keeps its 51 × 31 px track inside a 44 px hit area.

## Connection state

The header chip says **Live**, **Reconnecting** or **Offline**. It follows the app-wide event stream from Agent Harness
Server: Reconnecting after the stream drops, Offline after four failed attempts in a row or as soon as the browser
reports no network. The app keeps retrying in both states, with exponential backoff from 1 s up to 30 s and random
jitter, and retries at once when the browser comes back online or the app returns to the foreground. Inside a page
with Back, a live connection shrinks to its dot.

An open session's transcript shows a strip under Transcript / Changes / Info while its own stream is down, with how long
ago the last event arrived. When the Agents list or the recent chats fail to refresh, the list keeps what it shows and
says **List may be stale**, when it last updated and why, with **Retry**.

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
