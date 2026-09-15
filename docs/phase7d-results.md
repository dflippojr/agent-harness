# Phase 7d: scheduled jobs

Built 2026-09-15. Decision (user): **quiet unless attention.** The agent ends with an OK or ATTENTION status; OK
sends a low-priority notification (or none, per job), ATTENTION notifies normally, and approvals always notify.

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| Scheduler | `harness/jobs.py` `JobScheduler`, `jobs:` in `config/harness.yaml` | checks every 30 s; each run is an ordinary session (`sessions.job_id`), so it queues for the GPU, pauses for games, and asks for approvals |
| Cron | `harness/jobs.py` `Cron` | five fields in tower local time: lists, ranges, steps, `mon-fri`/`jan` names, 0 or 7 = Sunday, `@hourly/@daily/@weekly/@monthly`; either day field matches when both are set (standard cron); no new dependency |
| Storage | `jobs` table; `sessions.job_id`, `sessions.job_status` | `last_session_id`, `last_skip`, `last_error` shown in the app |
| Status | `jobs.STATUS_PROMPT`, `parse_status`, `summary` | appended to the job's prompt; the last `STATUS:` line of the answer decides; `summary` picks the verdict paragraph for the notification |
| Notifications | `notify.Notifier._job_finished` | OK → none (`attention`), priority 2 (`low`), or 3 (`always`); ATTENTION → priority 4 with the reason; no status line → 3; failed → 4; cancelled → none |
| API | `GET/POST /jobs`, `GET/PUT/DELETE /jobs/{id}`, `POST /jobs/{id}/run`, `GET /jobs/preview?cron=` | run-now doesn't move the schedule; 409 while the previous run is active |
| App | `⏰ Jobs` next to search | list (schedule, next run, last result), form with presets, live next-three-runs preview, notify choice, enable switch, Run now, recent runs; job sessions show an OK / ⚠ attention badge |
| Tests | `tests/test_phase7.py` | cron table (incl. leap day, weekday ranges, either-day rule), invalid schedules, status parsing and summary, due/overlap/skip, catch-up after downtime, notification priorities, API |

## Design

- **No pile-ups.** If a job's previous session is still active when its slot comes, that slot is skipped (`last_skip`).
- **Downtime.** At start, a slot missed by less than `catch_up_minutes` (default 6 h) runs once; older misses are
  skipped and recorded.
- **Bias toward reporting.** An OK is barely shown, so a wrong OK hides a real problem. The first live ATTENTION test
  came back OK: Qwen saw `harness-demo` "Exited (0)" and called it normal one-shot behaviour, the same misreading as in
  Phase 3. The status instruction now says OK only if everything the task expects is true, and ATTENTION otherwise,
  even when a harmless explanation is possible (state it; the user decides). The retry reported ATTENTION correctly.
- A job's title is `⏰ <name> · <start time>`, so its sessions are easy to spot and search.

## Verification

- 117 tests pass (1 skipped) before 7e.
- **Scheduled trigger** (temporary job `[test] Scheduled trigger`, cron set to 07:30, project `homelab`, notify `low`):
  session `e88eec86dd` started at 07:30:27 (within one poll), checked all six services in 3 turns (7 tool calls,
  66.7K prompt tokens), ended `STATUS: OK`, and built the priority-2 "OK" notification. Its first notification body
  was a flattened table, which led to `summary()`: now "All 6 allowlisted services are running with 0 restarts. …".
- **ATTENTION** (same job, Run now, `docker stop harness-demo` first):
  - First try (session `324b2d2308`): wrongly OK (see above).
  - After the prompt change (session `ba3c08082d`): `STATUS: ATTENTION: harness-demo exited cleanly (SIGTERM, exit 0)
    — may be intentional but container is not running`, notification priority 4 "Needs attention". 3 turns, 33K
    prompt tokens.
  - `harness-demo` was started again before the real 08:00 job, and the test job was deleted (its sessions remain).
- **Real job created:** "Morning homelab check", daily 08:00, project `homelab`, notify `low` (pause or delete it
  under ⏰ Jobs).
- Phone-size screenshots of the jobs list and job form checked.
