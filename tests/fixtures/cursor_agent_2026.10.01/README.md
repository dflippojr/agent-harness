# Cursor Agent 2026.10.01 stream-json transcripts

What `agent -p --output-format stream-json --stream-partial-output --force` writes to stdout for one turn. The CLI
image installs Cursor with its install script, which pins nothing; 2026.10.01-e373342 is the version
`docs/phase8a-design.md` records checking. The record types and fields follow Cursor's documented stream-json output
and the records the harness already handles; ids, text, token counts and timestamps are made up. They are assembled,
not captured byte for byte: capture them again from the CLI when its format changes (#991).

Cursor's print mode asks the host nothing (no permission requests) and reports no rate limits, so neither appears.

- `turn.jsonl`: init; the user's message; a thinking delta; streamed assistant deltas; a shell call and its result; a
  read that fails; the untimestamped aggregate assistant text (the answer); the result with usage.
- `limit.jsonl`: a result that failed on the usage limit.
