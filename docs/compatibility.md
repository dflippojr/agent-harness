# First-party client compatibility

Agent Harness versions its first-party wire contracts separately from Git commits and package versions. The Server
supports the current and immediately previous protocol for Agent Harness Web/app calls, the owner/admin API, and the
Mac Runner. Additive response fields are compatible; clients must ignore fields they do not understand.

Mac client 4.3 speaks runner protocol 3; the Server accepts runner protocols 2–3. Protocol 2 can still execute
sessions and discard workspaces, but Merge and Push require protocol 3's `scan_input` and `expect_head` guard.
The scan runs on the tower, so no scanner binary is installed by the Mac client.

`GET /health`, `GET /api/v1`, and `GET /api/admin/v1` publish the Server release/build, supported protocol ranges,
minimum client releases, effective capabilities, and machine-readable update actions. First-party HTTP clients send
`X-Agent-Harness-Client: web/<protocol>` or `cli/<protocol>`; the Runner sends `runner/<protocol>` and repeats its
release/protocol in every poll. A missing header is accepted for one transition release with an HTTP deprecation
notice. Too-old clients receive `426 client_update_required`; clients newer than the Server receive
`426 daemon_update_required`. Health and update discovery remain reachable during skew.

Agent Harness Web checks compatibility on startup and foreground restore. A compatible new shell is offered as a
reload; unsaved form input blocks the reload. An unsupported shell shows a blocking **Reload and update** action,
purges only the static shell cache, asks the service worker to update, and uses a session guard to prevent reload
loops. API responses, event streams, images, and transcripts are never cached.

Example of an additive field: `catalog_app_id` (App API 1.22, owner API 1.21, #518) appears on keys, pairing codes and
the pairing response. Older clients ignore it, an older Server ignores it in a create request (the key or code is
made without it), and the Python SDK exposes it as `Harness.pair(...).paired_app["catalog_app_id"]`.

Zero-touch pairing requests (App API 1.23, owner API 1.22, #519) are additive too. These are the new routes under
`/api/v1/pair/requests` and `/api/admin/v1/pairing-requests`, and `features.pairing_requests` in `GET /api/v1`. An App
checks that feature before it asks. Against an older Server it falls back to an owner-made pairing code, whose routes
are unchanged. The new routes return the same `{token, app, api_version}` shape as `POST /api/v1/pair`.

The exclusive Hub claim (App API 1.24, owner API 1.23, #543) is additive as well: an optional `kind` field on
`POST /api/v1/pair/requests`, the `/api/admin/v1/hub-claim` routes, `role` on `GET /keys` rows, `hub.claimed` in
`GET /api/admin/v1`, and `features.hub_claim` and `features.hub_claimed` in `GET /api/v1`. A Hub checks
`features.hub_claim` before it asks; an older Server has no such feature, and the SDK's `request_hub_claim` refuses
with `feature_unsupported` there instead of sending a request the Server would treat as an App's.

## Agent Harness for Mac

```sh
harness version
harness update
```

`harness update` downloads the version-matched package only from the configured Server, verifies the Server-published
size and SHA-256, safely stages it, atomically replaces the CLI/Runner runtime, preserves both configuration files and
credentials, and restarts the launchd agent. A failure rolls the old runtime back and records the result in
`~/.agent-harness/runner/last-update.json`.

The owner can also choose **Update Mac client** beside a compatible online Runner in Agent Harness Web. This queues a
dedicated update operation—not a command or URL. A Runner too old to understand that operation is given the exact
manual `harness update`/installer fallback instead.

Recovery: run `harness update` again. If the command itself is missing, reopen Settings → Apps → Pair Mac client and
run the displayed installer command; pairing credentials and user configuration are stored outside the replaced
runtime and are not overwritten.
