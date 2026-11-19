# Claude Code 2.1.272 stream-json transcripts

What `claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages
--permission-prompt-tool stdio` writes to stdout, at the version `sandbox/cli.Dockerfile` pins. The record types and
fields follow the verified protocol in `docs/phase8a-design.md` ("Claude Code"), including the `can_use_tool` request
recorded there; ids, text, token counts and timestamps are made up. They are assembled, not captured byte for byte:
capture them again from the pinned CLI when the pin moves (#991).

- `turn.jsonl`: init; partial-message deltas, one of them a `thinking_delta` (which carries `thinking`, not `text`, so
  it streams nothing); text; a Bash call the CLI asks permission for, and its result; a Read call that fails; a
  rate-limit record; the result.
- `limit.jsonl`: a rejected rate-limit record, then a result that failed on the API's rate limit.
