# Household member GitHub sign-in (issue #63)

An enabled household member can authorize **their own GitHub account** and use it to clone, fetch, and push their
own private repositories. Credentials are isolated by the member's opaque `user_id`. Member operations never fall
back to the machine owner's Git configuration or credentials. The feature is **off by default**. Existing owner
projects and credential-free public member clones work exactly as before.

## Trust boundary

This feature prevents accidental and application-level cross-account credential use: one member's credential
cannot be used for another member, the owner, an app, or an agent sandbox. It does **not** protect a member's
credential from the machine/OS owner, an administrator who can inspect the daemon's OS account or its secure
store, or a compromised host. This is the same host trust boundary as household accounts (#62).

## What GitHub is asked to grant

Git Credential Manager's built-in GitHub OAuth app requests the broad **`repo`, `gist`, and `workflow`** scopes.
That gives access to every repository the member's GitHub account can reach, not only the repositories they add
here. It is not repository-scoped or least-privilege. The connect screen says so. Agent Harness uses the
credential only through the constrained Git broker described below. A selected-repository GitHub App could
replace this later; it is not part of this version.

## Setup (owner)

1. Install **Git Credential Manager 2.9.0 or newer** in an administrator-owned location that household members
   cannot write. On Windows, use the copy bundled with Git for Windows
   (`C:/Program Files/Git/mingw64/bin/git-credential-manager.exe`).
2. Configure it in `harness.local.yaml` (file configuration only; Agent Harness Web cannot change these values):

   ```yaml
   github_member_auth:
     gcm_path: C:/Program Files/Git/mingw64/bin/git-credential-manager.exe
     credential_store: wincredman      # wincredman | dpapi (Windows), keychain (macOS), secretservice | gpg (Linux)
     # gpg only:
     # gpg_pass_store_path: /home/harness/.password-store   # an initialized `pass` store (contains .gpg-id)
     # gnupg_home: /home/harness/.gnupg
     # git_path: C:/Program Files/Git/cmd/git.exe            # optional; default: git on PATH
   ```

3. Restart the daemon, then turn it on in **Actions → Accounts → Member GitHub sign-in**. Turning it on runs a
   preflight. The owner page shows each member's coarse state (`disconnected`, `connecting`, `connected`,
   `reconnect_required`) and last-use time. It never shows repository URLs, GitHub usernames, or device codes.

### Supported stores and preflight

| Platform | Stores | Notes |
|----------|--------|-------|
| Windows | `wincredman`, `dpapi` | `dpapi` keeps a per-member store under `data_dir/github-broker/v1/dpapi/<user_id>`. |
| macOS | `keychain` | The daemon's login keychain must be unlocked. |
| Linux | `secretservice`, `gpg` | `secretservice` needs a session D-Bus (`DBUS_SESSION_BUS_ADDRESS`) and an unlocked keyring; `gpg` needs an initialized `pass` store. |

The `plaintext`, `cache`, and `none` stores, and any store from another platform, are refused. Preflight:

- refuses a GCM path that is missing, relative, a symlink or reparse point, under `data_dir`, or (POSIX)
  group/world-writable or owned by someone other than root or the daemon account;
- requires GCM 2.9.0 or newer;
- round-trips a dummy credential (`store`, `get`, `erase`, `get`) in the dedicated
  `agent-harness/v1/preflight` namespace. It never touches GCM's default `git` namespace or a member namespace;
- on Linux, requires a desktop session (`DISPLAY` or `WAYLAND_DISPLAY`). GCM only runs a custom UI helper in a
  desktop session.

A failed check reports `not_configured`, `store_unavailable`, `unsupported_context`, or `incompatible_gcm`. The
feature then fails closed with no silent downgrade. A daemon running as a Windows service in session 0, over SSH,
or without a keyring usually fails preflight or the first connect with `unsupported_context`.

On Windows, ACLs on the GCM path are not inspected. Choose an administrator-owned location such as
`Program Files`.

## How a member connects

**Account → GitHub → Connect** starts GCM's GitHub **OAuth device flow**:

- The daemon runs `git-credential-manager get` with `GCM_PROVIDER=github`, `GCM_GITHUB_AUTHMODES=device`,
  `GCM_NAMESPACE=agent-harness/v1/<user_id>`, and a pinned UI helper (`harness/gcm_ui_helper.py`).
- GCM starts the helper as `<helper> device --code <code> --url https://github.com/login/device`. The helper
  sends only the URL and code to the daemon over a one-time, nonce-checked loopback connection. GCM polls GitHub
  itself.
- GCM's `get` output (the token) is piped directly into `git-credential-manager store`. The daemon never reads it.
  Neither the daemon nor the browser ever receives a token.
- The code is shown only to the member who started the attempt, and is kept in memory only. The harness's hard
  deadline is GitHub's 15-minute device-code lifetime. A refresh resumes the same live attempt. Cancel, timeout,
  account or feature disable, and daemon shutdown end it.
- Connecting first erases the member's previous credential in their namespace, so reauthorization replaces only
  that member's credential.

Basic/password, PAT paste, browser callback, SSH, and arbitrary OAuth settings are not supported.

### Status, probe, and erase

- Status reads never touch the store. The noninteractive probe (`get` with `GCM_INTERACTIVE=never`, output
  discarded) runs after a connect, at daemon startup for rows that say `connected`, and inside erase.
- **Disconnect** (member) and **Erase GitHub credential** (owner, confirmed, erase-only) stop that member's
  in-flight credentialed Git, then run `git-credential-manager erase` for `github.com` in that member's namespace
  until the probe finds nothing. Repositories, workspaces, review history, and commits stay.
- **Disabling a member or the feature** cancels connection attempts and kills in-flight credentialed
  clone/fetch/push immediately. It does not erase credentials. The member can disconnect, or the owner can erase.
- A GitHub **401** (revoked or expired token) marks the member `reconnect_required` and erases the rejected
  credential. It never retries with another namespace or account.

## Constrained Git broker

All credentialed Git work runs host-side. GCM and credentials are never mounted into an agent sandbox or runner.

- Only canonical `https://github.com/<owner>/<repo>[.git]` URLs are accepted. The URL is parsed and re-rendered,
  and userinfo, ports, query/fragment, `www.`/Unicode/lookalike hosts, HTTP, SSH/SCP, `file:`, local paths,
  bundles, and remote helpers (`ext::`) are refused. Redirects are not followed (`http.followRedirects=false`); a
  renamed or moved repository needs its new URL.
- Child processes get a minimal allowlisted environment. `GH_TOKEN`, `GITHUB_TOKEN`, `GIT_*`, `GCM_*` (other than
  the broker's own), `SSH_*`, proxies, and tracing are removed. System config is off, and each member gets an
  isolated home and global config under `data_dir/github-broker/v1/members/<user_id>`, an empty hooks directory,
  and an empty clone template.
- `-c` overrides (highest precedence) clear inherited credential helpers and install only the pinned GCM with the
  member's namespace. They force HTTPS as the only transport, set prompts off, and clear extra headers and
  proxies. Submodule recursion and LFS filters are disabled.
- The validated canonical origin is stored in the member's project row and passed explicitly to every fetch and
  push. Before each operation the broker revalidates member and feature state, connection state, the stored
  origin, containment in the member's repository root, and the managed repository's local config. Config that
  could rewrite URLs, inject headers/proxies/helpers, select upload/receive-pack programs, or run hooks or filters
  refuses the operation.
- Only sanitized error classes leave the broker (`reconnect_required`, `repository_unavailable`, `timeout`,
  `quota`, `policy_rejected`, …). Child-process output is never shown or logged.

## Projects, sessions, and push

- **New project → Private GitHub repository (my GitHub connection)** clones with the member's own connection into
  their managed repository root, with the same quota, containment, and cleanup rules as public clones. A member
  who is not connected is asked to connect. The pending form stays in the page (never browser storage) and is
  submitted once connected.
- Each session start fetches the stored origin into the managed copy host-side and fast-forwards its branch when
  it has no local merges. A failed fetch is reported and the session uses the last fetched copy.
- **Push to GitHub** on a session's review card asks for confirmation and names the destination
  `owner/repo` and branch. The session branch is copied from the agent-writable workspace into the daemon-owned
  managed repository. Exactly `refs/heads/agent/<session>` is then pushed to the stored origin, without force,
  tags, deletes, mirror, or a default-branch push. The workspace's remotes, config, and hooks are never used for
  the credentialed push.
- The GitHub account that pushes and the Git commit author are separate. Commit identity is not rewritten.

## Limitations

- **No submodules or Git LFS.** Submodules are never initialized and LFS objects are never downloaded. Their
  pointer files stay visible for review, so a private repository that depends on them is incomplete.
- GitHub.com only: no GitHub Enterprise Server, GitLab, or Bitbucket. One GitHub identity per member. No repository
  browsing or search.

## Backups and recovery

Credentials live in the OS secure store, not in SQLite, YAML, or backups. Only non-secret state is in the
`github_connections` table: status, namespace version, timestamps, and the last sanitized error class. After a
restore (or a move to another machine), startup reconciliation probes each `connected` member. A missing
credential becomes `reconnect_required`, and the member connects again. To retire the feature, turn it off and
use **Erase GitHub credential** for each member.

## Tests

`tests/test_github_member_auth.py` uses fake GCM, secure store, UI helper, Git, and GitHub processes
(`tests/github_fakes.py`). No real token appears in fixtures or CI. The live installed-GCM and real-OS-store check
is a user-present smoke test:

1. Configure `github_member_auth`, turn the feature on, and confirm the owner page shows a passing preflight.
2. As a member, connect, enter the code at github.com/login/device, and confirm the status becomes `connected`.
3. Create a project from a private repository, start a session, and push its branch.
4. Revoke the authorization at github.com → Settings → Applications, push again, and confirm
   `reconnect_required` with no prompt.
5. Disconnect and confirm the store no longer has an `agent-harness/v1/<user_id>` entry (for example,
   `cmdkey /list` on Windows).
