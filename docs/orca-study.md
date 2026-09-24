# Orca design study

2026-09-21. Source: [stablyai/orca](https://github.com/stablyai/orca) (MIT, Electron/TypeScript plus a React Native
mobile app), read through its README, website, release notes, repository layout, and a handful of docs and issues
(`docs/agent-skill-sharing-implementation-checklist.md`, `mobile/`, `cloud/`, `skills/`, issue #17397). No source
files were cloned or read, and no Orca code is copied. Where this says "Orca does X" it means Orca's own docs say so.
Anything marked **unverified** is inferred from a title or a directory listing and needs a real read before use.

## Verification rule for follow-up work

Everything below comes from documentation, not code. A research or worker task that acts on it must first clone
`stablyai/orca` at a pinned commit (as the Hermes study did, under `D:\Agents\reference`), record the SHA, read the
specific files the docs described, and correct any claim here that the code does not support. Each backlog entry
filed from this study names the areas to read; where a path is marked "not yet located", search for it rather than
guessing.

## Different products

Orca is an IDE for a fleet of coding agents on one developer's desktop. It wraps other people's agent CLIs in
terminals, one git worktree each, with a companion phone app. Agent Harness is an always-on server that runs its own
agent loop against a local model or drives claude/codex/cursor in sandboxes, with a browser client, an app API and
household isolation. They overlap on "watch and steer agents from a phone" and on Review.

What Orca does not appear to have, and the harness does: per-session sandbox isolation (a review of Orca notes none
between parallel agents), a local inference server and GPU guard, multi-user isolation and scoped tokens, an
approvals policy layer, session-wide FTS search exposed to agents, and an always-on runner (Orca's own docs say a
closed laptop means a dark phone; a headless Linux mode exists). Orca collects opt-out telemetry, and its optional
mobile relay runs on Stably's servers. The harness is Tailscale-only.

## Backlog entries filed from this study

`docs/backlog.yaml`: `review-diff-comments` (#165), `session-fan-out-compare` (#166), `github-issue-to-task` (#167),
`generic-cli-backend` (#168). Landing the study itself is #169.

## Findings mapped to in-flight work

### Native client and pairing (native iOS work, #31)

| Orca | Harness today | Take-away |
| --- | --- | --- |
| Mobile is Expo/React Native. It speaks a versioned WebSocket RPC to the desktop; pairing is a QR code carrying endpoint, device token and TLS fingerprint. | The PWA speaks `/api/v1` and `/api/admin/v1`; browser pairing uses an origin-bound `hp-` code. | The pairing model is equivalent. Orca's RPC is typed per method; ours is the OpenAPI/SDK contract, which is the better base for a native client. |
| Issue #17397: pairing offers travel in `orca://` custom-scheme URLs, so another app registering that scheme can intercept a bearer credential. Proposed fix: verified Universal/App Links, short-lived and single-use offers. | `redeem_pairing_code` is already atomic and single-use, 10-minute TTL, hashed at rest, and bound to an origin (`harness/db.py`, `harness/apps.py`). | The server side is fine. **Constraint for the native client:** never carry a pairing code or token in a custom URL scheme; use a typed/QR code redeemed in-app, or verified associated-domain links. |
| `mobile/src/transport/mock-server.ts` lets the mobile app run scenarios with no desktop. | The web client tests use `client.mjs` against a live app. | A small fixture server for the SDK's session/approval/stream shapes would let a native client be built without the tower. |
| Push goes through a separate gateway (APNs/FCM), the host authenticates with an X25519 key, and logs never contain tokens, notification titles or bodies. | Self-hosted ntfy. | Keep the rule that notification text never lands in server logs. The relay/cell design is only relevant if a hosted relay is ever wanted, which Tailscale makes unnecessary. |

### Version skew (`client-daemon-version-skew`)

Orca keeps protocol constants on both sides (`src/shared/protocol-version.ts`, `mobile/src/transport/protocol-version.ts`).
Each end sends its version on the status request. Breaking changes bump the version; additive changes (new methods,
optional fields) explicitly do not. The skill-sharing design negotiates capabilities per feature, and an older host
answers "update required" instead of failing. Recent releases also have the desktop serve a mobile web bundle over the
air, so the client is versioned with the host it talks to.

For this repo: the "additive within v1, current-plus-previous" contract in `docs/compatibility.md` already matches.
Two additions are worth considering: (1) per-feature capability flags in the handshake so a client can hide a control
instead of comparing version numbers, and (2) serving the Web bundle from the Server it talks to (already the default)
as the answer for the browser, leaving the update-button work for the CLI and Mac Runner only.

### Chat (`chat-home`, merged) and its follow-ups

Orca's native chat resumes structured chats after a restart by checking the provider's own history for sends that
were stranded mid-restart, shows Claude subagent activity and Codex background tasks, summarizes file changes at the
end of a completed turn, and renders the plan as it streams. Candidates for Chat and session transcripts:
stranded-send recovery on daemon restart (does a queued or in-flight send survive?), a collapsed subagent/background
task line instead of raw tool noise, and a per-turn "files changed" summary.

### Skills and the catalog (agent-written skills, marketplace design)

Orca's skill-sharing checklist is the most transferable document found. Its package rules could tighten
`docs/marketplace-design.md` and the skill validators (`harness/skill_validate.py`):

- immutable, digest-identified archives; installs pin a version
- hard limits: 512 files, 32 MiB extracted, 40 MiB compressed, 16 path levels
- reject absolute, traversal and NUL paths, symlinks, hardlinks, devices and encrypted entries; realpath-aware
  containment; Windows drive-prefix handling
- installs never execute anything from the package
- quarantine staging for uploads, expiring after a day if never finalized
- unlisted sharing by unpredictable, revocable bearer tokens (explicitly not end-to-end encrypted)
- independent kill switches for upload, download and remote install
- installs are journaled with crash recovery and default to keeping the local copy on conflict
- one canonical skills root, with per-provider links or copies

### Approvals, hooks and trust

Orca's agent-status feature depends on hooks it writes into each agent's own settings, and its issue tracker shows
the cost: Windows hook wrappers echoing stdin and breaking the payload (#17977), and provider-switcher tools that
rewrite `~/.claude/settings.json` wholesale and silently wipe the hooks (#16771). The harness avoids this by keeping
each CLI's config inside its own auth volume in a container. If hook-based status or permission mapping is ever
added, keep it in harness-owned config and re-assert it at container start.

Orca issue #21867 asks to pre-trust a workspace for Claude Code so worker start does not fail. The harness
deliberately never accepts Claude's trust dialog for Remote Control. Keep that.

### Usage tracking (Backends card, Profile actions)

Orca cut Codex usage scanning "from minutes to under a minute" by resolving attribution once per scan and resuming at
the last parsed byte. If the 5h/7d usage figures are ever computed from CLI transcripts rather than a provider
endpoint, incremental byte-offset parsing is the pattern.

### Worktree lifecycle (Review, cleanup, Remote Control discovery)

Release notes list: a worktree is created even if a post-create step fails, and a failing archive hook now blocks
removal instead of deleting anyway. Directory listings also show reference docs on malformed worktree registration
removal and a worktree scan fingerprint (**unverified** content). The "fail closed on removal" rule maps to
`POST /maintenance/cleanup` and Review discard; the scan fingerprint may inform `admin-fs-browse-remote-locations`.

## Reading list, in priority order

`docs/reference/` in Orca has 47 files; the listing gave titles, not filenames.

1. Agent skill sharing: threat model and upstream boundary, and the checklist above (marketplace, skills)
2. Agent session search: contract and query tuning (compare with `harness/search.py` FTS5)
3. Agent status store and renderer agent-status performance (session list rendering, if it grows)
4. SSH execution boundary, host key verification, reconnect source recovery (Mac Runner, future remote targets)
5. Headless Linux server (`service` profile parity)
6. Agent PTY transcript capture (Transcript tab fidelity)
7. `cloud/docs` runbooks and `cloud/packages/relay-contract`, only if a hosted relay is ever considered

## Things to avoid copying

- Agent hooks installed into shared, user-owned provider config
- Running N parallel agents without a quota check (Orca's own reviewers note N agents mean N rate-limit hits)
- A closed-laptop-means-dark-phone dependency: keep sessions on the always-on Server
- Custom-scheme URLs that carry credentials

## Verification for #166 (fan-out compare), 2026-09-24

Read `stablyai/orca` at pinned commit `122b8c25d7c16f76e395bf9a65887d7c4bc5003b` (shallow clone, not copied).
- `src/cli` has a `worktree` command family (`rm`, `ps`, `set`, `focus`, `limit`, ...) and orchestration guides; no
  `worktree fan` command was found, so that name from the third-party review is **unconfirmed / probably wrong**.
- `src/renderer` has no 3-pane hunk picker component that a search for "hunk" turns up (matches are terminal/dashboard
  code). The hunk-level compare UI is **unconfirmed**; it is out of scope for v1 anyway.
- `docs/reference` does contain `worktree-scan-fingerprint.md` and `malformed-worktree-registration-removal.md`.

The harness design does not depend on Orca's shape: a compare group is a `compare_group` id on ordinary sessions.
v1 API (owner-only): `POST /compare`, `GET /compare/{group}`, `POST /compare/{group}/pick`,
`POST /compare/{group}/discard`. The comparison UI is not built yet. Pick (with `discard_rest`) and discard stop
members that are still running before discarding them, and only after the winner's merge or push has completed.
One pick or discard runs per group at a time; a second concurrent one gets 409 `compare_busy`. A member cancelled
while it is still cloning waits for the clone to finish and be recorded, so its discard removes the branch and workspace.
