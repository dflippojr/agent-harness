# Curated app catalog v1

Design-only (issue [#28](https://github.com/dflippojr/agent-harness/issues/28)). This document specifies an optional
catalog of third-party apps that talk to a user's own daemon. It does not ship a catalog service, installer, review
runner, daemon enforcement, or Agent Harness Web marketplace UI.

Companion artifacts:

- Threat model: [`marketplace-threat-model.md`](marketplace-threat-model.md)
- Manifest schema: [`marketplace-manifest.schema.json`](marketplace-manifest.schema.json)
- Ordinary example: [`marketplace-manifest.ordinary.example.json`](marketplace-manifest.ordinary.example.json)
- Elevated-risk example: [`marketplace-manifest.elevated.example.json`](marketplace-manifest.elevated.example.json)

Contracts this design is written against (shipped on `main` as of 2026-09-17):

| Contract | Source | Version |
| --- | --- | --- |
| App API, scopes, pairing, hosted backends | [`app-api.md`](app-api.md), `harness/apps.py` | `/api/v1` **1.7** |
| Owner API, `admin` prohibition, per-app provider policy | [`admin-api.md`](admin-api.md), `harness/admin.py` | `/api/admin/v1` **1.4** |
| Exact-origin browser pairing and SSE tickets | [`app-api.md`](app-api.md) § Pair a separately hosted browser app | App API 1.2+ |
| Capability discovery (`full` / `service`) | [`service-profile.md`](service-profile.md), `Config.capabilities()` | App API 1.4+ |
| SDK / OpenAPI lockstep | [`app-api.md`](app-api.md), `sdk/harness_client.py` | App API 1.5+ |
| Per-app provider allowlists and sanitized status | [`admin-api.md`](admin-api.md) § Per-app provider credentials | App API 1.6 / Owner API 1.3 |
| Additive versioning | [`app-api.md`](app-api.md) § Versioning | path-major, additive-within-v1 |
| First-party Control Center (not a catalog app) | [`control-center.md`](control-center.md) | owner token `ho-`, not `ha-` |

A later public launch must repeat the [provider-policy review](#provider-policy-matrix) against live official
sources. The 2026-09-17 matrix is evidence for this design, not permission to ship.

## 1. Product boundary

The catalog is a **curated discovery and review index**.

It is not:

- a package manager, installer, or auto-updater;
- an artifact mirror or CDN for binaries;
- an execution host, multi-tenant agent runtime, or shared daemon;
- a payment processor, revenue-share, or billing broker;
- a mint, store, or transport for daemon tokens (`ha-`, `ho-`, `hk-`, pairing codes `hp-`) or provider credentials;
- a way to prove or technically enforce an external app's commercial behavior.

An entry links to publisher-owned source, release artifacts, and install/uninstall instructions. Installation is an
explicit owner action outside the catalog. Pairing is a separate explicit owner action in the daemon
(`Settings → Apps` or `POST /pairing-codes` then `POST /api/v1/pair`). Catalog presence grants no token, scope,
origin, or provider assignment.

Every listed app must run against **that user's own daemon**. An app must not proxy other people through one
person's daemon, Tailscale identity, hosted-provider subscription, or API key.

Listing policy may require a free/non-commercial disclosure and may delist violations. Neither the daemon nor the
catalog can prove what an installed binary actually charges for.

Marketplace publication does not imply that Anthropic, OpenAI, or Cursor permit every authentication mode the app
would like to use. Review records must label each claim as **provider fact**, **marketplace policy**, or
**unresolved**.

Agent-written skill publishing remains issue [#17](https://github.com/dflippojr/agent-harness/issues/17) and is out
of scope.

## 2. Roles

| Role | Who | Authority | Must not |
| --- | --- | --- | --- |
| **Owner** | Person who runs the daemon | Installs software, approves pairing, grants scopes and origins, assigns provider policy, revokes `ha-` tokens, decides catalog warnings | Treat a listing as a grant of access |
| **Publisher** | Person or org that submits an app | Owns source, artifacts, support contact, and disclosures | Receive daemon or provider credentials; run one daemon for many users |
| **Reviewer** | Catalog operator doing a version review | Accept, request changes, reject, suspend, delist, warn | Silently change local owner state; keep a personal copy of secrets found in a bundle (must rotate/report) |
| **Catalog signer** | Role that signs published snapshots | Publish immutable per-version records; rotate catalog keys | Re-sign a mutated version as if it were the original |
| **Abuse/security intake** | Shared catalog mailbox | Receive vulnerability and abuse reports; open incident records | Store reporter secrets or provider keys in tickets |
| **Provider** | Anthropic, OpenAI, Cursor (and future backends) | Their terms, billing, and enforcement | A party to the catalog contract. The catalog does not speak for them |
| **Agent Harness Web** | Future first-party discovery UI | Display signed listings and owner-facing consent copy | Pair, install, or mint tokens by itself |

Control Center is a first-party owner client, not a catalog app. It uses `/api/v1` for sessions and
`/api/admin/v1` with an `admin` owner credential (`ho-` or Tailscale/localhost). Catalog apps never receive `admin`.

## 3. Trust boundaries

```
                    publisher-owned
            ┌──────────────────────────┐
            │ source repo, releases,   │
            │ install docs, SBOM, keys │
            └────────────┬─────────────┘
                         │ HTTPS links only
                         ▼
catalog  ┌─────────────────────────────────────────┐
         │ signed index + immutable review records │  never stores ha-/ho-/hk-/hp-
         │ no artifacts, no credentials, no exec   │  never stores provider keys
         └─────────────────┬───────────────────────┘
                           │ discovery (future Web UI)
                           ▼
owner    ┌─────────────────────────────────────────┐
machine  │ explicit install (outside catalog)      │
         │ explicit pairing in the daemon          │
         │ scoped ha- token, exact origin, policy  │
         └───────┬─────────────────────┬───────────┘
                 │                     │
                 ▼                     ▼
         ┌───────────────┐     ┌───────────────────┐
         │ app process   │     │ harness daemon    │
         │ (not in the   │ API │ /api/v1 only      │
         │  daemon       │────▶│ sandbox + CLIs    │
         │  sandbox)     │     │ provider volumes  │
         └───────────────┘     └───────────────────┘
```

Boundaries the catalog must not cross:

1. **Catalog ↛ daemon credentials.** No listing, review bundle, example, or test may contain live or sample
   `ha-` / `ho-` / `hk-` / `hp-` secrets, provider API keys, OAuth tokens, or login caches
   (`~/.claude`, `~/.codex/auth.json`, `~/.cursor`).
2. **Catalog ↛ install.** Publishing does not write files on an owner's machine, start processes, or mutate
   SQLite keys.
3. **App ↛ provider login.** Only the daemon talks to unmodified `claude` / `codex` / `agent` CLIs. Apps send
   sessions, context, and tools through `/api/v1`. See [`app-api.md`](app-api.md) § Subscription backends.
4. **App ↛ other users.** One daemon, one owner. No multi-tenant proxy.
5. **Reviewer sandbox ↛ host.** Future dynamic analysis (not executed by this issue) uses synthetic credentials
   and data, no host mounts, and no network except a recorded allowlist.
6. **Catalog ↛ owner API.** Apps cannot call `/api/admin/v1`. `admin` is not an app scope.

Code the owner installs runs **outside** the daemon sandbox. The catalog can disclose that, warn, and later hint
that a pairing should be refused; it cannot sandbox a random native or browser app.

## 4. Catalog architecture

v1 is a **signed, append-only index of immutable per-version records**.

### 4.1 Objects

| Object | Mutability | Contents |
| --- | --- | --- |
| Publisher account | Mutable metadata; `publisher_id` immutable | Display name, verified domains, security/support contacts, status (`active`, `suspended`, `banned`) |
| App identity | `app_id` immutable | Publisher, slug, created time. Never reused after revocation |
| Version record | Immutable after `approved` | Publisher manifest bytes, artifact digest list, review record, catalog signature |
| Listing projection | Derived | Latest **published** version plus active advisory flags (`warn`, `block_new_pairing`, `recommend_revoke`) |
| Advisory | Append-only | Revocation-level events that do not rewrite the version record |
| Audit event | Append-only | Who/what/when for every state change |

The catalog stores **links and hashes**, not binaries. `source_url`, `release_url`, `install_url`,
`uninstall_url`, `privacy_policy_url`, `issue_url`, and `sbom_url` must be `https` (loopback `http` is forbidden
in published records).

### 4.2 Identifiers

- `app_id`: lowercase reverse-DNS, immutable, pattern `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?){1,4}$`. First-party reserved prefixes: `dev.agent-harness.`, `com.agent-harness.`, `agent-harness.`. Typosquat review compares against reserved prefixes, live listings, and well-known product names.
- `version`: SemVer 2.0 core (`MAJOR.MINOR.PATCH`) plus optional pre-release. A published version string is never reused.
- `publisher_id`: catalog-assigned, immutable (`pub-` + 12 hex). Display names can change; IDs cannot.
- Daemon API keys remain local (`k-…` rows). They are not catalog IDs and must not appear in manifests.

Domain-backed `app_id` values require a one-time proof that the publisher controls the registrable domain (HTTPS
well-known file or DNS TXT). Publishers without a domain receive a catalog namespace `ahpub.<slug>.*` after
identity review. Changing the DNS owner of a listed domain is an **origin/ownership change** and needs a new
version plus review.

### 4.3 Signing

A published version record is the exact publisher JSON bytes plus the review object. The catalog computes:

- `publisher_manifest_sha256`: SHA-256 of the submitted publisher JSON bytes (no re-canonicalization);
- `catalog_record_sha256`: SHA-256 of the concatenated publisher bytes, a single newline, and the canonical
  review JSON (UTF-8, sorted keys, no insignificant whitespace);
- `catalog_signature`: detached signature over `catalog_record_sha256` with the current catalog signing key.

Clients that implement discovery later must verify the signature against a pinned catalog key set, then compare
artifact hashes at install time. This issue does not implement signing.

Key-compromise recovery is in [§12](#12-catalog-and-signing-key-compromise).

### 4.4 Forward compatibility

Schema id: `https://github.com/dflippojr/agent-harness/docs/marketplace-manifest.schema.json`
(`manifest_schema_version` currently `"1.0"`).

**v1.0 rule: unknown fields fail.** Every object sets `"additionalProperties": false`. A document with a field
not in this schema is invalid. Tests cover that rule.

Later additive fields require `1.x` (minor). Breaking changes require `2.0` and a new schema id. Catalog
software:

- **must** reject documents whose `manifest_schema_version` major number it does not implement;
- **may** accept a lower minor version of the same major;
- **must not** silently drop unknown fields of a newer minor it does not implement — refuse the listing instead.

This is stricter than `/api/v1`, which is additive for running apps. Catalog metadata is a review boundary; extra
fields are a review-bypass risk.

### 4.5 Relationship to the daemon

The catalog never calls a daemon. Future daemon hooks ([follow-up](#15-follow-up-implementation-issues)) may
**fetch** a signed advisory list and:

- show a warning on a paired app whose `app_id` matches a `warn` advisory;
- refuse to create a **new** pairing code for an `app_id` with `block_new_pairing`;
- recommend token revocation for `recommend_revoke`.

Those hooks still must not uninstall software, delete workspaces, or revoke tokens without an owner action.

## 5. Permission and review model

`GET /api/v1` currently advertises these app scopes (`harness.apps.SCOPES`). `admin` is an **owner** scope on
`/api/admin/v1` and is never granted to `kind=app` or `kind=device`.

Catalog presence grants nothing. The owner still creates a token or pairing code and must see the disclosure for
every requested scope at that moment. Expanding scopes later requires a new catalog version **and** owner
re-consent.

### 5.1 Risk tiers

| Tier | Meaning | Review | Owner consent |
| --- | --- | --- | --- |
| **standard** | Own-session work the owner already expects from a helper app | Standard human review | Shown at pairing; no extra prompt beyond the scope list |
| **elevated** | Cross-session visibility, deciding approvals, or starting Remote Control | Enhanced human review (two reviewers, or one reviewer plus catalog lead) | Conspicuous separate consent, not bundled under “install” or “pair with defaults” |
| **prohibited** | Not an app permission | Automatic reject | N/A |

### 5.2 Scope table

| Scope | Tier | What it allows today | Owner-facing disclosure (required copy) | Review requirement | Re-consent |
| --- | --- | --- | --- | --- | --- |
| `sessions` | standard | Create sessions; send messages and context; register tools; cancel; read **this app's** sessions and events | “This app can start agent tasks on your harness and read the tasks it created. It cannot see other apps' or your Control Center sessions.” | Confirm claimed tools/context match the app; no hidden `sessions:all` | New version if the justification or data flow changes; no re-consent for patch versions that keep this scope |
| `sessions:all` | elevated | `GET` every session, not only the app's own | “This app can **read every session on this harness**, including work you started yourself and work other apps started. It cannot create sessions unless it also has `sessions`.” | Enhanced review of need; deny dashboards that only want “status” without a user-visible reason | Always on grant and on any later addition of this scope |
| `approvals` | elevated | Approve or deny tool calls in the **app's own** sessions (normally the owner approves from the phone) | “This app can **approve or deny tool calls** in its own tasks, including shell and file changes the agent requests. That is a substitute for you tapping Approve.” | Enhanced review of the in-app consent UI; require a user-visible decision log | Always on grant and on later addition |
| `images` | standard | `POST /api/v1/images` and download PNGs; takes the GPU exclusively for a batch | “This app can generate images on your harness. Image jobs pause language-model work for a few minutes.” | Confirm prompts are owner-initiated; no silent bulk generation | Re-consent only if adding this scope |
| `inference` | standard | OpenAI/Anthropic-compatible `/v1` against the tower model | “This app can call your local model as a raw completion API, outside an agent session. That still uses the same GPU.” | Confirm the app is not a public proxy; no unauthenticated forwarding | Re-consent if adding this scope, and reject any app that would expose `/v1` to the public internet |
| `remote_control` | elevated | Start/stop Claude Code Remote Control in trusted tower project folders; those sessions **bypass** harness queue, sandbox, and approvals | “This app can **start Claude Remote Control** in a project folder. Those sessions run as native Claude Code on your machine: no harness queue, sandbox, or harness approvals.” | Enhanced review; require the app to show `trusted` / `running` state and never silently spawn | Always on grant and on later addition |
| `admin` | prohibited | Owner API | Not shown. Catalog and schema reject it. | Automatic reject | N/A |

`sessions:all` is read-only for other sessions (the daemon allows `GET` with that scope without `sessions`). An
app that also wants to create work still lists `sessions`. Reviewers treat the pair as elevated if `sessions:all`
is present.

Browser apps additionally declare `browser_origins`: exact origins as defined by `normalize_origin()` in
`harness/apps.py` (HTTPS except loopback HTTP; no path, query, fragment, or credentials). Pairing remains
owner-entered and exact-match. A catalog origin list is a **claim** the owner is asked to confirm, not a CORS
grant. Changing origins is an expansion: new version, review, re-consent.

### 5.3 Required capabilities and backends

The publisher lists:

- `required_capabilities.profile`: `full`, `service`, or `any`;
- `required_capabilities.modules`: subset of `local_model`, `homelab`, `memory_library`, `images`, `image_edit`,
  `jobs`, `gpu_guard`, `runners`, `remote_control`, `web`, `search`, `endpoint`, `notifications`, `backup`, `skills`;
- `required_backends`: subset of `local`, `claude`, `codex`, `cursor`.

`image_edit` is an opt-in module (~20 GB Qwen-Image-Edit). It is never part of an ordinary install and
depends on `images` / `local_model`.

Discovery uses `GET /api/v1` / `GET /health` `capabilities` and authenticated `GET /api/v1/backends`
`provider_policy`. An app must degrade or refuse when a backend is `allowed: false`. It must not scrape
owner-only fields, secret refs, or other apps' usage.

`min_daemon_api` / `max_daemon_api` are the inclusive `/api/v1` `api_version` range (currently `"1.0"` … `"1.7"`).
Additive v1 changes stay compatible; an app that depends on 1.6 provider policy must set `min_daemon_api` to
`"1.6"`. Unknown future 1.x versions are allowed up to `max_daemon_api`. Path-major `/api/v2` is out of this
range by definition.

## 6. Review pipeline

A version cannot be published unless every stage below is recorded. This issue does not execute submissions.

### 6.1 Stage A — automated checks (deterministic)

Run on the submitted publisher JSON and linked artifacts (downloaded into the review workspace, never into the
catalog database):

1. **Schema**: validate against `marketplace-manifest.schema.json`. Unknown fields fail.
2. **Identifier/version**: `app_id` reserved-prefix and typosquat check; version not reused; publisher owns the id.
3. **URL**: HTTPS; no credentials in URLs; no `javascript:` / `data:`; redirects stay on the declared host for
   source/release, or fail closed.
4. **License**: SPDX id is listed on the SPDX license list; `license_url` if `LicenseRef-` is used.
5. **Hash / signature / provenance**: every artifact has `sha256`; if `signature_status` is `signed`, a
   documented public key URL and signature file URL must resolve; `provenance_status` of `slsa` / `in_toto`
   requires a provenance URL. `unsigned` is allowed only for standard-tier apps and is disclosed to owners.
6. **SBOM**: URL or embedded CycloneDX/SPDX document; parseable; no secret-looking strings.
7. **Secret scan**: reject if the bundle contains token prefixes `ha-`, `ho-`, `hk-`, `hp-`, `sk-ant-`,
   `sk-`, `xox`, PEM private keys, `auth.json`, or env files with provider keys. Hits are an automatic reject
   plus publisher notification to rotate.
8. **Dependency confusion**: SBOM names that shadow well-known packages from a different registry/namespace fail
   unless the publisher proves ownership.
9. **Permission diff**: vs the previously published version (if any). Added scopes, origins, destinations,
   backends, or data stores mark the version as **expansion**.

Failure of A is `rejected` or `changes_requested` (linter-fixable issues). There is no human override that skips
A without an audit event named `auto_check_override` (catalog lead only; still cannot skip secret-scan fails).

### 6.2 Stage B — human review

Checklist version `review_checklist_version` (currently `"1.0"`) is stored on the review record. Reviewers
confirm:

1. Source corresponds to the distributed artifacts (reproducible build, documented bootstrap, or an explicit
   `source_matches_distributed_build: false` with a conspicuous owner disclosure).
2. Requested scopes match claimed behavior; elevated scopes have a specific, user-visible justification — not
   “future features”.
3. Install and uninstall instructions are complete, do not request provider credentials, and do not ask the
   owner to disable Tailscale ACLs or CORS.
4. Privacy, telemetry defaults, network destinations, filesystem paths, and subprocesses match the code sampled.
5. Provider-policy fit: authentication modes vs [§13](#13-provider-policy-matrix); usage/billing/limits UI copy;
   `never_handles_provider_credentials` is true and reflected in the client.
6. User-visible consent screens: pairing instructions, scope disclosures from [§5.2](#52-scope-table), and
   (for hosted backends) the subscription-usage wording in [`app-api.md`](app-api.md).
7. The app does not expose agent execution or `/v1` inference to an untrusted or public environment.
8. Conflict of interest declaration (reviewer is not the publisher and has no financial interest).

Elevated-tier apps require a second distinct reviewer. Disagreement escalates to the catalog lead. A lead who is
the publisher cannot approve their own app.

### 6.3 Stage C — dynamic analysis (future, not run here)

If later implemented:

- Disposable VM or container, destroyed after the run.
- Synthetic daemon, synthetic `ha-` test tokens, synthetic provider **stubs** (never a real subscription login
  volume, never a real API key).
- No host filesystem mounts.
- Network default-deny; only an explicit recorded allowlist (the destinations declared in the manifest).
- Recorded process, DNS, and filesystem traces become evidence links on the review record.
- Findings that contradict the manifest fail the review.

This issue does not execute untrusted submissions and does not add that runner.

### 6.4 Stage D — immutable review record

On `approved` / `rejected` / `published`, write a review object that includes:

- `decision`, `review_checklist_version`, `reviewed_at` (UTC), reviewer ids;
- `evidence_urls`;
- `publisher_manifest_sha256` and per-artifact `sha256`;
- permission-diff summary;
- provider-policy labels (`provider_fact` / `marketplace_policy` / `unresolved`) for each requested backend.

That record is never edited. Corrections are a new version or an advisory appended to the listing.

## 7. Lifecycle

### 7.1 States

| State | Who can enter it | Listing visible? | Notes |
| --- | --- | --- | --- |
| `draft` | Publisher | no | Local to the publisher; not a catalog record |
| `submitted` | Publisher | no | Bytes frozen for this attempt |
| `auto_checks` | Catalog | no | Stage A |
| `human_review` | Catalog | no | Stage B |
| `changes_requested` | Reviewer | no | Publisher may submit a new attempt (new bytes; version may stay if never published) |
| `rejected` | Reviewer or auto | no | Terminal for this version string if the version was never published |
| `approved` | Reviewer | no | Waiting for signer |
| `published` | Catalog signer | yes | Immutable version record |
| `suspended` | Catalog lead | no (or “temporarily unavailable”) | Investigation; installed owners may be warned |
| `delisted` | Catalog lead | no | Historical record retained |
| `abandoned` | Catalog (policy clock) | no, with abandoned badge if still resolvable | See [§11](#11-abandoned-apps) |
| `appealed` | Publisher | visibility of the underlying state unchanged | Clock on the appeal, not a silent re-publish |

Advisories (`warn_installed`, `block_new_pairing`, `recommend_revoke`, `emergency_compromise`) are **flags on
the listing**, not replacement states. A `published` app can carry `warn_installed`.

### 7.2 Transitions

```
draft → submitted → auto_checks ┬→ changes_requested → submitted
                                ├→ rejected
                                └→ human_review ┬→ changes_requested → submitted
                                                ├→ rejected → appealed → rejected | human_review
                                                └→ approved → published
published ─→ (new version submitted independently; old version stays published unless replaced as default)
published → suspended → published | delisted
published | suspended → delisted → appealed → published | delisted
published | delisted | suspended → abandoned
any published listing → advisory flags (warn / block pairing / recommend revoke / emergency)
```

Rules:

- A **new version** is a new `submitted` record. It does not rewrite a published version.
- The listing's “current” pointer may move to a newer published version. Owners are not auto-updated.
- Scope, origin, data-flow, provider-auth, monetization, or ownership **expansion** cannot be a patch-only bump
  under marketplace policy: bump at least `MINOR`, run full review, and require owner re-consent before the new
  scopes/origins are used. Publishers should bump `MAJOR` when the permission set grows.
- Ownership transfer of `app_id` requires a new version even if the binary is unchanged.

## 8. Revocation levels

Catalog decisions never silently uninstall software or delete owner data. Each level is an explicit action with
an audit event.

| Level | Catalog effect | Owner-visible effect (once daemon/Web hooks exist) | Local state the catalog may not change |
| --- | --- | --- | --- |
| **reject** | Version never published | Publisher sees reasons | none |
| **delist** | Removed from discovery; record retained | “No longer in the catalog.” Already-paired apps keep working until the owner revokes them | no uninstall, no token revoke |
| **warn_installed** | Advisory on the listing | Control Center / future Web UI shows a warning for paired apps whose `app_id` matches | no uninstall |
| **block_new_pairing** | Advisory | Daemon should refuse **new** pairing codes for that `app_id` once hooks exist; existing tokens still work | no revoke of existing `ha-` keys |
| **recommend_token_revocation** | Advisory | Owner is asked to revoke the app's tokens in Settings → Apps | owner must click revoke |
| **emergency_compromise** | Delist + warn + block new pairing + recommend revoke + public incident note | Same, with a security-severity banner | still no silent uninstall or data deletion |

Emergency response also includes: freeze the publisher account, invalidate pending submissions, rotate catalog
signing keys if the catalog itself is in the blast radius ([§12](#12-catalog-and-signing-key-compromise)), and
open a vulnerability record.

`block_new_pairing` and token revocation are **future daemon hooks**. Until those issues ship, the catalog can
only publish advisories. Design of the hook: match on declared `app_id` stored in app-key metadata when pairing
starts. Today's daemon keys have a local `name` but no catalog `app_id`; the hook issue must add an optional
`catalog_app_id` on pairing/key creation. This design forbids stuffing catalog IDs into token secrets.

## 9. Updates and re-consent

| Change | New catalog version? | Review? | Owner re-consent? |
| --- | --- | --- | --- |
| Bugfix, same scopes/origins/data-flow/provider-auth/monetization/owner | yes | abbreviated Stage A + diff review | no, unless an advisory says otherwise |
| Added scope, origin, destination, backend, collected data, subprocess, or filesystem path | yes | full A+B (C if available) | **yes**, before the new access is used |
| Provider auth mode change (e.g. adding `subscription`) | yes | full, with policy matrix | **yes** |
| Monetization / pricing disclosure change | yes | full | yes if paid features appear |
| Publisher / domain / signing-key ownership change | yes | full, treat as new publisher | **yes** |
| `source_matches_distributed_build` flipping to false | yes | full | **yes** |
| Catalog delist / warn / emergency | no new version required | incident review | warning only; access unchanged until owner acts |

Unsafe auto-update is forbidden as a listing policy: install docs may link to a publisher updater, but the
updater must not change granted scopes, origins, or provider modes without a new pairing/consent step. The
catalog will delist apps whose updater silently expands access. The harness cannot technically stop a native
updater the owner already installed; that residual risk is in the threat model.

## 10. Audit, retention, intake, appeal, conflicts

### 10.1 Audit log

Every transition, advisory, override, and reviewer assignment appends `{at, actor_id, action, app_id, version,
from, to, evidence_sha256}`. Audit events are immutable. Actors are reviewer/publisher ids, never IP-derived
owner identities from daemons (the catalog does not see daemons).

### 10.2 Retention

| Record | Retention |
| --- | --- |
| Published version bytes, hashes, signatures, review records | Life of the catalog + 7 years after delist |
| Rejected submissions and Stage A logs | 2 years |
| Abuse and vulnerability reports | 3 years after close, or 7 years if they led to emergency_compromise |
| Appeal records | 3 years |
| Draft unpublished publisher notes | 90 days after last edit, then delete |
| Reviewer sandbox traces (future) | 1 year; synthetic only |

Do not retain accidentally submitted secrets; replace the stored blob with a redaction marker and the hash of
the original, and require rotation.

### 10.3 Vulnerability intake

Security contact is `security_url` / `security_email` on the manifest plus a catalog address. Report handling:

1. Acknowledge within 3 business days.
2. Notify the publisher's security contact.
3. Publisher has 7 days to respond, 90 days for a fix on standard-tier issues, 14 days for actively exploited
   elevated-tier issues (catalog lead may shorten).
4. If the publisher misses the window or the issue is exploitable in the wild, issue `warn_installed` immediately
   and escalate revocation levels as needed.
5. Do not require reporters to attach live credentials.

### 10.4 Abuse reports

Spam, malware, credential harvesting, multi-tenant proxying, undisclosed telemetry, and commercial-policy
violations use the same intake. Commercial-use claims can justify **delist** under listing policy; they cannot
be technically proven. Record them as `marketplace_policy`, not `provider_fact`.

### 10.5 Publisher response and appeal

A publisher may appeal `rejected`, `delisted`, `suspended`, or an advisory within 30 days. Appeals are reviewed
by a reviewer who did not make the original decision. Appeal cannot silently restore a listing: a successful
appeal moves the record to `human_review` or `published` through the same signer path.

### 10.6 Reviewer conflicts

A reviewer must recuse if they are the publisher, a current contractor, or have equity/revenue share. Recusal is
an audit event. The catalog lead assigns a replacement. A catalog with a single reviewer must pause elevated-tier
approvals until a second person is available; standard-tier may proceed with a recorded exception.

## 11. Abandoned apps

A published app is `abandoned` when **all** of:

- no new version for 18 months, and
- security contact bounces or is unanswered for 30 days, and
- no publisher login for 18 months.

Abandoned apps are delisted from default discovery, keep their immutable records, and get `warn_installed` if
they still request elevated scopes or hosted-provider subscription mode. Owners keep running them until they
revoke. Recovery: a verified publisher (same `publisher_id` or proven domain) submits a new version through the
full pipeline.

## 12. Catalog and signing-key compromise

If a catalog signing key or the index store is compromised:

1. Publish a revocation of the key in a pre-announced out-of-band location (repository release + hashed status
   gist or equivalent; exact hosting is an implementation issue).
2. Generate a new key; sign a **key-rotation** snapshot that lists still-valid `catalog_record_sha256` values
   after a fresh integrity check against stored publisher bytes.
3. Do **not** re-sign records whose bytes do not match the stored hash.
4. Mark every listing `warn_installed` until the rotation snapshot is verified.
5. Versions published during the uncertain window are `suspended` pending re-review.
6. Daemons/Web UI (future) pin the new key only after owner-visible notice.

Local owner state still does not change automatically. A compromised catalog cannot be allowed to push uninstall
or token-revoke commands; that would turn catalog compromise into a remote-wipe primitive.

Publisher artifact-signing key theft is the publisher's incident: they rotate, submit a new version with new
digests, and the catalog issues `warn_installed` on the old version.

## 13. Provider-policy matrix

Checked **2026-09-17** from official pages (live fetch). This is the harness author's reading, not legal advice
and not provider approval. **Re-check from scratch before any public catalog launch.** Policy can change after
this design lands.

Column meanings:

- **Provider fact** — stated on the cited official page.
- **Marketplace decision** — Agent Harness listing rule. It can be stricter than a provider. It is revocable.
- **Unresolved** — not answered by the cited page; do not treat as permission.

Owner-managed official API keys remain the supported path for programmatic/unattended use
(`docs/admin-api.md` per-app `api_key` / `subscription_then_api_key` policies, secrets in owner files, never in
the catalog). Subscription-authenticated marketplace use is **provider-specific and revocable**. CLI capability
(`claude -p`, `codex exec`, `agent -p`) is not evidence that a third-party catalog app may use a subscription.

Marketplace-wide decisions (all backends):

1. Preserve an owner-managed official API-key path for programmatic use.
2. Do not infer marketplace permission from the fact that the daemon can launch a CLI.
3. Do not list an app that exposes agent execution to an untrusted or public environment.
4. Apps never handle, collect, relay, or display provider credentials, login caches, or OAuth tokens.
   `never_handles_provider_credentials` is required `true`.
5. Apps must surface daemon `rate_limit`, `billing_warning`, and `provider_policy` instead of hiding them.
6. Each user is billed to themselves; multi-user proxying is an automatic reject.

### 13.1 Claude (`claude` backend)

| Topic | Classification | Record (checked 2026-09-17) |
| --- | --- | --- |
| Auth modes the CLI provides | provider fact | OAuth (Claude Free/Pro/Max/Team/Enterprise) and API keys / 3P inference credentials. OAuth is “intended exclusively for purchasers of [those] subscription plans” and “ordinary use of Claude Code and other native Anthropic applications.” |
| Hosting unmodified CLI | provider fact | Preinstalling or running Claude Code in products/services (hosted sandboxes / agent infrastructure) requires Commercial Terms unless otherwise agreed; binary must be unmodified; customers may not pay for, resell, or intermediate usage; each end user authenticates and is billed themselves. |
| Third-party products | provider fact | “Developers building products or services that interact with Claude's capabilities, including those using the Agent SDK, should use API key authentication.” Anthropic “does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users.” Developers may not collect, store, or intermediate Claude.ai credentials or session tokens. |
| End-user sign-in to unmodified binary | provider fact | The same page says this “does not prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription, including where a platform hosts Claude Code” under the hosting conditions above. |
| Billing of `claude -p` / Agent SDK / third-party apps | provider fact | 2026-06-15 pause: Agent SDK, `claude -p`, and third-party app usage still draw from subscription usage limits. A previously announced separate monthly credit is **not** in effect. Limits assume ordinary, individual usage. Enforcement may occur without notice and lands on the user's account. |
| Credential handling | provider fact | Sign-in must complete through Anthropic's own flow. Apps/developers may not collect or store Claude.ai credentials or session tokens. |
| Marketplace decision | marketplace policy | Listed apps may **request** daemon-mediated Claude sessions. Default listing path is owner-assigned **API key** (`provider_policy.policy = api_key`). `subscription` or `subscription_then_api_key` is allowed only with conspicuous owner disclosure, the [`app-api.md`](app-api.md) wording, and a recorded `unresolved` that Anthropic may treat third-party app traffic as disallowed subscription routing. Catalog will revoke this permission if Anthropic clarifies it as disallowed. |
| Unresolved | unresolved | Whether a third-party catalog app that only calls `/api/v1` on the **owner's** daemon (never offering Claude.ai login itself) is “ordinary use of Claude Code” or “routing requests through … plan credentials on behalf of their users.” Sales contact is the provider's path; this catalog will not claim it is allowed. |
| Sources | — | [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance); [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) (pause update 2026-06-15). |

### 13.2 Codex (`codex` backend)

| Topic | Classification | Record (checked 2026-09-17) |
| --- | --- | --- |
| Auth modes | provider fact | ChatGPT subscription sign-in (`codex login`, device-code `--device-auth`); Platform API key (`codex login --with-api-key`); ChatGPT Enterprise Codex access tokens for trusted non-interactive local workflows. |
| Programmatic guidance | provider fact | “Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs. Don't expose Codex execution in untrusted or public environments.” Access tokens are for trusted scripts, schedulers, and private CI runners — not general API calls. Copying `~/.codex/auth.json` is documented as a headless fallback and must be treated as a password. |
| Billing / limits | provider fact | ChatGPT-plan sign-in uses that plan's Codex/ChatGPT allowances (varies by plan; Plus/Pro may buy credits). API key usage is billed on OpenAI Platform at API rates and does not use ChatGPT plan credits. Training/retention follows the sign-in method (ChatGPT workspace vs API org). |
| Credential handling | provider fact | Login caches live in `~/.codex/auth.json` or the OS keyring. Do not commit, paste into tickets, or share in chat. The harness already keeps this volume daemon-side and unread. Catalog/review bundles must never include it. |
| Marketplace decision | marketplace policy | Same as Claude: owner-managed API key is the default listed mode. Subscription mode is opt-in, disclosed, and revocable. **Reject** any app that would run Codex (or any backend) in a public or untrusted multi-tenant environment. **Reject** any app that copies, uploads, or asks for `auth.json` / `OPENAI_API_KEY` / `CODEX_ACCESS_TOKEN`. |
| Unresolved | unresolved | Whether ChatGPT-plan usage from a third-party app driving `codex` via a personal daemon is within “local work” contemplated by ChatGPT terms. Enterprise access-token rules do not automatically apply to consumer plans. |
| Sources | — | [Authentication](https://learn.chatgpt.com/docs/auth) (same content as [developers.openai.com/codex/auth](https://developers.openai.com/codex/auth)); [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan); [Access tokens](https://learn.chatgpt.com/docs/enterprise/access-tokens). |

### 13.3 Cursor (`cursor` backend)

| Topic | Classification | Record (checked 2026-09-17) |
| --- | --- | --- |
| Auth modes | provider fact | Browser login (`agent login`, `NO_OPEN_BROWSER=1` prints a URL) and user API keys (`CURSOR_API_KEY` or `--api-key`) documented for automation, scripts, and CI. Keys are created at Cursor Dashboard → API Keys. |
| Programmatic guidance | provider fact | Headless CLI print mode (`agent -p`) is documented for scripts and automation. `--force` / `--yolo` applies file changes without confirmation. |
| Terms restrictions | provider fact | ToS last updated 2026-09-03: users may not rent, lease, lend, or sell the Service. Account holders remain responsible for use. Optional third-party services are under their own terms. |
| Billing / limits | provider fact | Subscription pricing is set by Anysphere and may change with notice. Official CLI auth docs do **not** state whether `CURSOR_API_KEY` draws from the individual Cursor plan or a separate meter. |
| Credential handling | provider fact | Browser login stores credentials locally; API key is an env var or flag. Catalog never accepts either. |
| Marketplace decision | marketplace policy | Owner-managed `CURSOR_API_KEY` (daemon file ref) is the default listed mode. Subscription login via the daemon volume is disclosed and revocable. **Reject** apps that rent/lend a Cursor account. **Reject** public/untrusted execution. Print-mode `--force` is a harness sandbox concern (already containerized); the app still must not hide that writes happen. |
| Unresolved | unresolved | Whether a third-party catalog app using the owner's Cursor subscription through the daemon violates “rent, lease, lend, or sell.” Metering of API keys vs plan limits. No Cursor page reviewed on 2026-09-17 grants third-party marketplace rights. |
| Sources | — | [Headless CLI](https://cursor.com/docs/cli/headless); [CLI authentication](https://cursor.com/docs/cli/reference/authentication); [Terms of Service](https://cursor.com/terms-of-service) (updated 2026-09-03). |

### 13.4 How apps must show usage, billing, and limits

Required publisher claim (`usage_disclosure`):

- The app displays daemon-reported usage and rate-limit events for **its own** `GET /api/v1/backends` payload.
- It does not invent provider quota numbers.
- Before the first hosted-backend session it shows the disclosure in [`app-api.md`](app-api.md) (subscription
  counts as programmatic usage; provider may meter or limit; owner can switch to an API key in harness settings).
- It never asks the owner to paste a provider key into the app. The owner assigns keys in `/api/admin/v1/provider-credentials`.

## 14. Privacy and data-flow disclosures

Precise enough to test later. The publisher manifest must declare:

| Field | Testable rule |
| --- | --- |
| `network_destinations[]` | `{host, port, scheme, purpose}`. Future sandbox DNS not in this list is a fail. `*` is invalid. |
| `data_collected[]` | Categories (`session_prompt`, `session_transcript`, `approval_payload`, `image`, `local_file`, `identifier`, `other`) plus `sent_to` (destination id or `local_only`) and `retained` (`none`, `session`, `until_account_delete`, `publisher_defined` + days). |
| `telemetry` | `default` is `off` or `on`; if `on`, what is sent, opt-out URL, and that it is in `network_destinations`. Changing default `off` → `on` is an expansion. |
| `subprocesses[]` | Name, purpose, persists-after-exit (`boolean`). Background services that survive the UI must be disclosed. |
| `filesystem_access[]` | Paths or path classes (`app_config`, `user_documents`, `arbitrary_picker`, `workspace_via_daemon_only`) the app reads/writes **without** going through daemon APIs. Daemon workspace access is not listed here; it is session sandbox behavior. |
| `handles_daemon_token` | How the `ha-` token is stored (OS keychain, local file mode 0600, memory-only). Forbidden: URL query, logs, analytics, world-readable files. |

Reviewers sample-check these lists against source. Undeclared telemetry or destinations are delist-class abuse.

## 15. Follow-up implementation issues

None of these are built here. Proposed backlog keys:

1. **`marketplace-catalog-storage`** — signed index store, key management, immutable version records, advisory
   feed, audit retention.
2. **`marketplace-review-automation`** — Stage A checkers, evidence bundle, optional Stage C sandbox with the
   boundaries in [§6.3](#63-stage-c--dynamic-analysis-future-not-run-here).
3. **`marketplace-web-discovery`** — Agent Harness Web discovery and conspicuous consent UI; no pairing inside
   the catalog page except a deep-link into the owner's daemon Settings.
4. **`marketplace-daemon-revocation-hooks`** — optional `catalog_app_id` on keys/pairing; warn / block-new-pair
   / recommend-revoke; never silent uninstall or data deletion.

## 16. Future enforcement points (not v1 code)

When the follow-ups exist, enforcement is hint-and-consent, not remote control of the owner machine:

- Control Center Settings → Apps shows catalog advisories next to paired names.
- Pairing UI pre-fills claimed origins/scopes from a verified listing and still requires owner approval.
- `block_new_pairing` is enforced only at pairing-code creation.
- Token revoke remains an owner click.
- Provider policy remains owner-assigned on `/api/admin/v1/provider-credentials`.

## 17. Out of scope (reaffirmed)

Marketplace hosting code; package installation or auto-update; payments; technical enforcement of
non-commercial use; skill publishing (#17); general native-app sandboxing; executing untrusted submissions;
obtaining legal or provider approval.
