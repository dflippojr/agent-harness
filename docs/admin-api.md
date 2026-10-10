# Agent Harness Server owner API (admin v1)

App/member operational trails (#471) are private: the owner `/audit` reader never traverses their stores or returns
session/tool/login-attempt ids or delegated end-user subjects. It includes only approved aggregate `namespace.erase`
receipts retained for 365 days, with App/account id, category, count, authenticated actor/key, reason, timestamp,
outcome and a fresh receipt correlation id. `started` without `ok` means completion remains unresolved; `unknown`
does not prove no effect happened. Inspect before repeating an external erase. See the [private audit contract](app-api.md#private-operational-audit-120-471)
for scoped reads, retention, failure responses, live erasure, backup rotation/restore and provider-controlled copies.

Machine-owner operations for Agent Harness Web and other first-party operator surfaces. Agent Harness Apps keep
using [`/api/v1`](app-api.md); they cannot call this surface, even with every App scope.

Base path: `/api/admin/v1`. Versioning is in the path (`/api/admin/v2` for breaking changes). FastAPI
serves the machine-readable schema at `/openapi.json`.

Bundled and separately hosted Agent Harness Web use this contract, and so does the Agent Harness CLI: every owner
setting and action Web offers has a `harness` command too ([management-parity.md](management-parity.md)). Unversioned Agent Harness Server operator routes
remain a compatibility surface and keep Tailscale owner/guest rules.

## Credentials

Two owner credentials are accepted:

1. **Tailscale/localhost owner identity**, with no `Authorization` header. Same trust as bundled Agent Harness Web:
   a listed `allowed_logins` entry is the owner. The `Tailscale-*` headers count only when tailscaled sent them
   ([INSTALL](INSTALL.md#local-callers)). A request without `Tailscale-User-Login` reached the loopback
   listener directly and is the owner only when it carries the local owner token (`X-Agent-Harness-Local-Token`, see
   [INSTALL](INSTALL.md#local-callers)); otherwise it gets **401**. Guests (`guests:` in `harness.local.yaml`) are refused for every `/api/admin` path.
2. **Owner bearer token.** `POST /keys` or `POST /api/admin/v1/keys` with
   `{"name": "agent-harness-web", "kind": "owner", "scopes": ["admin"]}`. The secret is shown once,
   starts with `ho-`, and is sent as `Authorization: Bearer ho-...`.

App tokens (`ha-…`, kind `app`) and device/inference tokens (`hk-…`, kind `device`) receive **403**
`app tokens cannot use the owner API`. The `admin` scope cannot be granted to those kinds.

An owner token may include an exact browser-origin allowlist for separately hosted Agent Harness Web. Create it from
the bundled Web UI's **Settings → Connection** page (or `POST /api/admin/v1/keys` with `kind: "owner"`, the `admin`
scope, and an `origins` array). Versioned API CORS is allowlisted by those live keys, and each actual owner request
also checks that the presented token was approved for the request's origin. See [`web.md`](web.md).

Existing owner records named `control-center` remain valid and visible as legacy Web connections. Agent Harness
Server does not rename or revoke credentials for this terminology change.

## Discovery

### `GET /api/admin/v1`

Requires owner credentials. Returns `api_version`, the `admin` scope description, accepted `auth`
methods, the Agent Harness Server `capabilities`, and the versioned `operations` list (`method` + `path`).
It also publishes the first-party protocol ranges and update hints described in [`compatibility.md`](compatibility.md).

## Endpoint index

Generated from the route registrations and the owner routes listed in `ADMIN_PATHS` and each module's `admin_paths` (`scripts/docs/build.py`; do not edit between the markers). Every route here needs owner credentials. `TODO` marks a handler with no docstring.

<!-- generated:begin admin-api-endpoints -->
| Method | Path | Auth | Summary | Source |
| --- | --- | --- | --- | --- |
| GET | `/api/admin/v1` | owner | TODO | `harness/admin.py` `admin_root` |
| GET | `/api/admin/v1/accounts` | owner | TODO | `harness/admin.py` `list_accounts` |
| POST | `/api/admin/v1/accounts` | owner | TODO | `harness/admin.py` `create_account` |
| GET | `/api/admin/v1/accounts/audit` | owner | TODO | `harness/admin.py` `account_audit` |
| GET | `/api/admin/v1/accounts/{user_id}` | owner | TODO | `harness/admin.py` `get_account` |
| PATCH | `/api/admin/v1/accounts/{user_id}` | owner | TODO | `harness/admin.py` `update_account` |
| POST | `/api/admin/v1/accounts/{user_id}/github-connection/reset` | owner | TODO | `harness/admin.py` `reset_member_github` |
| DELETE | `/api/admin/v1/accounts/{user_id}/google` | owner | TODO | `harness/google_signin_api.py` `google_unlink` |
| DELETE | `/api/admin/v1/accounts/{user_id}/google/invitation` | owner | TODO | `harness/google_signin_api.py` `google_invite_cancel` |
| POST | `/api/admin/v1/accounts/{user_id}/google/invitation` | owner | TODO | `harness/google_signin_api.py` `google_invite` |
| POST | `/api/admin/v1/accounts/{user_id}/google/revoke-sessions` | owner | TODO | `harness/google_signin_api.py` `google_revoke` |
| GET | `/api/admin/v1/apps/erasures` | owner | TODO | `harness/admin.py` `app_erasures` |
| GET | `/api/admin/v1/apps/{app_id}/limits` | owner | An App's session caps: what the owner set, what applies, and its sessions that count against them. | `harness/admin.py` `get_app_limits` |
| PUT | `/api/admin/v1/apps/{app_id}/limits` | owner | Set how many sessions an App may have running and queued (#524); a field left out keeps its value and null restores the default. Only the owner can; an App cannot. | `harness/admin.py` `set_app_limits` |
| POST | `/api/admin/v1/apps/{app_id}/restore` | owner | TODO | `harness/admin.py` `restore_app` |
| PUT | `/api/admin/v1/apps/{app_id}/retention` | owner | TODO | `harness/admin.py` `set_app_retention` |
| GET | `/api/admin/v1/audit` | owner | Owner-only review of every retained audit row, newest first, with a cursor (#467). | `harness/admin.py` `audit_review` |
| GET | `/api/admin/v1/backends` | owner (`admin` scope) | TODO | `harness/api.py` `backends` |
| PUT | `/api/admin/v1/backends/{name}` | owner (`admin` scope) | Settings → Backends: persist the default model (and effort, for hosted CLIs). | `harness/api.py` `update_backend` |
| GET | `/api/admin/v1/chats` | owner (`admin` scope) | TODO | `harness/api.py` `list_chats` |
| POST | `/api/admin/v1/chats` | owner (`admin` scope) | TODO | `harness/api.py` `create_chat` |
| GET | `/api/admin/v1/chats/options` | owner (`admin` scope) | TODO | `harness/api.py` `chat_options` |
| GET | `/api/admin/v1/chats/snippet-languages` | owner (`admin` scope) | TODO | `harness/api.py` `snippet_languages` |
| DELETE | `/api/admin/v1/chats/{ref}` | owner (`admin` scope) | TODO | `harness/api.py` `delete_chat` |
| GET | `/api/admin/v1/chats/{ref}` | owner (`admin` scope) | TODO | `harness/api.py` `get_chat` |
| PATCH | `/api/admin/v1/chats/{ref}` | owner (`admin` scope) | TODO | `harness/api.py` `rename_chat` |
| PUT | `/api/admin/v1/chats/{ref}` | owner (`admin` scope) | TODO | `harness/api.py` `rename_chat` |
| POST | `/api/admin/v1/chats/{ref}/cancel` | owner (`admin` scope) | TODO | `harness/api.py` `cancel_chat` |
| GET | `/api/admin/v1/chats/{ref}/events` | owner (`admin` scope) | TODO | `harness/api.py` `chat_events` |
| POST | `/api/admin/v1/chats/{ref}/messages` | owner (`admin` scope) | TODO | `harness/api.py` `send_chat_message` |
| POST | `/api/admin/v1/chats/{ref}/snippets` | owner (`admin` scope) | Run one snippet the owner chose, in a fresh sandbox. The result arrives as a snippet_result event. | `harness/api.py` `run_snippet` |
| POST | `/api/admin/v1/chats/{ref}/snippets/{run_id}/cancel` | owner (`admin` scope) | TODO | `harness/api.py` `cancel_snippet` |
| GET | `/api/admin/v1/config` | owner | TODO | `harness/config_api.py` `admin_config` |
| PATCH | `/api/admin/v1/config` | owner | TODO | `harness/config_api.py` `admin_patch` |
| POST | `/api/admin/v1/config/restart` | owner | TODO | `harness/config_api.py` `admin_restart` |
| POST | `/api/admin/v1/config/rollback` | owner | TODO | `harness/config_api.py` `admin_rollback` |
| GET | `/api/admin/v1/config/schema` | owner | TODO | `harness/config_api.py` `admin_schema` |
| POST | `/api/admin/v1/config/validate` | owner | TODO | `harness/config_api.py` `admin_validate` |
| GET | `/api/admin/v1/events` | owner (`admin` scope) | Status-level events for every session (the session list). Live only; reload the list to catch up. | `harness/api.py` `all_events` |
| GET | `/api/admin/v1/github-member-auth` | owner | TODO | `harness/admin.py` `github_member_auth` |
| PUT | `/api/admin/v1/github-member-auth` | owner | TODO | `harness/admin.py` `set_github_member_auth` |
| GET | `/api/admin/v1/github/projects/{project}/items` | owner (`admin` scope) | TODO | `harness/api.py` `github_items` |
| GET | `/api/admin/v1/github/projects/{project}/items/{number}` | owner (`admin` scope) | TODO | `harness/api.py` `github_item` |
| POST | `/api/admin/v1/github/sessions` | owner (`admin` scope) | TODO | `harness/api.py` `create_github_session` |
| GET | `/api/admin/v1/google-signin` | owner | TODO | `harness/google_signin_api.py` `google_status` |
| GET | `/api/admin/v1/gpu` | owner (`admin` scope) | TODO | `harness_modules/local_model/routes.py` `gpu` |
| POST | `/api/admin/v1/gpu/{action}` | owner (`admin` scope) | pause: hold the GPU for other uses until resumed. resume: end the hold, ignoring the current triggers (the model stays unloaded until something needs it). load: load the model now and keep it loaded for duration_seconds. unload: unload it now without holding the queue. | `harness_modules/local_model/routes.py` `gpu_action` |
| GET | `/api/admin/v1/images` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `list_images` |
| POST | `/api/admin/v1/images` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `create_image` |
| POST | `/api/admin/v1/images/cooldown` | owner (`admin` scope) | Drop an unused Images-tab warmup so the language model can come back. | `harness_modules/images/routes.py` `cooldown_images` |
| POST | `/api/admin/v1/images/uploads` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `upload_image` |
| POST | `/api/admin/v1/images/warmup` | owner (`admin` scope) | Start ComfyUI without a checkpoint. Called when the owner opens the Images tab. | `harness_modules/images/routes.py` `warmup_images` |
| DELETE | `/api/admin/v1/images/{iid}` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `delete_image` |
| GET | `/api/admin/v1/images/{iid}` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `get_image` |
| POST | `/api/admin/v1/images/{iid}/cancel` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `cancel_image` |
| POST | `/api/admin/v1/images/{iid}/edit` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `edit_image` |
| POST | `/api/admin/v1/images/{iid}/upscale` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `upscale_image` |
| GET | `/api/admin/v1/jobs` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `list_jobs` |
| POST | `/api/admin/v1/jobs` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `create_job` |
| GET | `/api/admin/v1/jobs/preview` | owner (`admin` scope) | The next few run times of a schedule, or why it's invalid. | `harness_modules/jobs/routes.py` `preview_cron` |
| DELETE | `/api/admin/v1/jobs/{jid}` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `delete_job` |
| GET | `/api/admin/v1/jobs/{jid}` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `get_job` |
| PUT | `/api/admin/v1/jobs/{jid}` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `update_job` |
| POST | `/api/admin/v1/jobs/{jid}/run` | owner (`admin` scope) | Run a job now, outside its schedule (the next scheduled run is unchanged). | `harness_modules/jobs/routes.py` `run_job` |
| GET | `/api/admin/v1/keys` | owner (`admin` scope) | TODO | `harness/api.py` `list_keys` |
| POST | `/api/admin/v1/keys` | owner (`admin` scope) | TODO | `harness/api.py` `create_key` |
| DELETE | `/api/admin/v1/keys/{kid}` | owner (`admin` scope) | TODO | `harness/api.py` `revoke_key` |
| GET | `/api/admin/v1/maintenance` | owner (`admin` scope) | TODO | `harness/api.py` `maintenance` |
| POST | `/api/admin/v1/maintenance/backup` | owner (`admin` scope) | TODO | `harness_modules/backup/routes.py` `maintenance_backup` |
| POST | `/api/admin/v1/maintenance/cleanup` | owner (`admin` scope) | TODO | `harness/api.py` `maintenance_cleanup` |
| POST | `/api/admin/v1/maintenance/image-archive/retention/apply` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `image_archive_retention_apply` |
| POST | `/api/admin/v1/maintenance/image-archive/retention/preview` | owner (`admin` scope) | TODO | `harness_modules/images/routes.py` `image_archive_retention_preview` |
| GET | `/api/admin/v1/me` | owner (`admin` scope) | TODO | `harness/api.py` `me` |
| GET | `/api/admin/v1/memory` | owner (`admin` scope) | The agent profile new sessions get, and the latest change agents saved to the memory library. | `harness_modules/memory_library/routes.py` `memory` |
| PUT | `/api/admin/v1/memory/profile` | owner (`admin` scope) | Owner edit of the agent profile from Settings. Commits and pushes like an approved memory write. | `harness_modules/memory_library/routes.py` `update_memory_profile` |
| GET | `/api/admin/v1/models` | owner (`admin` scope) | TODO | `harness/api.py` `models` |
| GET | `/api/admin/v1/models/status` | owner (`admin` scope) | TODO | `harness_modules/local_model/routes.py` `models_status` |
| POST | `/api/admin/v1/models/warm` | owner (`admin` scope) | Load the default model if it's asleep. The web app calls this when it opens. | `harness_modules/local_model/routes.py` `models_warm` |
| POST | `/api/admin/v1/notify/test` | owner (`admin` scope) | TODO | `harness_modules/notifications/routes.py` `notify_test` |
| GET | `/api/admin/v1/pairing-codes` | owner (`admin` scope) | Owner view. Codes themselves are shown only by the create response. | `harness/apps.py` `pairing_codes` |
| POST | `/api/admin/v1/pairing-codes` | owner (`admin` scope) | TODO | `harness/apps.py` `create_pairing_code` |
| DELETE | `/api/admin/v1/pairing-codes/{pid}` | owner (`admin` scope) | TODO | `harness/apps.py` `revoke_pairing_code` |
| GET | `/api/admin/v1/pairing-requests` | owner | Owner view of pairing requests: metadata, match codes and disclosure copy; never a token or verifier. | `harness/pairing_requests.py` `list_pairing_requests` |
| POST | `/api/admin/v1/pairing-requests` | owner | Arm a pre-approved slot for a Hub entry (#519): no secret; the App claims it with its own challenge. | `harness/pairing_requests.py` `arm_pairing_request` |
| POST | `/api/admin/v1/pairing-requests/{rid}/approve` | owner | Approve an App's pairing request with the match code it shows. Elevated scopes need acknowledge_elevated. No token exists until the App redeems. | `harness/pairing_requests.py` `approve_pairing_request` |
| POST | `/api/admin/v1/pairing-requests/{rid}/confirm` | owner | Confirm a native App's claim on an armed slot with the match code it shows; then it may redeem. | `harness/pairing_requests.py` `confirm_pairing_request` |
| POST | `/api/admin/v1/pairing-requests/{rid}/deny` | owner | Deny a pairing request, or withdraw an armed slot or an approval the App has not redeemed yet. | `harness/pairing_requests.py` `deny_pairing_request` |
| GET | `/api/admin/v1/profile` | owner (`admin` scope) | TODO | `harness/api.py` `profile` |
| PUT | `/api/admin/v1/profile` | owner (`admin` scope) | TODO | `harness/api.py` `update_profile` |
| GET | `/api/admin/v1/projects` | owner (`admin` scope) | TODO | `harness/api.py` `projects` |
| POST | `/api/admin/v1/projects` | owner (`admin` scope) | TODO | `harness/api.py` `create_project` |
| GET | `/api/admin/v1/provider-credentials` | owner | TODO | `harness/admin.py` `provider_credentials` |
| POST | `/api/admin/v1/provider-credentials` | owner | TODO | `harness/admin.py` `set_provider_credential` |
| DELETE | `/api/admin/v1/provider-credentials/{credential_id}` | owner | TODO | `harness/admin.py` `revoke_provider_credential` |
| GET | `/api/admin/v1/queue` | owner (`admin` scope) | TODO | `harness/api.py` `queue` |
| GET | `/api/admin/v1/remote-control` | owner (`admin` scope) | TODO | `harness_modules/remote_control/routes.py` `rc_status` |
| POST | `/api/admin/v1/remote-control/discovery/scans` | see source | TODO | `harness_modules/remote_control/discovery_api.py` `start` |
| DELETE | `/api/admin/v1/remote-control/discovery/scans/{scan_id}` | see source | TODO | `harness_modules/remote_control/discovery_api.py` `cancel` |
| GET | `/api/admin/v1/remote-control/discovery/scans/{scan_id}` | see source | TODO | `harness_modules/remote_control/discovery_api.py` `status` |
| POST | `/api/admin/v1/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote` | see source | TODO | `harness_modules/remote_control/discovery_api.py` `promote` |
| DELETE | `/api/admin/v1/remote-control/folders/{slug}` | see source | TODO | `harness_modules/remote_control/discovery_api.py` `remove` |
| POST | `/api/admin/v1/remote-control/{project}` | owner (`admin` scope) | TODO | `harness_modules/remote_control/routes.py` `rc_launch` |
| POST | `/api/admin/v1/remote-control/{project}/stop` | owner (`admin` scope) | TODO | `harness_modules/remote_control/routes.py` `rc_stop` |
| POST | `/api/admin/v1/remote-control/{project}/trust` | owner (`admin` scope) | TODO | `harness_modules/remote_control/routes.py` `rc_trust` |
| GET | `/api/admin/v1/resources` | owner (`admin` scope) | TODO | `harness_modules/local_model/routes.py` `gpu` |
| GET | `/api/admin/v1/resources/diagnostics` | owner (`admin` scope) | One reading for Actions -> Resources (VRAM, RAM, GPU/CPU load, model and guard state). Not polled. | `harness_modules/local_model/routes.py` `resources_diagnostics` |
| POST | `/api/admin/v1/resources/{action}` | owner (`admin` scope) | pause: hold the GPU for other uses until resumed. resume: end the hold, ignoring the current triggers (the model stays unloaded until something needs it). load: load the model now and keep it loaded for duration_seconds. unload: unload it now without holding the queue. | `harness_modules/local_model/routes.py` `gpu_action` |
| GET | `/api/admin/v1/runner-pairing-codes` | owner (`admin` scope) | Owner view. Native pairing codes and runner tokens are never included. | `harness_modules/runners/routes.py` `runner_pairing_codes` |
| POST | `/api/admin/v1/runner-pairing-codes` | owner (`admin` scope) | TODO | `harness_modules/runners/routes.py` `create_runner_pairing_code` |
| DELETE | `/api/admin/v1/runner-pairing-codes/{pid}` | owner (`admin` scope) | TODO | `harness_modules/runners/routes.py` `revoke_runner_pairing_code` |
| GET | `/api/admin/v1/runners` | owner (`admin` scope) | TODO | `harness_modules/runners/routes.py` `runners` |
| POST | `/api/admin/v1/runners/{name}/update` | owner (`admin` scope) | TODO | `harness_modules/runners/routes.py` `runner_update` |
| GET | `/api/admin/v1/search` | owner (`admin` scope) | Full-text search over past sessions. Passages mark matches with \u0002 ... \u0003. | `harness_modules/search/routes.py` `search_sessions` |
| GET | `/api/admin/v1/sessions` | owner (`admin` scope) | TODO | `harness/api.py` `list_sessions` |
| POST | `/api/admin/v1/sessions` | owner (`admin` scope) | TODO | `harness/api.py` `create_session` |
| GET | `/api/admin/v1/sessions/{ref}` | owner (`admin` scope) | TODO | `harness/api.py` `get_session` |
| PATCH | `/api/admin/v1/sessions/{ref}` | owner (`admin` scope) | TODO | `harness/api.py` `patch_session` |
| PUT | `/api/admin/v1/sessions/{ref}` | owner (`admin` scope) | TODO | `harness/api.py` `patch_session` |
| GET | `/api/admin/v1/sessions/{ref}/approvals` | owner (`admin` scope) | TODO | `harness/api.py` `approvals` |
| POST | `/api/admin/v1/sessions/{ref}/approvals/{approval_id}` | owner (`admin` scope) | TODO | `harness/api.py` `decide` |
| POST | `/api/admin/v1/sessions/{ref}/cancel` | owner (`admin` scope) | TODO | `harness/api.py` `cancel` |
| GET | `/api/admin/v1/sessions/{ref}/changes` | owner (`admin` scope) | TODO | `harness/api.py` `changes` |
| GET | `/api/admin/v1/sessions/{ref}/checkpoints` | owner (`admin` scope) | TODO | `harness/api.py` `session_checkpoints` |
| POST | `/api/admin/v1/sessions/{ref}/checkpoints/{turn}/fork` | owner (`admin` scope) | TODO | `harness/api.py` `fork_checkpoint` |
| POST | `/api/admin/v1/sessions/{ref}/checkpoints/{turn}/rewind` | owner (`admin` scope) | TODO | `harness/api.py` `rewind_checkpoint` |
| GET | `/api/admin/v1/sessions/{ref}/events` | owner (`admin` scope) | Server-sent events: replays persisted events after `after`, then streams live ones. Ephemeral events (token deltas, queue moves) have `seq: null` and are never replayed. | `harness/api.py` `events` |
| POST | `/api/admin/v1/sessions/{ref}/messages` | owner (`admin` scope) | TODO | `harness/api.py` `send_message` |
| GET | `/api/admin/v1/sessions/{ref}/metrics` | owner (`admin` scope) | Owner-only per-turn context-efficiency metrics for one agent session (#159). | `harness/api.py` `session_metrics` |
| POST | `/api/admin/v1/sessions/{ref}/rerun` | owner (`admin` scope) | TODO | `harness/api.py` `rerun` |
| GET | `/api/admin/v1/sessions/{ref}/review-comments` | owner (`admin` scope) | TODO | `harness/api.py` `review_comments` |
| POST | `/api/admin/v1/sessions/{ref}/review-comments` | owner (`admin` scope) | TODO | `harness/api.py` `add_review_comment` |
| POST | `/api/admin/v1/sessions/{ref}/review-comments/send` | owner (`admin` scope) | Send the drafted line comments to the agent as one follow-up message. | `harness/api.py` `send_review_comments` |
| DELETE | `/api/admin/v1/sessions/{ref}/review-comments/{comment_id}` | owner (`admin` scope) | TODO | `harness/api.py` `delete_review_comment` |
| POST | `/api/admin/v1/sessions/{ref}/review/{action}` | owner (`admin` scope) | merge \| push \| discard the session's git branch. | `harness/api.py` `review` |
| POST | `/api/admin/v1/sessions/{ref}/secret-findings/fix` | owner (`admin` scope) | Ask agent to fix: one draft review comment per open secret-scan finding (send them like any draft). | `harness/api.py` `secret_findings_fix` |
| POST | `/api/admin/v1/sessions/{ref}/secret-findings/{fingerprint}/dismiss` | owner (`admin` scope) | Owner-only: dismiss one secret-scan finding with a reason (audited). | `harness/api.py` `dismiss_secret_finding` |
| POST | `/api/admin/v1/sessions/{ref}/taint/clear` | owner (`admin` scope) | TODO | `harness/api.py` `clear_taint` |
| GET | `/api/admin/v1/sessions/{ref}/transcript` | owner (`admin` scope) | TODO | `harness/api.py` `get_transcript` |
| GET | `/api/admin/v1/skills` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skills_overview` |
| GET | `/api/admin/v1/skills/enabled` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skills_enabled` |
| DELETE | `/api/admin/v1/skills/proposals/{pid}` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_delete_draft` |
| GET | `/api/admin/v1/skills/proposals/{pid}` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_proposal` |
| POST | `/api/admin/v1/skills/proposals/{pid}/install` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_install` |
| POST | `/api/admin/v1/skills/proposals/{pid}/reject` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_reject` |
| POST | `/api/admin/v1/skills/proposals/{pid}/reopen` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_reopen` |
| POST | `/api/admin/v1/skills/proposals/{pid}/review` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_hosted_review` |
| POST | `/api/admin/v1/skills/{slug}/disable` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_disable` |
| POST | `/api/admin/v1/skills/{slug}/enable` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_enable` |
| GET | `/api/admin/v1/skills/{slug}/export` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_export` |
| PUT | `/api/admin/v1/skills/{slug}/projects` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_projects` |
| POST | `/api/admin/v1/skills/{slug}/rollback` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_rollback` |
| POST | `/api/admin/v1/skills/{slug}/uninstall` | owner (`admin` scope) | TODO | `harness_modules/skills/routes.py` `skill_uninstall` |
| GET | `/api/admin/v1/smart-approvals` | owner (`admin` scope) | TODO | `harness/api.py` `smart_approvals` |
| PUT | `/api/admin/v1/smart-approvals` | owner (`admin` scope) | TODO | `harness/api.py` `update_smart_approvals` |
| GET | `/api/admin/v1/templates` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `list_templates` |
| POST | `/api/admin/v1/templates` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `create_template` |
| DELETE | `/api/admin/v1/templates/{tid}` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `delete_template` |
| PUT | `/api/admin/v1/templates/{tid}` | owner (`admin` scope) | TODO | `harness_modules/jobs/routes.py` `update_template` |
<!-- generated:end admin-api-endpoints -->

## Operations

Handlers match the unversioned operator routes. Bodies, query strings and response shapes are the
same; only the prefix and the owner credential check are new.

| Area | Paths |
| --- | --- |
| Identity | `/me`, `/profile` |
| Household accounts | `/accounts`, `/accounts/{user_id}`, `/accounts/audit` |
| Sessions | `/sessions`, `/sessions/{ref}`, messages, cancel, rerun, approvals, transcript, events, **metrics** |
| Review | `/sessions/{ref}/changes`, `/sessions/{ref}/review/{action}` (`merge` \| `push` \| `discard`) Line comments: `GET/POST /sessions/{ref}/review-comments`, `DELETE .../{comment_id}`, `POST .../send` (one follow-up; owner and members in their own sessions, never app tokens; the same paths under `/api/v1`). Secret scan (tower and Mac Runner sessions): `changes` carries `secret_scan`; `POST /sessions/{ref}/secret-findings/fix` drafts one comment per open finding in the diff and asks the agent to rewrite the branch's unpushed commits for a finding only in history (already-pushed ones: dismiss); `POST /sessions/{ref}/secret-findings/{fingerprint}/dismiss` `{reason}` (owner only, audited). See [app-api.md](app-api.md#secret-scan-before-push-and-merge) |
| Checkpoints | `GET /sessions/{ref}/checkpoints` lists the visible per-turn snapshots (one after each turn in which a mutating tool ran, even when an error, a cancel or the disk-quota stop ended the turn; tower agent sessions; a hidden ref `refs/harness/checkpoints/<session>/<turn>` in a host-side store outside the sandbox, never pushed). `POST /sessions/{ref}/checkpoints/{turn}/rewind` restores the workspace (tracked, untracked and deleted files; ignored files are left alone; a nested clone as its plain files, without its `.git`, and a clone made after the checkpoint is removed), resets the session branch to the HEAD recorded with the checkpoint (a detached HEAD is detached again at that commit), and puts back the model context and the agent's saved state and notes as they were at that turn (a checkpoint from before those were kept clears them); the listing then shows exactly the checkpoints up to that turn, so rewinding forward again to a later retained turn lists it again; local-model sessions only. A rewind is all or nothing: when a file it must remove or replace is held open by another program (Windows), or a step fails midway, the branch, index and files are put back, the context is untouched, and it answers 409 naming the files; when the restore succeeds but recording it fails (a database error), the files are put back the same way and it answers 500. If even that undo fails, the session is marked (`run.rewind_unsettled`) and a message answers 409 until a rewind succeeds. `POST /sessions/{ref}/checkpoints/{turn}/fork` `{prompt}` (201) starts a new session on its own branch from that checkpoint, with the agent's state and notes of that turn; a hosted CLI session forks with a transcript digest instead of its CLI state. A fork that fails leaves no new session, workspace or checkpoint store behind. Both need an idle session (409 otherwise) and hold it until they end: meanwhile a message, a merge, push or discard, or another rewind or fork of that session answers 409 (a daemon restart releases the hold). Up to 50 per session, oldest pruned; over a member's quota the oldest are pruned, and when even that would not fit the turn is marked not checkpointed and every existing checkpoint, rewound-past ones included, is kept. A snapshot that fails (disk full, git or database error) is likewise marked not checkpointed and leaves every existing checkpoint as it was; once a snapshot is recorded, a later failure cleaning up after it only logs a warning (#261) |
| Session safety | `POST /sessions/{ref}/taint/clear` (owner) clears a session's untrusted-content taint |
| GitHub tasks | `GET /github/projects/{project}/items` (`page`, `q`), `GET /github/projects/{project}/items/{number}`, `POST /github/sessions` (a `POST /sessions` body plus `number`). See [github-tasks.md](github-tasks.md) |
| Search | `/search`, `/events`, `/queue` |
| Projects and jobs | `/projects`, `/templates`, `/jobs` |
| Tokens | `/keys`, `/keys/{kid}`, `/pairing-codes`, `/pairing-codes/{pid}`, `/pairing-requests` and its `approve`, `confirm` and `deny` routes (see [Zero-touch pairing requests](#zero-touch-pairing-requests)) |
| App provider policy | `/provider-credentials`, `/provider-credentials/{credential_id}` |
| Mac pairing | `/runner-pairing-codes`, `/runner-pairing-codes/{pid}` |
| Maintenance | `/maintenance`, `/maintenance/cleanup`, `/maintenance/backup` (image-archive retention: see Images) |
| Configuration | `/config`, `/config/schema`, `/config/validate`, `/config/rollback`, `/config/restart` |
| GPU and models | `/gpu`, `/gpu/{pause\|resume}`, `/models`, `/models/status`, `/models/warm`, `/backends` |
| Smart approvals | `/smart-approvals` (`GET` status, `PUT` `{mode: off\|shadow\|auto}`; last writer with Settings `smart_approvals.mode`; `off` calls no reviewer) |
| Images (the images module, [`modules.md`](modules.md): absent on the service profile) | `/images`, `/images/uploads`, `/images/warmup`, `/images/cooldown`, `/images/{iid}`, `/images/{iid}/edit`, `/images/{iid}/upscale`, `/images/{iid}/cancel`, `/maintenance/image-archive/retention/{preview\|apply}` |
| Runners | `GET /runners` (status only; poll/results stay on the runner token) |
| Memory | `/memory`, `/memory/profile` |
| Skills | `/skills`, `/skills/enabled`, `/skills/proposals/{pid}`, install/reject/reopen/review, `/skills/{slug}/enable`, disable, rollback, uninstall, projects, export |
| Notifications | `/notify/test` |
| Remote Control | `/remote-control`, launch/stop, `/remote-control/{project}/trust` |

Not on this surface: `/api/v1` app sessions, `/v1` inference, runner `POST /runners/{name}/poll|results`,
and ntfy `POST /a/{token}/{decision}`. Household members receive **403** `members cannot use the owner API` on
every `/api/admin/v1` path and learn no admin data from the error.

## Maintenance

`POST /api/admin/v1/maintenance/backup` (also `/maintenance/backup`) is owner-only. The dated backup contains
`harness.sqlite3`, each App store in `apps/<app_id>.sqlite3` (including Web), config files and managed overlays,
the owner's `transcripts.zip`, and nonempty transcript archives for known members in
`transcripts/users/<user_id>.zip` and Apps in `transcripts/apps/<app_id>.zip`. All transcript archives use
deflate compression and relative file paths. Working directories, checkpoints and artifacts are excluded.

The backup result and `/maintenance` backup status add `transcript_archives` (the count of completed archives,
including the owner's) and `warnings` (a list of skipped links/reparse points and member/App archive failures).
A failed member or App archive is omitted and does not fail the backup; an owner archive failure still fails it.
`backup.keep_days` prunes the entire dated folder, including every transcript archive. A delete, retention or
revoke only erases live data: older backups retain the erased stores and transcripts until they rotate out.
Backups and maintenance remain owner-only; no new API exposes member transcripts. The OS-level machine owner
is outside the member privacy guarantee.

## Household accounts

`POST /api/admin/v1/accounts` with `login` (exact Tailscale login), `display_name`, and optional `disk_quota_bytes`,
`max_running`, and `max_queued` creates a member immediately with an opaque `user_id`. First login does not create
identity. `PATCH /api/admin/v1/accounts/{user_id}` can rename, rebind the login (same `user_id`, old login invalid
immediately), disable/re-enable, or change quota and concurrency. Disable cancels that member's running and queued
work and revokes their live streams; data stays. There is no Delete in v1.

Session admission (#524). A member's sessions parked on an approval, the Mac, an App tool reply or a provider limit
count toward both `max_queued` (new sessions past it get `429`) and `max_running` (a queued session waits for the GPU
or a hosted backend slot until one ends). Apps have the same two caps: `GET /api/admin/v1/apps/{app_id}/limits`
shows what is set, what applies and what counts against it; `PUT` with `max_running` and/or `max_queued` sets them
(a field left out keeps its value, `null` restores the `budgets.app_max_*` default). A session waiting on an approval
gives its hosted backend slot (and the GPU) to others and takes it back after the decision. Pending approvals are
denied after `budgets.approval_timeout_seconds` and their run ends (`stop_reason` `approval_expired`); members' and
Apps' runs end after `budgets.max_run_seconds` spent running (`budget_time`). The owner's own runs have no time
budget.

Member GitHub sign-in ([`member-github-auth.md`](member-github-auth.md)): `GET /api/admin/v1/github-member-auth`
returns `configured`, `enabled`, the last preflight result, and each member's coarse `status` and `last_used_at`
only (no URLs, usernames, or codes; `?refresh=true` reruns preflight). `PUT /api/admin/v1/github-member-auth`
with `{"enabled": true|false}` switches the feature (default off; disabling stops attempts and in-flight
credentialed Git without erasing). `POST /api/admin/v1/accounts/{user_id}/github-connection/reset` with
`{"confirm": true}` erases that member's stored GitHub credential. The owner cannot connect, test, or use it.

Member Google sign-in ([`google-signin.md`](google-signin.md)): `GET /api/admin/v1/google-signin` returns readiness,
the last preflight, and the exact redirect URI (`?refresh=true` reruns preflight). Per member,
`POST`/`DELETE /api/admin/v1/accounts/{user_id}/google/invitation` creates (shown once) or cancels a one-time link
code, `POST .../google/revoke-sessions` ends every Google Web session, and `DELETE .../google` with
`{"confirm": true}` unlinks. Account rows carry a coarse `google` object; never the `sub`, claims, or tokens. The
owner cannot start a Google authorization as a member.

`GET /api/admin/v1/accounts` returns aggregate metadata only: display name, login, account-id hint, enabled flag,
disk used/quota, running/queued counts, last activity, and limits. It never includes prompts, answers, filenames,
repo URLs, diffs, or transcript excerpts. `GET /api/admin/v1/accounts/audit` is owner-only (365-day retention) and
stores actor/target opaque ids, action, outcome, and timestamp — not prompts, diffs, tokens, or headers.

`GET /api/admin/v1/audit` (`harness audit list`) is the complete owner review of the same table: `{items,
next_before_id}`, newest first by immutable row id, `limit` 1-500 (default 200), exclusive `before_id`, exact
`actor_id`, `key_id`, `target_id`, `action` and `outcome` filters, and `since` (inclusive) / `until` (exclusive)
timestamps. `next_before_id` is the page's last id, or `null` once exhausted; rows inserted meanwhile never repeat
or skip rows already read. Each row adds `actor_kind` (`owner`, `owner_key`, `member`, `system`, `unknown`),
`key_id` (the validated owner bearer key, else empty), `source` (the server entry point, e.g. `admin_api`),
`target_kind` and a small `metadata` object. Rows from before this field existed read `unknown`/empty; no history
is guessed. Attribution comes only from what the server authenticated: a valid owner bearer key wins over the
ambient Tailscale/localhost identity, every allowlisted owner login is `owner`, and distinct owner keys differ by
`key_id`. Caller-supplied actor or source headers are ignored, and a rejected credential is never recorded.
`metadata` is an allowlist per action (changed field names, numeric limits and their old/new values, booleans,
stable reason codes); login and display-name values, secrets, paths, URLs and free text are never stored.

Owner operation audit (#470) covers approval decisions and smart auto-approval in owner Web sessions,
Review merge/push/discard, taint clear, checkpoint rewind/fork, owner jobs and cleanup/backup. The legacy,
admin and owner-key App routes carry the authenticated initiator; the executing agent is the separate
`session_id`. Notification buttons prove possession of an owner approval capability: actor `owner`,
source `notification_link`, empty key id, no link token or named-human claim. Auto-approval has a system
actor and source `agent`. Scheduled jobs use system/`job`; periodic maintenance uses system/`maintenance`.
Manual requests retain their authenticated key and API source. Member and other App session detail is
outside this main-store owner trail; each namespace's contract belongs to #471.

Job create/update/delete and their metadata audit share the main-store transaction. Updates record only
registered changed-field names and the enabled boolean, never job names, prompts, cron values or paths.
Cross-store session operations, job runs and maintenance commit `started` with a generated `operation_id`
before effects, then append one `ok` or conservative `unknown` settlement. Maintenance targets a generated
operation id and stores aggregate removed/kept/expired counts, never erased session ids or filenames.
An already-decided approval returns 409 and records one `failure` row with only the known target and
`already_decided` reason (a failed request, no new tool decision or cross-store effect);
a repeated notification press remains harmless and never claims another successful decision.
If the refusal audit write fails, a content-free warning is logged and the response remains 409.

A failed first audit commit returns 503 `audit_unavailable` and prevents starting the action. If an action
settles but its audit commit fails, the actual state remains and the started row stays unresolved.
Request callers receive 503 `audit_record_incomplete`, `operation_id`, `may_have_completed: true` and
`retryable: false`; inspect the session, job or maintenance status before deciding whether to retry.
Scheduled work logs a content-free warning without repeating an effect. Cancellation or a crash can leave
unresolved started evidence, including a worker thread still in flight. This is neither distributed rollback
nor an exactly-once promise. Existing Review gates, session events and checkpoint undo behavior still apply.

Use `harness audit list --action approval.decide` or `--action job.run`, or the equivalent admin query,
to reconstruct initiator and executing session after a restart. Match settlements to starts by
`metadata.operation_id`; a start without a settlement is unresolved, not success. These owner operations
use the existing 365-day main-store audit retention. Restoring an older backup rolls this trail back too;
there is no independent journal that survives a restore.

Audit rows are inserted in the same transaction as the account change they describe, so a failed audit write
(503 `audit_unavailable`) leaves the account unchanged. No route updates or deletes audit rows, and erasing a
session or account does not cascade to them. Rows older than 365 days are pruned when a new row is inserted. The
table is not tamper-evident: a host administrator can edit the SQLite file, and restoring a backup rolls the
history back to that snapshot.

### Credential, pairing and provider-grant history (#468)

The same audit table records who issued, revoked or restored a credential and who changed a billing grant. One row per
committed change, written in the same transaction as the change (an audit failure returns 503 `audit_unavailable` and
the key, pairing, App policy, grant or member ciphertext is unchanged; no new token is returned). The admin aliases and
the compatibility routes share one handler, so a change is never logged twice.

| Action | Written by | Target and metadata |
| --- | --- | --- |
| `key.create`, `key.revoke` | `POST /keys`, `DELETE /keys/{kid}` | opaque key id; `kind`, scope names; `catalog_app_id` on create |
| `pairing.create`, `pairing.revoke`, `pairing.redeem` | `/pairing-codes`, `POST /api/v1/pair` | pairing and key ids; scope names; `catalog_app_id` on create and redeem |
| `pairing_request.create`, `.claim`, `.approve`, `.deny`, `.redeem`, `.expire` | `/pairing-requests`, `POST /api/v1/pair/requests` and its `claim` and `token` routes, the expiry sweep | request and key ids; scope names; `catalog_app_id`; `browser`, `armed`, `acknowledged` (elevated scopes acknowledged) and `confirmed` (a native claim confirmed) |
| `runner_pairing.create`, `.revoke`, `.redeem` | `/runner-pairing-codes`, `POST /api/v1/runner-pair` | pairing and key ids |
| `app.restore`, `app.retention` | `POST .../apps/{id}/restore`, `PUT .../apps/{id}/retention` | App id; old/new retention days (or null) |
| `provider_grant.set`, `provider_grant.revoke` | `.../provider-credentials` | grant, previous grant and App ids; backend, policy, changed field names |
| `member_key.set`, `.delete`, `.test` | `/api/v1/me/api-keys/{backend}` | member id; backend, configured/replaced; outcome `ok`, `rejected`, `unavailable` or `noop` |

A successful pairing redemption is attributed to the key it minted (`actor_kind` `app` or `device`, `key_id` = the new
key), which proves possession of the bootstrap code and not a person; the metadata links the approved pairing id. An
unknown, expired, reused or origin-mismatched attempt records `unknown` with a `reason` enum only, never the submitted
code, origin, name or guessed id, and at most 200 such rows per action per hour (further ones are dropped, so an
unauthenticated caller cannot grow the table). An unknown target (a mistyped id) leaves `target_id` empty; a retry on an
already-revoked key records `noop` with `reason: already_revoked`. Secret values, secret references, file paths, model
lists, ciphertext, last-four snippets, token prefixes and hashes are never stored. These rows hold registry/account
metadata only, so erasing an App's payload leaves them (365-day retention, as for the other admin and credential events).

The durable owner scope remains `user_id = owner`. SQLite stores non-secret account metadata only: never Tailscale
session material, provider credentials, GitHub tokens, or Google tokens.

## Per-app provider credentials

The owner can give an Agent Harness App its own hosted-provider billing policy without giving either the Server
database or the App a plaintext provider key. First put the key in an owner-readable file and map an opaque name to it in
`harness.local.yaml`:

```yaml
provider_secret_files:
  invoice-automation: D:/Agents/harness/secrets/invoice-automation.key
```

Then assign the reference to the app token's `id`:

```http
POST /api/admin/v1/provider-credentials HTTP/1.1
Content-Type: application/json

{
  "app_id": "app-...",
  "backend": "claude",
  "secret_ref": "invoice-automation",
  "policy": "subscription_then_api_key",
  "models": ["claude-opus-5"]
}
```

Policies are `subscription`, `api_key`, and `subscription_then_api_key`. A subscription assignment must use an
empty `secret_ref`; the other policies require a configured reference. An empty `models` list allows every model,
while a nonempty list is an allowlist. Only one assignment is active for an app/backend pair; posting a replacement
revokes the prior assignment.

`GET /api/admin/v1/provider-credentials` returns assignments, opaque references, revocation times, and whether each
referenced file is available. It never returns a file path or key value. Revoke with
`DELETE /api/admin/v1/provider-credentials/{credential_id}`. Revocation stops an active provider process and blocks
new sessions.

Creating the first assignment puts that app into hosted-provider allowlist mode. Every unassigned hosted backend is
denied, and revoking the last assignment keeps the app managed and denied; it never falls back to a machine-wide
subscription or key. Local-model sessions are unaffected. Credential-store integration is intentionally outside
this file-based contract; protect the files with OS permissions and rotate them by replacing the file.

## App retention and erasure

Each App's sessions live in its own store and folder (`<data_dir>/apps/<app_id>/`, see
[App API](app-api.md#where-an-apps-data-lives)). The owner controls how long they stay and what happens on revoke
(#330 decision 5). These routes take an App's (or device key's) `id` from `GET /keys`.

- **Default retention.** `PUT /api/admin/v1/apps/{app_id}/retention` with `{"retention_days": 30}` erases that
  App's sessions once they have been idle 30 days (since their last event, or their creation), unless a session was
  created with its own `retention_days`, which wins. `{"retention_days": null}` keeps them until the App deletes
  them. Returns the key row; `404` for an owner key, an erased App or an unknown id, `422` for a value that isn't a
  positive number of days (at most 36500). `GET /keys` shows it as `retention_days`.
- **The sweep.** The maintenance cleanup (every `cleanup.interval_minutes`, hourly by default; also
  `POST /maintenance/cleanup`) erases expired sessions exactly as the App's `DELETE` would, whether or not the App
  is online. Its report includes `sessions_expired` and `apps_erased` counts. All cleanup result categories and the saved `last_cleanup` report contain counts rather than session, container or workspace identifiers.
- **Revoke.** `DELETE /keys/{kid}` on an App or device key kills its token at once and schedules the erasure of its
  whole store and folder 7 days later: `GET /keys` shows `revoked_at` and `erase_after` (Unix seconds), and the Apps
  card lists it under "Revoked: data to be erased" with the date. Owner keys have no erasure.
- **Agent Harness Web** (`app-web`) is the owner's own App (#330 decision 4): its store holds the owner's and members'
  sessions. It has no key, so `GET /keys` doesn't list it and `DELETE /keys/app-web` is a `404`; its retention route
  is a `404` too, and the sweep never erases it. See [Web's store](#web-store) below.
- **Pending erasures.** `GET /api/admin/v1/apps/erasures` lists them, soonest first:
  `[{"id", "name", "kind", "revoked_at", "erase_after"}]`.
- **Undo.** `POST /api/admin/v1/apps/{app_id}/restore` during the grace keeps the App's id, store, folder, scopes,
  origins, retention and its own settings (`/api/v1/config`, kept but unused while it is revoked), and returns the
  key row with a new token in `key`, shown once (`Cache-Control: no-store`); the revoked token stays dead. `404`
  when nothing is pending for that id (never revoked, already restored, or erased). The Apps card's **Undo** button
  does this.
- **After the grace.** The sweep stops the App's running sessions, then erases its store and folder. The registry
  keeps a tombstone: the key row with `erased_at` set and no scopes, origins or retention. The App's settings,
  provider credentials and error counts are deleted with it; usage rows stay as the owner's metadata. Older nightly
  backups keep the App's store until they rotate out.

<a id="web-store"></a>
## Web's store and the data layout

Since #330 stage (c) the owner's and members' sessions live in Agent Harness Web's own store; the main store keeps
no sessions. A one-time step at startup moved them (see [migrations](migrations.md#web-store)).

| Data | Where |
|---|---|
| Owner and member sessions, with their events, approvals, artifacts, checkpoints, App tool calls, smart reviews, review drafts, secret dismissals and search index | `<data_dir>/apps/app-web/harness.sqlite3` |
| Their files: working directories, transcripts, checkpoint snapshots, artifacts | unchanged: `<data_dir>/workspaces/`, `transcripts/`, `checkpoints/`, `artifacts/` for the owner, `<data_dir>/users/<id>/...` for a member |
| An App's sessions and their files | `<data_dir>/apps/<app_id>/` |
| App registry (`api_keys`, Web's `app-web` row included), usage counters (`usage`), per-App error counts (`meta`), provider credentials, App settings, stream tickets | main store, `<data_dir>/harness.sqlite3` |
| Global data: jobs and schedules, templates, images, skills (proposals, installs, versions, reviews, allowlists), the memory library, accounts and Google identities, member projects, GitHub connections, pairing codes, endpoint request log, audit log, canary results, backend usage, browser sign-ins (`web_sessions`), settings and other `meta` | main store |

## Examples

Tailscale/localhost owner (bundled Agent Harness Web, no bearer token):

```http
GET /api/admin/v1/sessions HTTP/1.1
```

Owner token:

```http
GET /api/admin/v1/gpu HTTP/1.1
Authorization: Bearer ho-...
```

Mint an owner token from the PC:

```bash
curl -s http://127.0.0.1:8100/api/admin/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"name":"agent-harness-web","kind":"owner","scopes":["admin"]}'
```

For a separately hosted Agent Harness Web copy, add `"origins":["https://harness-web.example"]`. Browser origins must be HTTPS except for
loopback development and contain no path, query, fragment, or credentials.

## Zero-touch pairing requests

An App pairs without the owner handling its token (#519, owner API 1.22): the App asks, the owner approves, and the
daemon hands the `ha-` token straight to the App. The App side is in [app-api.md](app-api.md#zero-touch-pairing).
These owner routes carry request metadata only: never a token, the App's verifier or its challenge. The standalone Hub
drives them, and every one has a CLI command.

| Route | CLI | What it does |
| --- | --- | --- |
| `GET /pairing-requests` | `harness pairing-requests list` | requests of the last day, newest first |
| `POST /pairing-requests` `{catalog_app_id, scopes, origin?, name?, acknowledge_elevated?}` | `harness pairing-requests arm <catalog_app_id> --scopes ... [--origin ...]` | arm a pre-approved slot for a Hub entry (201) |
| `POST /pairing-requests/{id}/approve` `{match, acknowledge_elevated?}` | `harness pairing-requests approve <id> --match <code>` | approve an App-initiated request |
| `POST /pairing-requests/{id}/confirm` `{match}` | `harness pairing-requests confirm <id> --match <code>` | release a native App's claim on an armed slot |
| `POST /pairing-requests/{id}/deny` | `harness pairing-requests deny <id>` | deny a request, or withdraw a slot or an unredeemed approval |

Each request reads back as `{id, kind, name, scopes, catalog_app_id, origin, browser, armed, state, match_code, needs,
created_at, expires_at, approved_at, finished_at, key_id, elevated, disclosures}`. `state` is `pending` (waiting for
approval), `armed` (a Hub slot no App has claimed yet), `claimed` (a native App claimed the slot; confirm its match
code), `approved` (the App may fetch its token), `redeemed`, `denied` or `expired`. `needs` names the next step:
`approve`, `claim`, `confirm`, `redeem` or `""`. `match_code` is shown only while the owner still has to compare it.
`disclosures` holds, for every requested scope, `{scope, tier, text}`, where `text` is the required copy from
[marketplace-design.md section 5.2](marketplace-design.md#52-scope-table) and `tier` is `standard` or `elevated`. The
daemon supplies this text so the Hub and the CLI show the same words.

Approving needs the match code the App shows; a wrong or missing code is refused with 400. Elevated scopes
(`sessions:all`, `approvals`, `remote_control`, `memory_library`, `homelab`) also need `acknowledge_elevated: true`
(`--acknowledge-elevated`), on approval and when arming. Without it the call is refused with 400. Arming is the
approval for a Hub slot, so a browser App's claim from the armed origin needs nothing more. A native claim needs the
confirm, because the slot's id alone does not prove which App claimed it. For a browser slot the id is the only
thing a caller needs to claim it from the armed origin, so hand it only to that App, as a one-time code. A decision on a request in the wrong state
answers 409, and an unknown id 404. Approval is a state change only: the key exists only after the App redeems, and it
then appears in `GET /keys` with the request's name, scopes, origin and `catalog_app_id`. Requests expire 10 minutes
after they are made, armed or claimed, and 5 minutes after approval.

## Agent Harness for Mac pairing

`POST /api/admin/v1/runner-pairing-codes` with `{"name":"My Mac","runner":"macbook"}` creates a code that
expires after 10 minutes and works once. The owner response shows the code once; list responses contain only its
metadata, and `DELETE /api/admin/v1/runner-pairing-codes/{id}` cancels it. Settings uses this operation to produce the
Agent Harness for Mac install command documented in [`mac-client.md`](mac-client.md).

The Mac redeems the code at `POST /api/v1/runner-pair`. That one response contains a new non-browser owner token and
the selected runner's existing token. It is marked `Cache-Control: no-store`. The code is stored only as a hash, the
runner token stays in its configured owner file and never enters SQLite, and neither token is printed by the CLI.

## Context-efficiency metrics

Owner-only. `GET /api/admin/v1/sessions/{ref}/metrics` (`require_owner`; 404 `no session matches that id` for an
unknown agent session, same as the other session routes) returns per-turn rows plus a session aggregate. The rows
come from persisted `turn_metrics` events, kept as long as the session's events.

```json
{
  "session_id": "s-…",
  "turns": [
    {
      "turn": 1,
      "prompt_tokens": 1200,
      "completion_tokens": 80,
      "composition": {
        "system_state": 200,
        "tool_outputs": 400,
        "file_contents": 500,
        "reasoning_other": 100
      },
      "estimated": false,
      "cache_tokens": 800,
      "recomputed_tokens": 400
    }
  ],
  "aggregate": {
    "dead_end_retries": 2,
    "compaction_correlated_retries": {"elide": 1, "summary": 0, "round_reset": 0},
    "largest_tool_output_chars": 48000,
    "largest_tool_output_by_tool": {"read_file": 48000, "run_shell": 1200}
  }
}
```

Nulls: Claude, Codex and Cursor sessions report null composition, cache, recomputed, and retry fields (native loop
only). Older sessions without `turn_metrics` / `output_chars` also report null rather than a guess. `estimated` is
true when the server omitted `prompt_tokens` and the four buckets are unscaled char estimates; when `prompt_tokens`
is present the buckets are scaled to sum to it. Codex/Claude cache fields are deliberately unused. Delegate calls
are ignored until #157.

`tool_result.output_chars` is Unicode code points of the result before the 20,000-character event truncation.
Failed outputs count. `largest_tool_output_by_tool` is session-API only (never a Prometheus label).

### Prometheus (`GET /metrics`)

Counters, bounded labels, no session id or tool name. Aggregates sum precomputed `turn_metrics` fields (sessions
without those fields contribute nothing). `harness_round_resets_total` is unchanged and is not duplicated here.
Every series covers the owner's and members' sessions only, read from Agent Harness Web's store
(`<data_dir>/apps/app-web/harness.sqlite3`, #330 decision 4): an App's sessions live in its own store, which the owner
surfaces never read (#330); `GET /keys` carries each App's counts instead. The smart-approval stats
(`GET /smart-approvals`) and Control Center's counts follow the same rule.

| Metric | Type | Unit | Labels | PromQL |
| --- | --- | --- | --- | --- |
| `harness_dead_end_retries_total` | counter | retries | none | `sum(harness_dead_end_retries_total)` |
| `harness_compaction_correlated_retries_total` | counter | retries | `tier=elide\|summary\|round_reset` | `sum by (tier) (harness_compaction_correlated_retries_total)` |
| `harness_prompt_cache_tokens_total` | counter | tokens | `kind=cached\|recomputed` | `sum by (kind) (harness_prompt_cache_tokens_total)` |

Cache series stay 0 until a native llama-server session records `cache_tokens` (first prompt-progress chunk of the
generate call; `-1` maps to null; the UI `processed` fallback is not used). Hosted backends never increment them.

Compaction correlation counts a repeat of a pre-compaction failure in the 5 model turns after `elide`, `summary`,
or `round_reset`. `mask` is a size/composition signal only. The generate immediately after compaction is turn 1;
a matching repeat at turn 5 counts and at turn 6 does not.

## Configuration registry

Owner operational settings live on `/api/admin/v1/config` (schema, GET, validate, PATCH, rollback,
restart). The typed allowlist, persistence, recovery, and error codes are documented in
[`config-registry.md`](config-registry.md). App tokens, device tokens, and guests receive 403.

## Changelog

| Version | Date | Changes |
| --- | --- | --- |
| 1.22 | 2026-10-09 | Zero-touch pairing requests (#519, see [Zero-touch pairing requests](#zero-touch-pairing-requests)): `GET` and `POST /pairing-requests`, `POST /pairing-requests/{id}/approve`, `/confirm` and `/deny`. They carry request metadata, match codes and the section 5.2 disclosure copy, never a token or verifier. `pairing_request.*` audit rows record them. CLI `harness pairing-requests list`, `arm`, `approve`, `confirm` and `deny` |
| 1.21 | 2026-10-09 | Optional `catalog_app_id` on `POST /keys` and `POST /pairing-codes` (#518, see [App API](app-api.md#catalog-app-id)): a lowercase reverse-DNS label, at most 120 characters, reported by `GET /keys` and `GET /pairing-codes` (`""` when absent) and in the `key.create`, `pairing.create` and `pairing.redeem` audit metadata. An invalid value is refused with 400 and a `denied` audit row. It grants nothing and is never part of a token |
| 1.20 | 2026-10-04 | Nightly backups include known members' and Apps' transcript archives; backup results add `transcript_archives` and `warnings` (#378) |
| 1.19 | 2026-10-04 | Management parity (#334): checkpoints (list, rewind, fork), secret-finding fix and dismiss, taint clear, and GitHub tasks (items, item, `POST /github/sessions`) answer under `/api/admin/v1`, where Agent Harness Web already called them. Every owner setting and action Web offers has an Agent Harness CLI command ([management-parity.md](management-parity.md)) |
| 1.18 | 2026-10-03 | Agent Harness Web's store (#330 decision 4): the owner's and members' sessions move into `<data_dir>/apps/app-web/harness.sqlite3` at startup (`HARNESS_WEB_STORE_MIGRATION=dry-run` only logs what would move). `/metrics`, smart-approval stats and Control Center counts read it. Web is registered as `app-web` (kind `web`, no key): not in `GET /keys`, `404` from `DELETE /keys/app-web` and its retention route. Backups add `apps/app-web.sqlite3` |
| 1.17 | 2026-10-03 | App retention and erasure (#330 decision 5): `PUT /apps/{app_id}/retention`, `GET /apps/erasures`, `POST /apps/{app_id}/restore`. `GET /keys` adds `retention_days`, `erase_after` and `erased_at`; revoking an App or device key schedules its erasure 7 days later; the cleanup report adds `sessions_expired` and `apps_erased` |
| 1.16 | 2026-10-03 | Owner surfaces never reach an App's sessions (#330 decision 3): `/sessions` (404 by id or prefix), `/search`, `/queue`, `/events`, approvals and transcripts leave them out. `GET /keys` adds a `store` object per App and device key: `sessions` (counts by status), `usage` (`requests`, `prompt_tokens`, `completion_tokens`, `cost_usd`), `errors`, `last_error` (the stop reason's kind only), `last_error_at`. `/metrics`, smart-approval stats and Control Center counts cover the owner's and members' sessions only. Backups add `apps/<app_id>.sqlite3` per App and `app_stores` (a count) to the backup result |
| 1.15 | 2026-10-01 | Secret scan of a session's added lines on `changes`; Review `merge`/`push` on tower sessions return 409 `secret_findings` (or 503 `secret_scan_unavailable`) until findings are fixed or dismissed; fix and dismiss endpoints |
| 1.14 | 2026-09-28 | Owner session context-efficiency metrics and Prometheus retry/cache counters |
| 1.12 | 2026-09-19 | Owner masked inpainting: upload, edit, cancel, and delete |
| 1.11 | 2026-09-19 | First-party client protocol ranges, version-skew enforcement, and update discovery metadata |
| 1.10 | 2026-09-19 | Smart-approval effective mode: last writer among PUT and Settings; `off` is truly off |
| 1.9 | 2026-09-18 | Owner smart-approvals status and live mode (`off`/`shadow`/`auto`) |
| 1.8 | 2026-09-18 | Owner-approved instruction skills: proposals, hash-bound install, enable/allowlist/rollback. `POST /sessions` `skills`: omit the field to inject the project's allowlisted enabled skills; send an explicit list (including `[]`) as the include set so an unchecked box is excluded |
| 1.7 | 2026-09-18 | Typed configuration registry, managed overlay, supervised restart/rollback |
| 1.6 | 2026-09-17 | Image archive health and explicit retention preview/apply operations |
| 1.5 | 2026-09-17 | Owner-provisioned household members: accounts, audit, aggregate metadata, no member content |
| 1.4 | 2026-09-16 | One-time native Mac client and runner pairing |
| 1.3 | 2026-09-16 | Owner-managed per-app provider policy, opaque key-file references, and revocation |
| 1.2 | 2026-09-16 | Daemon profile and optional-module capability discovery |
| 1.1 | 2026-09-16 | Origin-bound Control Center owner tokens and cross-origin browser access |
| 1.0 | 2026-09-16 | First release: versioned owner operations, `admin` scope, owner tokens (`ho-`) |
