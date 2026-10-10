# Hub inventory

The optional daemon-side Hub module provides `GET /api/admin/v1/hub` and `harness hub status`.
The standalone Hub app lives in its own repository; Agent Harness Web does not consume this inventory.
Owner credentials and the exclusive Hub key can read it. Other apps, members and guests cannot.

## Local entries

`<config_dir>/hub.entries.json` contains `{"entries": [manifest, ...]}`. Each manifest uses
[marketplace-manifest.schema.json](marketplace-manifest.schema.json) unchanged: publisher `app` plus
`review`. Its `app.app_id` is the `catalog_app_id` used for matching; display names never match.
Unknown fields, missing review, bad ids, non-HTTPS origins, admin/unknown scopes, and duplicate ids fail validation.
After schema validation, ids must also satisfy the core key/pairing label contract (at most 120 characters,
no token prefixes). This uses the module-interface `normalize_catalog_app_id` helper; the manifest schema
is unchanged.
A missing file means an empty list; an unreadable or invalid file produces a named error and a doctor failure.

`harness hub entries init [--config-dir DIR]` creates the file from
[hub.entries.example.json](hub.entries.example.json) and refuses to overwrite any existing file.
`harness hub entries validate [file] [--config-dir DIR]` validates offline. Without a directory override,
both use `HARNESS_CONFIG_DIR` or the checkout's `config` directory. They require no daemon or token.

The initial Web and financial-planner entries have placeholder `https://example.invalid/...` URLs and
digests and an owner self-review record. Replace these on the daemon host to describe your apps. The Web entry
declares ordinary app permissions, not daemon administration. The reserved Web store is not a paired key, so it
does not automatically mark the Web entry paired.

Entries grant nothing. Every entry is `verified: false`. V1 performs no signature checks, canonical hash
checks, artifact digest checks, remote fetching or downloads. Those remain in #161, #162 and #164.

## Inventory contract

The response has three arrays:

- `modules`: known capability switches with `name` and `state` (`present`,
  `switched_off`, `absent`), plus optional `status` detail from the runtime hook. Hooks run concurrently,
  with a six-second timeout for both sync and async implementations. Sync hooks use one daemon thread
  per runtime; a timed-out probe remains in flight and later polls reuse it until it finishes, so blocked
  hooks cannot exhaust the shared executor or delay daemon shutdown. Only the first eight fields are
  considered; keys and string values are capped at 200 characters (long keys are dropped). Nested values,
  non-finite floats and integers larger than 64 bits are dropped. Hooks must still avoid secrets in scalars.
- `apps`: the safe key-list fields (id, name, kind, role, scopes, origins, catalog id, existing prefix,
  creation/use/revocation/erasure times), `token_age_seconds`, `state`, `errors` and
  `store` metadata (session counts by status, usage, error count, last error kind/time). No app store contents
  are opened or read. Owner/device keys are included as in the key list; keys without stores have no store metrics.
- `entries`: full `manifest`, `catalog_app_id`, `verified: false`,
  `state` (`paired` or `not_paired`), `paired` (matching key ids, including revoked
  keys), and `pending_pairing` (an unexpired pending, armed, claimed or approved app request exists).
  Both require a matching catalog id and manifest browser origin: a key must have nonempty origins all
  listed in `app.browser_origins`, and a pending request's origin must be listed there. Manifest origins
  use the same host-case and default-port normalization as pairing; the original manifest is preserved. A catalog label
  alone never marks a trusted entry paired or pending. Native keys and requests with no origin are
  reported separately under `unverified_origin` (`paired` key ids and a `pending_pairing` boolean);
  they do not affect the entry's state. These origin checks are attribution checks, not publisher verification.

Successful scoped App/device and owner/Hub bearer authentication records `last_used_at` in a background
main-store write, at most once per key per 60-second window, including session creation/polling and admin
reads. A pending write suppresses another even after the window expires. Inference accounting already
updates activity, so those requests do not queue an additional metadata write. Rejected scope/origin credentials do not
record activity, and a metadata-write failure never changes a completed operation’s response.

States: erasure pending takes precedence over revoked, then never used, active (age <= 15 minutes), idle
(age <= 7 days), and stale. Errors are flagged when the last recorded error is within 24 hours. Token age
is reported in seconds; there is no token expiry. `harness_hub_apps{state="..."}` counts keys per state
and `harness_hub_app_errors` counts keys with recent errors. Labels contain only the fixed state names.

`hub.enabled: false` keeps settings and answers disabled (400); its metrics become zero. With the package
removed or `module_packages: []`, the route answers 404, no Hub metrics/settings are contributed, and pairing
and keys work unchanged. The service profile must opt in with `modules.hub: true`.

Hub claim management remains independent of this optional module. `harness hub claim-status` is the
claim view formerly named `hub status`; approve, deny and release commands are unchanged.
The SDK's `Harness.hub_status()` reads the inventory with the owner's or Hub's credentials.
