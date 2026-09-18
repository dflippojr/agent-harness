# Marketplace threat model v1

Companion to [`marketplace-design.md`](marketplace-design.md). Schema:
[`marketplace-manifest.schema.json`](marketplace-manifest.schema.json). Written against the shipped `/api/v1` **1.7**
app boundary, exact-origin pairing, capability discovery, and per-app provider policy
([`app-api.md`](app-api.md), [`admin-api.md`](admin-api.md)).

This is a design artifact. It does not add daemon enforcement.

## 1. Protected assets

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| Owner daemon (`ha-` app tokens, pairing codes `hp-`, owner tokens `ho-`) | Owner machine, hashed in SQLite | Session control, CORS, approvals |
| Provider credentials and login caches | Daemon Docker volumes and owner-managed key files; never the catalog | Subscription and API billing; account takeover |
| Session prompts, context, transcripts, approvals, images | Daemon DB / workspaces | Private work, secrets pasted into chats |
| Cross-session data | All sessions on one daemon | `sessions:all` can read other apps' and Control Center work |
| Remote Control surface | Native Claude Code in a trusted folder | Bypasses harness sandbox and approvals |
| Local model `/v1` inference | Tower GPU endpoint | Unmetered local compute; prompt exfiltration if proxied |
| Catalog signing keys and published records | Catalog operator (future) | Integrity of “reviewed” claims |
| Publisher source, artifacts, and signing keys | Publisher | Supply chain |
| Owner machine outside the sandbox | Whatever the owner installed | Catalog cannot sandbox it |

## 2. Attacker classes

| Class | Typical goal |
| --- | --- |
| Malicious publisher | Get listed, then steal tokens, prompts, or provider access |
| Compromised publisher (account or repo) | Push a bad version under a trusted name |
| Compromised reviewer or catalog operator | Publish without review; steal submission contents |
| Network attacker / typosquat domain buyer | Intercept installs; clone a listing name |
| Malicious or curious app already paired | Scope creep, session read, approval spoof |
| Stolen `ha-` token holder | Drive the daemon as that app |
| Dependency/supply-chain attacker | Poison a build the publisher did not review |
| Abandoned-app hijacker | Take over an unused name or domain |

Honest-but-policy-violating publishers (undisclosed paid features) are treated as an abuse class even when they
are not trying to steal credentials.

## 3. Risks the harness cannot solve

State these once; every row below may point here.

1. **Code installed outside the daemon sandbox.** A native binary, browser origin, or updater the owner ran can
   do anything that process's OS user can do. Catalog review reduces likelihood; it does not confine the process.
2. **Commercial-use enforcement.** Listing policy can require a free/non-commercial disclosure and can delist.
   Neither daemon nor catalog can prove an external app's billing.
3. **Provider enforcement on the owner's account.** Anthropic, OpenAI, and Cursor may limit or ban a
   subscription because of how an app used it. The catalog can disclose and revoke a listing; it cannot bind the
   provider.
4. **Owner-granted elevated scopes.** If the owner consents to `sessions:all`, `approvals`, or `remote_control`,
   the daemon will honor the token. Review and consent copy are the controls, not a second sandbox.
5. **Physical or host compromise** of the tower (unlocked Control Center, stolen disk, malicious local process
   reading `harness.sqlite3` or provider volumes).

## 4. Control owners (legend)

Used in the tables:

| Abbrev | Owner of the control |
| --- | --- |
| Pub | Publisher |
| Rev | Reviewer / catalog lead |
| Cat | Catalog service (future storage/signing issue) |
| Own | Daemon owner |
| Dae | Daemon implementation (today + future revocation-hook issue) |
| Web | Agent Harness Web discovery/consent UI (future) |
| Prv | Hosted provider (Anthropic / OpenAI / Cursor) |

## 5. Abuse cases

Each case lists prevention, detection, response, residual risk, and who owns the control.

### 5.1 Malicious publisher

A publisher submits a helpful-looking app whose real purpose is to steal prompts, tokens, or access.

| | |
| --- | --- |
| **Prevention** | Identity proof for `app_id`; Stage A secret/URL/hash checks; Stage B source-vs-behavior review; elevated scopes need two reviewers; `never_handles_provider_credentials` required. **Rev, Cat** |
| **Detection** | Abuse mailbox; permission-diff on updates; future Stage C destination/telemetry traces. **Rev, Own** |
| **Response** | Reject or emergency_compromise (delist, warn, block new pairing, recommend revoke). **Rev, Own** |
| **Residual** | Owner still has to install and pair; a malicious binary after pairing is [§3.1](#3-risks-the-harness-cannot-solve). |

### 5.2 Compromised publisher

A previously honest publisher's GitHub org, domain, or artifact-signing key is stolen.

| | |
| --- | --- |
| **Prevention** | Immutable old versions; new version required for ownership or signing-key change; domain-control proof; no auto-update of scopes. **Pub, Cat** |
| **Detection** | Sudden signing-key or domain DNS change; security contact report; hash mismatch vs last reviewed artifact. **Pub, Rev** |
| **Response** | `warn_installed` on versions signed with the stolen key; freeze publisher; require new version with rotated keys; recommend token revocation if a bad version shipped. **Rev, Own** |
| **Residual** | Owners who already ran a malicious updater are in [§3.1](#3-risks-the-harness-cannot-solve). |

### 5.3 Release / source mismatch

Distributed artifacts do not match the linked source (hidden payload, different dependency lock).

| | |
| --- | --- |
| **Prevention** | Required `source_matches_distributed_build` flag; Stage B correspondence review; unsigned artifacts limited to standard-tier and disclosed. **Pub, Rev** |
| **Detection** | Hash check at review; owner-side hash check at install (future Web/docs); SBOM vs lockfile diff. **Rev, Own** |
| **Response** | Reject or delist; `warn_installed` if already published. **Rev** |
| **Residual** | Review samples; it cannot prove every future bit the publisher hosts unless owners re-hash. Catalog is not an artifact mirror. |

### 5.4 Artifact compromise

A release URL starts serving different bytes after review (compromised GitHub Releases, swapped CDN object).

| | |
| --- | --- |
| **Prevention** | Immutable recorded `sha256`; listing points at URLs but trusts hashes; owners (and future UI) must verify hashes before run. **Cat, Own** |
| **Detection** | Hash mismatch; publisher or reporter notice. **Own, Rev** |
| **Response** | Treat as publisher compromise: warn, block new pairing, require a new version with new hashes. Do not “fix” the old record. **Rev** |
| **Residual** | Owners who skip hash verification. Catalog does not host bytes, so it cannot make the URL immutable. |

### 5.5 Catalog compromise

Attacker modifies the index or publishes a record the reviewers did not approve.

| | |
| --- | --- |
| **Prevention** | Append-only records; detached signatures; split reviewer vs signer roles; no remote-wipe API. **Cat** |
| **Detection** | Signature failure; hash mismatch vs stored publisher bytes; out-of-band key list. **Cat, Web, Dae** |
| **Response** | [Design §12](marketplace-design.md#12-catalog-and-signing-key-compromise): revoke key, rotate, re-sign only matching hashes, warn all listings, suspend the uncertain window. **Cat, Own** |
| **Residual** | Clients that pin nothing will believe a fake index. Until Web/daemon hooks exist, owners have no automated verifier. |

### 5.6 Dependency confusion

Build pulls `acme-utils` from a public registry instead of the publisher's namespace.

| | |
| --- | --- |
| **Prevention** | Stage A SBOM namespace check; human review of lockfiles; require pinned versions and hashes where the ecosystem allows. **Rev, Pub** |
| **Detection** | Unexpected registry in SBOM; future sandbox seeing undeclared destinations. **Rev** |
| **Response** | Reject; if already published, emergency or warn depending on exploitability. **Rev** |
| **Residual** | Dynamic `pip install` at runtime that is not in the SBOM — listing policy forbids it; native apps can still do it ([§3.1](#3-risks-the-harness-cannot-solve)). |

### 5.7 Signature-key theft

Publisher or catalog signing key is stolen.

| | |
| --- | --- |
| **Prevention** | Catalog keys offline/HSM in the storage issue; publisher keys disclosed by URL; version immutability. **Cat, Pub** |
| **Detection** | Unexpected signed version; CT-like monitoring is out of v1; publisher report. **Pub, Rev** |
| **Response** | Publisher: rotate, new version, warn old. Catalog: §12 rotation, never silent uninstall. **Pub, Cat, Own** |
| **Residual** | Signatures already accepted by owners who installed during the theft window. |

### 5.8 Typosquatting

`dev.agent-harnesss.notes` or `com.anthropic.helper` impersonates a known app or vendor.

| | |
| --- | --- |
| **Prevention** | Reserved prefixes (`dev.agent-harness.`, `com.agent-harness.`, `agent-harness.`); typosquat distance check vs live listings and well-known names; no Anthropic/OpenAI/Cursor trademarks in `app_id` or display name. **Cat, Rev** |
| **Detection** | Automated Levenshtein/prefix check on submit; abuse reports. **Cat** |
| **Response** | Reject; delist clones; optional warn if a clone was published. **Rev** |
| **Residual** | Lookalike Unicode names — schema is ASCII-only `app_id`; display names still need human review. |

### 5.9 Origin takeover

Browser app listed for `https://app.example` ; the domain expires or DNS is hijacked; pairing still allows that origin.

| | |
| --- | --- |
| **Prevention** | Exact-origin pairing (`normalize_origin`, HTTPS except loopback); catalog origin list is a claim, not a CORS grant; domain-control proof at listing time; origin change is an expansion. **Dae, Rev, Pub** |
| **Detection** | Publisher/domain monitoring is publisher-owned; abuse reports; future pairing hook can notice catalog `block_new_pairing`. **Pub, Own** |
| **Response** | New version or delist; `block_new_pairing` + `recommend_revoke` for the old origin. Existing origin-bound tokens still work until the owner revokes them — by design. **Rev, Own** |
| **Residual** | Daemon cannot know the domain's current owner. HTTPS helps but does not stop a hijacked cert+DNS pair. |

### 5.10 Scope creep

A published notes app adds `sessions:all` or `approvals` in a “patch”.

| | |
| --- | --- |
| **Prevention** | Permission-diff is an expansion: new version, full review, owner re-consent; auto-update must not expand scopes (delist if it does). **Cat, Rev, Pub** |
| **Detection** | Stage A diff; owner sees a new pairing/consent prompt (future Web + daemon). **Cat, Own** |
| **Response** | Reject the version or delist the updater; old version remains what was consented. **Rev** |
| **Residual** | A native updater that ignores policy and writes a new token request into local config still needs the owner to approve a new pairing — unless it reuses an already-over-scoped token. Granting elevated scopes “just in case” is an owner mistake [§3.4](#3-risks-the-harness-cannot-solve). |

### 5.11 App-token theft

`ha-` bearer stolen from logs, URLs, analytics, or a world-readable file.

| | |
| --- | --- |
| **Prevention** | Pairing flow already forbids putting tokens in URLs; SSE uses tickets; docs and review require OS keychain or mode 0600; secret scan of submissions; CORS exact origin for browser tokens. **Dae, Pub, Rev** |
| **Detection** | Owner notices unexpected sessions; future last-used display (already on keys). **Own, Dae** |
| **Response** | Owner revokes the key (`Settings → Apps` / admin keys API). Catalog `recommend_revoke` if the app is the leak. **Own** |
| **Residual** | Bearer tokens are equivalent to the app until revoked. The catalog never sees them and must not. |

### 5.12 Approval spoofing

App with `approvals` auto-approves dangerous tools, or an app without it spoofs the UI to look like the owner approved.

| | |
| --- | --- |
| **Prevention** | `approvals` is elevated; daemon still records the deciding app name on the decision; apps without the scope cannot decide; Control Center remains first-party. **Dae, Rev** |
| **Detection** | Transcript shows the deciding app; owner can compare. **Own, Dae** |
| **Response** | Revoke the app token; delist; recommend revoke. **Own, Rev** |
| **Residual** | A legitimately granted `approvals` token **is** the approver. Conspicuous consent is the control. Cursor print-mode has no host approval bridge today (`docs/phase8a-design.md`); harness contains that with the container and branch review, not with the catalog. |

### 5.13 Cross-app / cross-session data access

App A reads App B's sessions or Control Center work.

| | |
| --- | --- |
| **Prevention** | Default `sessions` is own-sessions only; `sessions:all` is elevated, GET-only, separately consented; owner API is `admin`-only; `provider_policy` and usage on `GET /api/v1/backends` are per calling app. **Dae, Rev, Own** |
| **Detection** | Unexpected reads in logs; review of apps that request `sessions:all`. **Own, Rev** |
| **Response** | Revoke token; delist if the listing hid the scope. **Own, Rev** |
| **Residual** | Owner-granted `sessions:all`. Prompt text of *that app's* own sessions is always visible to it. |

### 5.14 Prompt / context exfiltration

App injects context then copies transcripts or tool payloads to `network_destinations`.

| | |
| --- | --- |
| **Prevention** | Context is labeled as app-provided information in the daemon; destination and data-collected lists are required; undeclared telemetry is delist-class; future Stage C allowlist. **Dae, Rev, Pub** |
| **Detection** | Owner traffic inspection; mismatch vs manifest; abuse reports. **Own, Rev** |
| **Response** | Delist + warn + recommend revoke. **Rev, Own** |
| **Residual** | Any paired app can send its own prompts/results wherever its process can network. Disclosure and review, not confinement [§3.1](#3-risks-the-harness-cannot-solve). |

### 5.15 Provider-credential capture

App asks the owner to paste `ANTHROPIC_API_KEY` / `CURSOR_API_KEY` / `auth.json`, or reads the provider volume.

| | |
| --- | --- |
| **Prevention** | Daemon never exposes keys on `/api/v1`; owner files and CLI volumes are daemon-side; apps cannot call `/api/admin/v1`; schema `never_handles_provider_credentials: true`; review rejects install docs that ask for provider secrets; secret scan. **Dae, Rev** |
| **Detection** | Review; user report; secret scan on updates. **Rev, Own** |
| **Response** | Emergency delist; tell owners to rotate **provider** keys (catalog still does not handle those keys); recommend `ha-` revoke. **Rev, Own, Prv** |
| **Residual** | Social engineering after install [§3.1](#3-risks-the-harness-cannot-solve). Copying login caches is also forbidden by OpenAI's own auth docs; the catalog must not document that as an install step. |

### 5.16 Undisclosed telemetry

Crash reporter or “anonymous stats” not in the manifest.

| | |
| --- | --- |
| **Prevention** | `telemetry.default` plus destinations required; default `off→on` is expansion; Stage B sample of network calls. **Pub, Rev** |
| **Detection** | Future Stage C; owner firewall; binary diff on update. **Rev, Own** |
| **Response** | Delist or changes_requested; warn if already published. **Rev** |
| **Residual** | Encrypted channels to an allowed host can still carry extra fields. Review cannot see inside TLS. |

### 5.17 Unsafe auto-update

Publisher updater replaces the binary and starts using new scopes or a new origin without consent.

| | |
| --- | --- |
| **Prevention** | Listing policy forbids silent permission expansion; updates that change scopes/origins/data-flow need a new version and re-consent; catalog is not an updater. **Rev, Pub, Own** |
| **Detection** | Hash change vs last reviewed artifact; owner notices new behavior. **Own, Rev** |
| **Response** | Delist; warn; block new pairing; recommend revoke. Daemon still will not expand a token's scopes by itself — the owner would have to pair again. **Rev, Dae, Own** |
| **Residual** | If the original token already had extra scopes, an updater can use them. If the app is native, it can ignore the catalog entirely [§3.1](#3-risks-the-harness-cannot-solve). |

### 5.18 Review bypass

Publisher slips extra JSON fields, a second artifact, or an `auto_check_override` to skip Stage A.

| | |
| --- | --- |
| **Prevention** | `additionalProperties: false`; version records hash **exact bytes**; override is lead-only, audited, cannot skip secret scan; two reviewers for elevated. **Cat, Rev** |
| **Detection** | Schema tests; audit of overrides. **Cat** |
| **Response** | Reject; revoke reviewer access if abuse; treat published bypass as catalog incident (§12 if signer collusion). **Cat** |
| **Residual** | Collusion of reviewer + signer. Split roles and audit are the mitigation, not a mathematical guarantee. |

### 5.19 Reviewer compromise

Stolen reviewer account publishes malware under the catalog signature.

| | |
| --- | --- |
| **Prevention** | Separate signer key; elevated needs two people; conflict recusal; no secrets in review bundles. **Cat** |
| **Detection** | Unusual publish rate; signer/reviewer mismatch; owner reports. **Cat** |
| **Response** | Freeze reviewer; rotate if the signer key was used on unreviewed bytes; warn listings from the window; do not remote-wipe owners. **Cat, Own** |
| **Residual** | A dual-control failure is a catalog compromise [§5.5](#55-catalog-compromise). |

### 5.20 Abandoned software

Unmaintained app with a live pairing and an expired security mailbox.

| | |
| --- | --- |
| **Prevention** | 18-month inactivity + bounced security contact → `abandoned`, delist from default discovery, warn if elevated or subscription mode. **Cat** |
| **Detection** | Clock + bounce. **Cat** |
| **Response** | Warn; do not uninstall. Recovery is a verified new version. **Cat, Own, Pub** |
| **Residual** | Owners who keep running it. Abandoned does not mean benign. |

## 6. Responsibilities

### Owner

- Install only from hash-verified artifacts.
- Pair with the minimum scopes; never grant `sessions:all`, `approvals`, or `remote_control` for convenience.
- Keep provider keys in daemon owner files, not in apps.
- Watch catalog warnings when they exist; revoke tokens yourself.
- Understand that listing ≠ permission from Anthropic/OpenAI/Cursor.

### Publisher

- File an accurate manifest; bump a version for every expansion.
- Never collect daemon or provider credentials.
- Run each user against that user's daemon only.
- Respond to security mail; rotate artifact keys.
- Show usage/limit events and the subscription disclosure before hosted backends.

### Reviewer / catalog

- Run Stage A without skipping secret scan.
- Do elevated reviews with two people; recuse on conflicts.
- Label provider claims as fact / marketplace policy / unresolved.
- Issue revocation levels without mutating owner machines.
- Re-check official provider pages before a public launch.

## 7. Mapping to future tests

Without executing submissions, later tests can still lock this model:

- Schema rejects unknown fields, `admin`, and `never_handles_provider_credentials: false`.
- Permission-diff fixtures mark added scopes/origins/destinations as expansions.
- Synthetic Stage C (when built) fails closed on undeclared DNS.
- Daemon hook tests (when built) show that delist does not revoke keys or delete sessions.
