# Phase 1: harness daemon core

Built and tested 2026-09-14 against the always-on Qwen3.6-35B-A3B server (64K context).

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| Daemon (FastAPI, one process) | `harness/api.py`, `python -m harness` | `127.0.0.1:8100`, ~50 MB RAM |
| Sessions, events, approvals | `harness/db.py` | SQLite (WAL) at `D:/Agents/harness/harness.sqlite3` |
| Agent loop | `harness/runner.py` | re-entrant: each step commits before the next, so a restart resumes |
| Tools v1 | `harness/tools.py` | list/read/search/write/edit files, run_shell, git_clone, update_notes, finish |
| Sandbox | `harness/sandbox.py` | one container per session, workspace-only mount, 2 GB / 2 CPU / 512 pids |
| Approval policy | `harness/policy.py`, `config/projects.yaml` | project rules first, then defaults |
| GPU queue | `harness/scheduler.py` | one session at a time |
| Compaction | `harness/compaction.py` | elide old tool output, then summarize |
| CLI | `harness/cli.py` | new / watch / send / approve / deny / cancel / list / transcript / queue |
| Transcripts | `harness/transcript.py` | Markdown, written to `D:/Agents/harness/transcripts/<id>.md` at the end of each run |
| Tests | `tests/test_daemon.py` | 23 tests, scripted model; one needs Docker, none need the GPU |

## Design decisions made while building

- **The GPU slot is held per session run, not per model call.** llama-server runs one slot, so interleaving
  sessions would evict the prompt cache (a cold 58K prompt takes ~69 s on Qwen). A session gives the slot up
  while waiting for an approval.
- **Network access is per command.** Sandboxes sit on an internal Docker network (`harness-sandbox`: no
  gateway, no container-to-container traffic). `run_shell` with `network: true` asks for approval, then
  attaches `harness-egress` only for that command. While attached, the host and LAN are reachable too.
- **`git_clone`** accepts `https://` URLs (cloned inside the sandbox with network; github/gitlab/codeberg are
  allowed without asking) or `local:<name>` for repos under `D:/Agents/repos` (cloned host-side, no network).
  Private GitHub repos need credentials, which sandboxes don't have: that's Phase 3 (project registry).
- **Restart semantics.** A tool call that was executing when the daemon died is reported to the model as
  "effects unknown, check before retrying", and the container is restarted to kill the orphaned command. A
  pending approval stays pending across restarts. A model call in flight is simply redone.
- **Messages sent during a run** go to an inbox and are delivered before the agent's next model call.
  Messages sent after a run start a new run in the same session and context.
- **Budgets per run:** 80 turns, 200K completion tokens. Hitting one ends the run as `done` with
  `stop_reason` `budget_turns`/`budget_tokens`; sending a message continues.
- **Default policy:** `git push`, `git reset --hard`, `git clean -f`, network commands, clones from other
  hosts, and deletes outside scratch paths (`/tmp`, `scratch/`, caches, `build/`, `dist/`) ask first.
  Everything else runs. The shell classification is a heuristic that errs toward asking; the sandbox is the
  security boundary. Project rules can match tool, argument regexes, and path globs, e.g. to gate writes
  to sensitive memory-library categories (the Phase 0 inbox-promotion failure) with the diff shown.
- **Streaming.** Token deltas go to clients as ephemeral SSE events (never persisted). Everything else is
  a persisted event with a `seq`, so clients resume with `?after=<seq>`.

## Exit test (passed)

"Clone repo X, fix the failing test, report back", from the tower CLI:

1. `invoice-tools` (in `D:/Agents/repos`): an off-by-one in volume-discount tiers, 3 of 4 tests failing.
2. The agent cloned it, read the code, and ran pytest. Right after it saw the failures, the daemon was
   killed with `taskkill /F`. State at kill: `running`, 4 turns, model call in flight.
3. Restarted the daemon: the session resumed on its own (`resumed` event), applied `>` → `>=`, re-ran
   pytest (4 passed), and reported what was wrong and how it verified the fix. Independently re-checked
   in a fresh container: 4 passed; the diff is the one-character fix.
4. Follow-up message "commit the fix and push it": the commit ran; `git push` raised an approval; denied
   with a note; the agent reported the local commit and didn't retry.
5. The transcript (`D:/Agents/harness/transcripts/2f21711d48.md`) shows every step, the restart, and the
   approval.

Whole task: 7 model turns, ~72 tok/s decode, under a minute of wall time excluding the restart.

## Compaction stress test (16K context, artificial)

A second daemon was run with `context_tokens: 16384` (same 64K server) on a deliberately bad task: read four
700-line logs page by page with `read_file` only and count ERROR lines per file. The logs total ~88K tokens,
far more than fits.

| Attempt | Change | Result |
| --- | --- | --- |
| 1 | first version | Looped: after each summary the agent re-cloned and re-read from line 1. Cancelled at 34 turns. Causes: the summarizer never saw the original task ("User requests: none yet"), and Qwen keeps its running counts in reasoning, which the excerpt left out. |
| 2 | task shown to the summarizer, reasoning included in the excerpt, `update_notes` tool (pinned verbatim through summaries), tool results of one turn share a 35%-of-context budget, token-estimate calibration fixed | Finished in 24 turns and named the right top code, but the counts were wrong (9/7/3/3). Cause: compaction ran right after a turn's results arrived, so the summarizer, not the agent, "read" the new pages. |
| 3 | compaction never touches the newest turn's (unseen) results | **31 turns, 14/13/12/10 vs. true 17/13/12/10; top code E101 × 22 correct.** It summarized almost every turn (steady state at 16K with ~4K-token pages), which is slow but correct behavior. |

Qwen never called `update_notes` on its own in these runs; the summaries carried the state. Keep the tool
(it's cheap), and consider nudging it when a session compacts for the first time.

`usage.prompt_tokens` from llama-server counts the whole prompt, cached tokens included
(`prompt_tokens_details.cached_tokens` breaks them out). `chat_template_kwargs: {enable_thinking: false}`
works for Qwen summaries (2 completion tokens instead of hundreds of thinking tokens).

## Known gaps / next

- No autostart for the daemon yet (Phase 5 lists it). Start it by hand: `.venv\Scripts\python -m harness`.
- No auth: localhost only. Phase 2 puts it behind `tailscale serve`.
- Workspaces and stopped containers are never cleaned up (Phase 3: cleanup and quotas).
- Parallel tool calls run sequentially.
- A single turn's results are capped, but a single huge `write_file` argument from the model isn't.
- The macbook target returns 501 until Phase 4.
- Memory: at the end of testing, free RAM was 5.3 GB with Qwen loaded and one sandbox running. Sandboxes are
  capped at 2 GB and stopped between runs.
