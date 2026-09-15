# Phase 7b and 7c: memory library writes and the agent profile

Built 2026-09-15. Decisions (user): agents propose edits only in the six readable categories and **every write asks**,
with the diff on the phone; sensitive categories stay invisible and unwritable. The frozen snapshot is a **curated
agent profile**, about 1–2K tokens, that agents update through the same approved writes, taking effect from the next
session.

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| Write tools | `memory_edit(path, old_text, new_text, summary)`, `memory_write(path, content, summary)` in `harness/memory_library.py` | readable categories plus the profile; `memory_library.writes` in `config/harness.yaml` |
| Gate | `policy.ALWAYS_ASK` + `MemoryLibrary.apply` | the policy always asks (a project rule can deny, never allow); the library also refuses to write without an approved approval whose diff still matches |
| Approval card | `harness/web/app.js` `approvalCard` | summary line plus a coloured diff; the notification shows the summary and path |
| Save | daemon clone `D:\Agents\memory-library` | fetch + hard reset to `origin`, apply, re-check the diff, commit (message = summary + session id), push; one `pull --rebase` retry; anything unpushed is dropped and the agent is told |
| Profile | `agent-profile.md` at the library root (`memory_library.profile_path`, 6,000-character cap) | readable and writable as an explicit exception to the category allowlist; added to each new session's system prompt, not to app-API sessions |
| Settings | Memory library card, `GET /memory` | readable categories, last saved change, profile size and contents |
| Tests | `tests/test_phase7.py` | policy, approve → commit → push against a bare repo, refusals that never reach the user, denied/unapproved, stale diff, sensitive warning, profile frozen per session and not given to apps, API |

## Design

- **Refusals don't reach the phone.** Before an approval is created, the library works out the change: a path outside
  the allowed files, `old_text` that isn't unique, no actual change, or a profile over its cap goes straight back to
  the agent as an error.
- **The approved diff is the one applied.** The approval stores the summary and diff. On approval the clone is reset to
  the remote's state and the change is computed again; if the diff differs (someone pushed to that file in the
  meantime), nothing is written and the agent is asked to propose it again.
- **Sensitive-detail hint.** Added lines are scanned for health, finance, relationship, identity and credential
  terms. A hit adds "⚠ added lines may mention … details" to the approval reason. It's a hint for the reviewer, not a
  filter: the Phase 0 memory suite showed models copy such details despite instructions, and the approval is the gate.
- **Non-interactive git.** `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`, so a push that would need a login fails
  instead of hanging the daemon.
- **Profile frozen per session.** It's read when the session is created and becomes part of the system message, which
  compaction never rewrites. Sessions of the same project therefore share an identical prompt prefix, and an edit
  (by an agent or on another machine) appears in the next session. The clone is refreshed in the background at start
  and whenever a session is created after `refresh_minutes`.
- `run_cmd` gained an `env` argument for the git environment.

## Verification

- 97 tests pass (1 skipped) before the 7d work started.
- **7b exit test** (session `740cf9899a`, Qwen): "Please remember this in my agent profile: on the tower, all GitHub
  work (git pushes, gh CLI, new repos) uses my personal account dflippojr, never my work account."
  - `memory_read` of the profile, then `memory_edit` with a one-line addition to the Home server section and a
    summary. The approval notification went out; the card rendered the summary and diff (phone-size screenshot).
  - Approved (by the building agent, via the API, as the exit test): applied in 3.05 s, commit `426f7da` pushed, and
    GitHub's `refs/heads/master` showed that commit.
- **7c exit test** (session `6887fb793c`, a new session): "Answer from what you already know about me, without using
  any tools: which GitHub account should agents use for work on the tower, and where did you learn that?"
  - Answered `dflippojr`, citing the agent profile in its system prompt. One turn, no tool calls, 3,617 prompt tokens
    (the profile is about 600 of them).
- Found during the run and fixed: approval diff lines didn't wrap (CSS precedence), and search indexed earlier
  `session_search`/`session_read` output, so results echoed other sessions with literal `[ ]` marks (index version 2
  skips them).
