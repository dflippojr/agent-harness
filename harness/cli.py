"""Small CLI client for testing the daemon: python -m harness.cli --help"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("HARNESS_URL", "http://127.0.0.1:8100")
TERMINAL = ("done", "cancelled", "failed")
DIM, BOLD, YELLOW, GREEN, RED, CYAN, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[32m", "\033[31m", "\033[36m", "\033[0m"


def api(method: str, path: str, retries: int = 30, **kwargs) -> dict | list | str:
    for attempt in range(retries + 1):
        try:
            resp = httpx.request(method, BASE + path, timeout=60, **kwargs)
            break
        except httpx.TransportError:
            if attempt == retries:
                sys.exit(f"daemon not reachable at {BASE}")
            time.sleep(2)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        sys.exit(f"error {resp.status_code}: {detail}")
    return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text


def indent(text: str, max_lines: int | None) -> str:
    lines = text.rstrip().splitlines() or [""]
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    return "\n".join("    " + line for line in lines)


def iter_sse(sid: str, after: int):
    """Yields events; returns quietly if the connection drops so the caller can reconnect from `after`."""
    try:
        yield from _iter_sse(sid, after)
    except httpx.HTTPError as e:
        print(f"{DIM}connection lost ({type(e).__name__}); reconnecting...{RESET}")


def _iter_sse(sid: str, after: int):
    with httpx.stream("GET", f"{BASE}/sessions/{sid}/events", params={"after": after},
                      timeout=httpx.Timeout(None, connect=10)) as resp:
        if resp.status_code != 200:
            sys.exit(f"error {resp.status_code}: {resp.read().decode()}")
        data = []
        for line in resp.iter_lines():
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "" and data:
                yield json.loads("\n".join(data))
                data = []


def watch(sid: str, args, after: int = 0) -> int:
    streamed = {"content": False, "reasoning": False}
    position = None
    last_content = ""
    while True:
        prompt_for = None
        for e in iter_sse(sid, after):
            t, d = e["type"], e["data"]
            if e["seq"] is not None:
                after = e["seq"]
            if t == "delta":
                if d["kind"] == "reasoning" and not args.reasoning:
                    continue
                if not any(streamed.values()):
                    print(f"{CYAN}assistant:{RESET} ", end="")
                if d["kind"] == "reasoning" and not streamed["reasoning"]:
                    print(DIM, end="")
                if d["kind"] == "content" and streamed["reasoning"] and not streamed["content"]:
                    print(RESET + "\n", end="")
                streamed[d["kind"]] = True
                print(d["text"], end="", flush=True)
                continue
            if t == "queue":
                if d["position"] != position and d["position"] > 0:
                    print(f"{DIM}queued: position {d['position']}{RESET}")
                position = d["position"]
                continue
            if t == "compacting":
                print(f"{DIM}compacting {d['messages']} messages...{RESET}")
                continue
            if t == "user_message":
                print(f"{BOLD}user:{RESET} {d['content']}")
            elif t == "assistant":
                last_content = d["content"].strip()
                if any(streamed.values()):
                    print(RESET)
                elif d["content"].strip():
                    print(f"{CYAN}assistant:{RESET} {d['content'].strip()}")
                streamed = {"content": False, "reasoning": False}
                for call in d["tool_calls"]:
                    print(f"  {CYAN}→ {call['function']['name']}{RESET} {call['function']['arguments'][:300]}")
                tps = f", {d['gen_tps']} tok/s" if d.get("gen_tps") else ""
                print(f"  {DIM}[{d['prompt_tokens']} prompt / {d['completion_tokens']} completion tokens{tps}]{RESET}")
            elif t == "tool_call" and d["decision"] != "allow":
                print(f"  {YELLOW}policy {d['decision']}: {d['reason']}{RESET}")
            elif t == "tool_result":
                color = GREEN if d["ok"] else RED
                print(f"  {color}← {d['name']}{RESET} {DIM}({d['seconds']}s){RESET}")
                print(DIM + indent(d["output"], None if args.full else 12) + RESET)
            elif t == "approval_requested":
                print(f"\n{YELLOW}{BOLD}approval needed [{d['id']}]{RESET}{YELLOW}: {d['tool']} — {d['reason']}{RESET}")
                print(indent(json.dumps(d["args"], indent=2), 40))
                if d.get("detail"):
                    print(indent(d["detail"], 80))
                prompt_for = d["id"]
            elif t == "approval_decided":
                print(f"{YELLOW}approval {d['id']} {d['status']}{RESET}")
                if prompt_for == d["id"]:
                    prompt_for = None
            elif t == "compaction":
                print(f"{DIM}context compacted ({d['tier']}): ~{d['tokens_before']} → ~{d['tokens_after']} tokens{RESET}")
            elif t in ("error", "llm_retry"):
                print(f"{RED}{t}: {d.get('message') or d.get('error')}{RESET}")
            elif t == "resumed":
                print(f"{YELLOW}daemon restarted; session resumed{RESET}")
            elif t == "status":
                print(f"{DIM}status: {d['status']}{(' (' + d['stop_reason'] + ')') if d.get('stop_reason') else ''}{RESET}")
                # Replayed history can contain earlier runs' endings; only stop if the session is still ended.
                if d["status"] in TERMINAL and api("GET", f"/sessions/{sid}")["status"] in TERMINAL:
                    if d.get("answer") and d["answer"].strip() != last_content:
                        print(f"\n{GREEN}{BOLD}answer:{RESET}\n{d['answer'].strip()}")
                    return 0 if d["status"] == "done" else 1
            if prompt_for and t in ("approval_requested", "status") and sys.stdin.isatty() and not args.no_prompt:
                pending = {a["id"] for a in api("GET", f"/sessions/{sid}/approvals")}
                if prompt_for in pending:
                    break  # leave the stream to ask, then reconnect from `after`
                prompt_for = None
        else:
            time.sleep(2)  # stream ended or dropped; reconnect
            continue
        answer = input(f"{YELLOW}approve {prompt_for}? [y]es / [n]o / [s]kip: {RESET}").strip().lower()
        if answer.startswith("y"):
            api("POST", f"/sessions/{sid}/approvals/{prompt_for}", json={"decision": "approve"})
        elif answer.startswith("n"):
            note = input("note for the agent (optional): ")
            api("POST", f"/sessions/{sid}/approvals/{prompt_for}", json={"decision": "deny", "note": note})
        else:
            print(f"{DIM}left pending; approve later with: approve {sid} {prompt_for}{RESET}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.system("")  # enable ANSI colors in the Windows console
    parser = argparse.ArgumentParser(prog="python -m harness.cli", description="agent-harness client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="start a session")
    p.add_argument("prompt")
    p.add_argument("--project", default="scratch")
    p.add_argument("--model")
    p.add_argument("--title")
    p.add_argument("--detach", action="store_true", help="don't watch")
    for name, help_ in (("watch", "stream a session's events"), ("show", "session details"),
                        ("transcript", "Markdown transcript"), ("cancel", "cancel a session")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("session")
    for sp in (p, sub.choices["watch"]):
        sp.add_argument("--reasoning", action="store_true", help="also stream the model's reasoning")
        sp.add_argument("--full", action="store_true", help="show full tool output")
        sp.add_argument("--no-prompt", action="store_true", help="don't ask about approvals interactively")
    sp = sub.add_parser("send", help="send a message (continues a finished session)")
    sp.add_argument("session")
    sp.add_argument("message")
    sp.add_argument("--watch", action="store_true")
    for name in ("approve", "deny"):
        sp = sub.add_parser(name, help=f"{name} a pending tool call")
        sp.add_argument("session")
        sp.add_argument("approval", nargs="?", default="pending")
        sp.add_argument("--note", default="")
    sub.add_parser("list", help="list sessions")
    sub.add_parser("queue", help="GPU queue")
    args = parser.parse_args()

    if args.cmd == "new":
        s = api("POST", "/sessions", json={"prompt": args.prompt, "project": args.project,
                                           "model": args.model, "title": args.title})
        print(f"session {s['id']} ({s['project']}, {s['model']})")
        return 0 if args.detach else watch(s["id"], args)
    if args.cmd == "watch":
        return watch(api("GET", f"/sessions/{args.session}")["id"], args)
    if args.cmd == "list":
        for s in api("GET", "/sessions"):
            when = time.strftime("%m-%d %H:%M", time.localtime(s["created_at"]))
            print(f"{s['id']}  {when}  {s['status']:<16} {s['project']:<10} {s['title']}")
        return 0
    if args.cmd == "show":
        print(json.dumps(api("GET", f"/sessions/{args.session}"), indent=2))
        return 0
    if args.cmd == "transcript":
        print(api("GET", f"/sessions/{args.session}/transcript"))
        return 0
    if args.cmd == "cancel":
        print(api("POST", f"/sessions/{args.session}/cancel")["status"])
        return 0
    if args.cmd == "send":
        before = api("GET", f"/sessions/{args.session}")
        s = api("POST", f"/sessions/{args.session}/messages", json={"content": args.message})
        print(f"sent; session is {s['status']}")
        if args.watch:
            args.reasoning = args.full = args.no_prompt = False
            return watch(s["id"], args, after=before["last_event_seq"])
        return 0
    if args.cmd in ("approve", "deny"):
        a = api("POST", f"/sessions/{args.session}/approvals/{args.approval}",
                json={"decision": args.cmd, "note": args.note})
        print(f"{a['id']} {a['status']}")
        return 0
    if args.cmd == "queue":
        for q in api("GET", "/queue"):
            print(f"{q['position']}  {q['session_id']}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
