# First-party client compatibility

Agent Harness versions its first-party wire contracts separately from Git commits and package versions. The Server
supports the current and immediately previous protocol for Agent Harness Web/app calls, the owner/admin API, and the
Mac Runner. Additive response fields are compatible; clients must ignore fields they do not understand.

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
