# Hosted CLIs: split mode (#427)

A hosted Claude Code or Codex session normally runs the vendor CLI, its provider token, its state volume and its
built-in shell and file tools in one container. Any command the agent runs can read the token. Split mode separates them,
the way the native loop separates its brain (runner) from its hands (`harness/sandbox.py`).

```yaml
backends:
  claude:
    tool_mode: split      # builtin (default) | split
```

`tool_mode` is per backend, for `claude` and `codex` with `mcp: true`; anything else fails at config load. The default is
`builtin` until the comparison below is done.

## What changes in `split`

- **Brain container** (the CLI): keeps the token and the state volume. It has **no `/workspace` bind mount**
  (`workspace_args` in `harness/cli_backends.py`) and no shell or file tool of its own.
  - Claude Code runs with `--tools "Task,TodoWrite"`: every other built-in (Bash, Read, Edit, Write, Glob, Grep,
    WebFetch...) is off. Task and TodoWrite touch no environment. Checked against `claude` 2.1.295's `--tools`
    allowlist; recheck when the pin moves.
  - Codex starts with `environments: []` (no shell, `apply_patch`, `view_image`) and the `CODEX_SPLIT_OVERRIDES`
    feature flags off. Its plan tool stays on. Lost: `view_image`, `apply_patch` (`edit_file` replaces it), Codex's
    multi-agent and its hosted `web_search` (the harness `web_search` is still served).
- **Hands**: the harness MCP server serves `run_shell`, `read_file`, `write_file`, `edit_file`, `search` and
  `list_files` (`SPLIT_TOOLS`) to the CLI. They are the native loop's tools of the same names, so `Policy` rules,
  network grants (`run_shell` with `network`), approvals and the end-of-run checkpoint apply as they do for the native
  loop. (The issue's `run_command`, `grep` and `glob` are these tools' native names `run_shell`, `search` and
  `list_files`; policy rules key on the native names.)
  - Shell commands run through `Sandbox.exec` in the per-session sandbox container, which has no provider
    environment. File tools run on the session workspace on the host, as in the native loop.
  - Every call needs a one-use grant that the approval path records when it allows the call; an ungranted call is
    refused and nothing runs.
- A split session never falls back to `builtin`: with MCP unavailable the run fails with `split_unavailable`. A Codex
  item other than the plan, reasoning, messages or a harness MCP call stops the run. App-tools-only sessions are
  unchanged and take precedence.

If the hands container dies mid-command, `Sandbox.exec` recreates it on the next call; the CLI container, and with it the
session's history, is a separate process and keeps running.

## Quality comparison

```
python -m bakeoff.hosted --backends claude,codex --modes builtin,split --suite hard --repeats 3
```

Drives the same bakeoff tasks through a throwaway Manager, one session per run, and reports pass rate, turns, tokens and
wall time per backend, mode and task. It uses the real hosted login, so the owner runs it. The decision stays as the
issue wrote it: if the pass-rate drop is small, `split` becomes the default and `builtin` an opt-in for trusted repos;
if it is large, record which tools account for it (likely `edit_file` against `apply_patch`) first.
