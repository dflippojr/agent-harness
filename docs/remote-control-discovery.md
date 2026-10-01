# Owner Remote Control folder discovery

Folder discovery is Windows-only, owner-only, metadata-only and disabled by
default. Enable it in Agent Harness Web Settings after adding at least one valid
discovery root. These are the only writable discovery settings:

| Setting | Default | Bounds |
| --- | --- | --- |
| `remote_control.discovery.enabled` | `false` | Boolean |
| `remote_control.discovery.roots` | `[]` | Dedicated validated path list, up to 8 roots |
| `remote_control.discovery.max_depth` | `3` | Integer 1–5 |

Environment variables and home notation expand on the server. Roots must be
existing local directories on fixed NTFS or ReFS volumes. Drive roots, UNC,
mapped/subst/network/removable/optical/RAM volumes, device paths, alternate data
streams, relative paths and trailing-dot/space aliases are refused. Case and
overlapping roots are canonicalized using directory handles and component-aware
containment. Every existing ancestor must be free of reparse points: symlinks,
junctions, volume mount points and cloud placeholders are all excluded. This can
exclude OneDrive Documents/Desktop and junctioned development folders. Choose a
plain local directory instead. Validation returns reason codes without secret
configuration values.

Click **Find folders** under owner Actions → Claude Remote Control to start a
scan. There is no watcher, background index, automatic scan or resumable cursor.
Each scan has fixed limits: 20,000 visited directories, 500 candidates, 30 seconds
and 50 reported partial errors. Only one scan can run at a time; starting another
returns the active scan. Cancellation is idempotent. Hitting a limit successfully
returns clearly truncated results. Results are memory-only and expire after 15
minutes, including finished or cancelled results.

Enumeration uses directory metadata and no-follow directory handles. Ancestor
handles deny delete sharing while a directory is inspected. Discovery never
reads project files or `.git` contents, runs Git/package managers/code/plugins,
hashes files or contacts the network. It recognizes `.git` presence, Python,
Node, Rust, Go, Maven/Gradle, CMake/Meson, Visual Studio solution/project and VS
Code workspace markers. Once a candidate is found, discovery does not descend
below it. Hidden/system entries, reparse points, registered daemon/credential/
Docker/system locations, dependency/package caches, virtual environments and
build/output/coverage folders are excluded. A hidden `.git` marker is recognized
by presence only; a reparse marker is ignored. Reported error locations mask
component names, so directory names containing usernames or secrets are not
echoed in partial errors.

**Add folder** is a separate explicit owner action. Review the exact path and
markers and choose a unique safe display slug. Markers do not imply safety or
Claude trust. Candidates are opaque IDs bound to their scan, the **global #66
configuration revision**, root, canonical path, volume serial and 128-bit file
ID. Any configuration revision change invalidates prior candidates, including
unrelated settings changes; scan again. Promotion accepts an ID and confirmation
data, never an arbitrary path. It reopens the root and candidate without
following reparse points, rechecks identity/containment/exclusions and requires
a Git marker when the current spawn mode is `worktree`.

Confirmed folders enter `data_dir/remote-control/folders.json`, an isolated
revisioned envelope using #66's atomic-write/locking/LKG primitives. The folder
document has its own validated nested payload; it does not relax the settings
registry's flat allowlist. `folders.lkg.json` recovers an invalid active document.
The directory has a protected Windows owner/SYSTEM DACL and promoted entries
always carry `owner_only: true`, canonical path, root and filesystem identities.
File-configured folders remain read-only and win slug conflicts. A later conflict
marks the managed entry invalid instead of shadowing configured entries.

Adding never runs Claude, initializes Git, creates a worktree, starts a server or
edits `~/.claude.json`. Later **Trust in Claude** opens the existing visible
terminal on the tower: Claude presents its trust prompt and the owner decides.
The harness cannot accept that prompt and trust is not inherited from a root.
Trust and launch revalidate managed identity and containment every time; invalid
entries remain visible. Held directory handles narrow substitution races, but
Claude later opens a string path itself, so the harness cannot eliminate that
external race after its handles are released.

**Remove folder** affects only the managed entry. Close its Claude trust window
and stop its Remote Control server first; active entries return 409. Removal
never stops processes, deletes files/worktrees, revokes Claude trust or edits
file-configured folders.

Only owner identity or an owner bearer token with the `admin` scope can call:

| Method | Owner API path |
| --- | --- |
| POST | `/api/admin/v1/remote-control/discovery/scans` |
| GET / DELETE | `/api/admin/v1/remote-control/discovery/scans/{scan_id}` |
| POST | `/api/admin/v1/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote` |
| DELETE | `/api/admin/v1/remote-control/folders/{slug}` |

Promotion requires `slug`, `confirmed_path` and `confirmed_markers` matching the
candidate. Non-Windows owner calls return `unsupported_platform`; the UI hides
discovery controls. Apps (including `remote_control` scope), members, guests,
devices, runners and agents gain no capability. Managed folders are absent from
the app API, app configuration/schema/capabilities and agent tool schema, and
direct agent calls guessing a managed slug are refused. Cross-origin owner
credentials retain the existing origin checks.

Discovery audit records contain actor/scan IDs, revisions, counts, reason codes
and HMAC identity fingerprints. They contain no raw paths, listings or marker
names. The managed overlay necessarily stores only explicitly confirmed paths;
raw scan results are never persisted. Tests run metadata/Win32 mocks on every
platform, with host Windows checks and phone/desktop DOM interaction coverage.
