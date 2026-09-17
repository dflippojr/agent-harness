# Smart approvals (issue #18)

A small owner-configured hosted API model can rate tagged, local, reversible shell asks after the
deterministic `Policy` has already returned `ASK`. The sandbox and that policy remain the security
boundaries. The reviewer never auto-denies, never sees the task prompt or transcript, and never
runs with tools, browsing, a workspace mount, or conversation history.

## Setup

Off by default. In `config/harness.local.yaml`:

```yaml
provider_secret_files:
  smart-reviewer: D:/Agents/harness/secrets/smart-reviewer.key

smart_approvals:
  enabled: true
  provider: openai          # openai | anthropic
  model: gpt-4.1-mini
  secret_ref: smart-reviewer
  timeout_seconds: 8
  min_confidence: 0.85
  mode: shadow
  proxy: http://127.0.0.1:8890
```

After setup the live mode is `shadow`: recommendations are recorded and shown on the ordinary
approval card, but the owner still decides. Switch to `auto` from Settings → Smart approvals, or
`PUT /smart-approvals` / `PUT /api/admin/v1/smart-approvals` with `{ "mode": "auto" }`. Apps, guests,
and app tokens cannot enable it, pick its credential, or widen eligibility. Turning it `off` or
back to `shadow` applies to the next tool call without restarting sessions.

The optional proxy is the `reviewer` service in `ops/egress/compose.yaml` (host `127.0.0.1:8890`),
allowlisting official OpenAI and Anthropic API hosts only.

## Authority

1. Deterministic policy runs first. `ALLOW` executes; `DENY` stays denied. Neither is sent to the
   reviewer.
2. Only built-in rules tagged `smart_eligible` may be reviewed. V1 tags the Claude Code `Bash`
   catch-all. Project rules cannot opt in.
3. Static eligibility must then prove a parseable, local, reversible workspace command (tests,
   linters, type checks, read-only inspection, local builds). Human-only classes never call the
   model: `ALWAYS_ASK`, networked/clone/install/auth, secrets, deletion outside scratch, git
   push/reset/force/merge/release, privilege, Docker/mounts, substitutions, globs, chaining,
   unknown tools, and writes outside `/workspace`.
4. Auto-approve only in `auto` mode when the strict JSON schema, `approve`, and the confidence
   threshold all pass. `deny`, `escalate`, low confidence, risk flags, timeout, malformed JSON,
   missing credential, or provider errors produce one durable human approval.

## Shadow evaluation (synthetic)

`tests/test_smart_approvals.py::test_shadow_eval_zero_unsafe_auto_approval_candidates` runs more
than 100 routine and human-only cases through the static gate. Unsafe classes must not be
eligibility hits (zero unsafe auto-approval candidates). Confusion: false negatives (safe commands
that still ask a human) are acceptable; false positives are not.

## Live exit checklist

Owner-run after explicitly switching to `auto`. Do not put real credentials, paths, logins, tokens,
or tailnet names in public comments.

- [ ] Provider / model / threshold recorded (Settings → Smart approvals)
- [ ] Routine `pytest` / linter / local build from a Claude Code session auto-approves with the
      transcript badge "deterministic gate and smart reviewer both allowed this call"
- [ ] `git push`, a networked command, and a destructive delete still reach the phone
- [ ] Latency and estimated/API-reported usage visible on the settings card
- [ ] Switching to `off` mid-session returns the next eligible call to a human card without
      restarting the daemon
