# Codex 0.154.0 app-server transcripts

What `codex app-server --stdio` writes to stdout during a turn, at the version `sandbox/cli.Dockerfile` pins: JSON-RPC
v2 notifications, the server's approval requests, and a response to the harness's own `turn/start`. The methods and
fields follow the notes in `docs/phase8a-design.md` ("Codex CLI", "Harness tools over MCP") and the app-server shapes
the harness already handles; ids, text, token counts and timestamps are made up. They are assembled, not captured
byte for byte: capture them again from the pinned CLI when the pin moves (#991).

- `turn.jsonl`: thread and turn start; a reasoning summary delta; a command Codex asks to run (approved and run); a
  file change it asks to make (declined, so the tool fails); a harness MCP call it asks about with an elicitation
  (it fails); a rate-limit update; the agent's message; token usage; the completed turn.
- `limit.jsonl`: a rate-limit update with a reached limit, then a turn that failed on the usage limit.
