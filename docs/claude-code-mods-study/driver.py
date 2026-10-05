"""Drive one worker-shaped `claude -p` stream-json session against the stubs (#306).

The docker flags follow `ClaudeSession.command()` in harness/cli_backends.py; the differences are the study's own:
a throwaway state volume, a tmpfs where the login volume would be, the stub API instead of the egress proxy, and the
study mod mounted read-only. The driver plays the harness: it sends `initialize` and the prompt, answers every
`can_use_tool` request with allow (logging it, so the run shows which calls reached the harness), and prints the
events that matter as JSON lines.

    python driver.py --scenario events [--plugin flag|env|none] [--resume <id>] [--extra "<docker args>"]
"""

import argparse
import json
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE = "agent-harness-cli-modstudy:2.1.289"
RELAY = "modstudy-relay"
STATE = "modstudy-state"
MOD_TARGET = "/opt/harness-mods/harness-policy"


def command(args) -> list[str]:
    work = Path(args.workspace).resolve()
    cli_home = Path(args.cli_home).resolve()
    cmd = [
        "docker", "run", "--rm", "-i", "--name", "modstudy-claude",
        "--network", f"container:{RELAY}",
        # Production points these at the egress proxy; the study network has no route out at all.
        "-e", "HTTPS_PROXY=http://egress.invalid:3128", "-e", "HTTP_PROXY=http://egress.invalid:3128",
        "-e", "NO_PROXY=localhost,127.0.0.1,modstudy-api", "-e", "NODE_USE_ENV_PROXY=1",
        "-e", "CLAUDE_CONFIG_DIR=/home/agent/.claude",
        "-e", "CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude-login",
        "-v", f"{STATE}:/home/agent/.claude",
        "--mount", "type=tmpfs,target=/home/agent/.claude-login",  # never a login volume
        "--mount", f"type=bind,source={cli_home / 'settings.json'},target=/home/agent/.claude/settings.json,readonly",
        "--mount", f"type=bind,source={cli_home / 'CLAUDE.md'},target=/home/agent/.claude/CLAUDE.md,readonly",
        *[a for d in ("agents", "commands", "skills", "plugins", "hooks", "output-styles", "rules")
          for a in ("--mount", f"type=tmpfs,target=/home/agent/.claude/{d},tmpfs-mode=0555")],
        "--mount", f"type=bind,source={work},target=/workspace",
        "-w", "/workspace",
        "--memory", "2g", "--cpus", "2", "--pids-limit", "512",
        "--security-opt", "no-new-privileges",
        "--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE",
        "-e", "ANTHROPIC_BASE_URL=http://modstudy-api:8080", "-e", "ANTHROPIC_API_KEY=sk-ant-study-stub",
    ]
    if args.plugin != "none":
        cmd += ["--mount", f"type=bind,source={HERE / 'harness-policy'},target={MOD_TARGET},readonly"]
    if args.plugin == "env":
        cmd += ["-e", f"CLAUDE_CODE_PLUGIN_DIRS={MOD_TARGET}"]
    if args.probe:
        cmd += ["-e", "STUDY_PROBE=1"]
    cmd += shlex.split(args.extra)
    cmd += [args.image, "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio",
            "--permission-mode", "default", "--model", "claude-sonnet-5-5",
            "--append-system-prompt", "Study session for #306."]
    if args.plugin == "flag":
        cmd += ["--plugin-dir", MOD_TARGET]
    if args.resume:
        cmd += ["--resume", args.resume]
    cmd += ["--mcp-config", '{"mcpServers": {}}', "--strict-mcp-config", *shlex.split(args.cli_extra)]
    if args.debug:
        cmd += ["--debug-file", "/workspace/debug.log"]
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="events")
    parser.add_argument("--plugin", choices=["flag", "env", "none"], default="flag")
    parser.add_argument("--resume", default="")
    parser.add_argument("--extra", default="")
    parser.add_argument("--cli-extra", default="")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cli-home", default=str(HERE.parents[1] / "harness" / "cli_home" / "claude"))
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    proc = subprocess.Popen(command(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    stderr: list[str] = []
    threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()

    def send(item: dict) -> None:
        proc.stdin.write(json.dumps(item) + "\n")
        proc.stdin.flush()

    def show(kind: str, **data) -> None:
        print(json.dumps({"driver": kind, **data}), flush=True)

    send({"type": "control_request", "request_id": "init-1", "request": {"subtype": "initialize"}})
    send({"type": "user", "message": {"role": "user", "content": f"SCENARIO:{args.scenario} go"},
          "parent_tool_use_id": None, "session_id": ""})
    # The read below blocks, so a CLI that stalls without writing is ended from a timer, not by checking a deadline.
    timed_out = threading.Event()

    def watchdog() -> None:
        timed_out.set()
        subprocess.run(["docker", "rm", "-f", "modstudy-claude"], capture_output=True)
        proc.kill()

    timer = threading.Timer(args.timeout, watchdog)
    timer.daemon = True
    timer.start()
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            show("non-json", line=line[:200])
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            show("init", session_id=event.get("session_id"), claude_code_version=event.get("claude_code_version"),
                 plugins=event.get("plugins"), tools=len(event.get("tools") or []))
        elif kind == "system":
            show("system", subtype=event.get("subtype"), data={k: v for k, v in event.items()
                                                                 if k not in ("type", "uuid", "session_id")})
        elif kind == "control_request":
            request = event.get("request") or {}
            show("can_use_tool", tool=request.get("tool_name"), input=request.get("input"))
            send({"type": "control_response", "response": {"subtype": "success", "request_id": event["request_id"],
                  "response": {"behavior": "allow", "updatedInput": request.get("input") or {}}}})
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)
                    show("tool_result", is_error=block.get("is_error", False), text=text[:300])
        elif kind == "result":
            show("result", subtype=event.get("subtype"), result=str(event.get("result"))[:200],
                 session_id=event.get("session_id"))
            break
    timer.cancel()
    if timed_out.is_set():
        show("timeout", seconds=args.timeout)
    try:
        proc.stdin.close()
    except OSError:  # the watchdog already killed it
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(0.5)
    if stderr:
        show("stderr", lines=[s.rstrip() for s in stderr][-15:])
    show("exit", code=proc.returncode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
