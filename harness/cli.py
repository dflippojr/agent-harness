"""Small CLI client for testing the daemon: python -m harness.cli --help"""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

try:
    from .compat import CLIENT_PROTOCOLS, MAC_CLIENT_VERSION
    from .updater import apply_update
except ImportError:  # installed native bundle imports these as sibling modules
    from harness_compat import CLIENT_PROTOCOLS, MAC_CLIENT_VERSION
    from harness_update import apply_update

HOME_DIR = ".agent-harness"
HARNESS_HOME = Path.home() / HOME_DIR
DEFAULT_CONFIG = HARNESS_HOME / "client" / "config.json"
DEFAULT_RUNNER_CONFIG = HARNESS_HOME / "runner" / "config.json"
BASE = "http://127.0.0.1:8100"
TOKEN = ""
CONFIG_PATH = DEFAULT_CONFIG
ADMIN_PREFIX = "/api/admin/v1"
TERMINAL = ("done", "cancelled", "failed")
DIM, BOLD, YELLOW, GREEN, RED, CYAN, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[32m", "\033[31m", "\033[36m", "\033[0m"


def configure(path: Path | str = DEFAULT_CONFIG) -> dict:
    """Load the paired native-client transport. Environment variables remain useful for development."""
    global BASE, TOKEN, CONFIG_PATH
    CONFIG_PATH = Path(path).expanduser()
    data = {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        sys.exit(f"invalid client config {CONFIG_PATH}: {exc}")
    BASE = str(os.environ.get("HARNESS_URL") or data.get("server") or "http://127.0.0.1:8100").rstrip("/")
    TOKEN = str(os.environ.get("HARNESS_TOKEN") or data.get("token") or "").strip()
    return data


def _headers(extra: dict | None = None) -> dict:
    headers = {**(extra or {}), "X-Agent-Harness-Client": f"cli/{CLIENT_PROTOCOLS['cli']}"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def _harness_file(parser: argparse.ArgumentParser, flag: str, value: str) -> Path:
    """A --config / --runner-config path. Both live under ~/.agent-harness (see macrunner/install.sh), and the
    CLI writes credentials to them, so anything that resolves elsewhere is refused."""
    base = HARNESS_HOME.expanduser().resolve()
    path = Path(value).expanduser().resolve()
    if path == base or not path.is_relative_to(base):
        parser.error(f"{flag} must be a file under {base}")
    return path


def _write_private_json(path: Path, data: dict) -> None:
    """Replace `path` with `data`. The temp file is created new and owner-only before the credentials go in, so a
    file or link already sitting at its name is removed rather than written through."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def pair_native(server: str, code: str, client_path: Path, runner_path: Path) -> dict:
    """Redeem once, then persist the owner and runner credentials without printing either secret."""
    server = server.rstrip("/")
    try:
        response = httpx.post(server + "/api/v1/runner-pair", json={"code": code}, timeout=60,
                              headers={"X-Agent-Harness-Client": f"cli/{CLIENT_PROTOCOLS['cli']}"})
    except httpx.TransportError as exc:
        sys.exit(f"daemon not reachable at {server}: {exc}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        sys.exit(f"pairing failed ({response.status_code}): {detail}")
    paired = response.json()
    _write_private_json(client_path, {"server": paired["server"], "token": paired["owner_token"]})
    _write_private_json(runner_path, paired["runner"])
    return paired


def add_project_root(path: str, runner_path: Path = DEFAULT_RUNNER_CONFIG) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project directory does not exist: {root}")
    try:
        config = json.loads(runner_path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("pair this Mac before adding projects") from exc
    roots = [str(Path(item).expanduser().resolve()) for item in config.get("repo_roots") or []]
    if str(root) not in roots:
        roots.append(str(root))
    config["repo_roots"] = roots
    _write_private_json(runner_path, config)
    return root


def _show_runner_logs(log: Path, lines: int, follow: bool) -> int:
    """Print the last `lines` from the runner log, optionally following new lines without invoking a shell tool."""
    try:
        stream = log.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"could not open runner log {log}: {exc}", file=sys.stderr)
        return 1
    with stream:
        sys.stdout.writelines(deque(stream, maxlen=max(1, int(lines))))
        sys.stdout.flush()
        if not follow:
            return 0
        try:
            while True:
                line = stream.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.2)
        except KeyboardInterrupt:
            return 130


def launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    domain = f"gui/{os.getuid()}/dev.agent-harness.runner"
    return subprocess.run(["launchctl", *args, domain], check=check, text=True)


def api(method: str, path: str, retries: int = 30, **kwargs) -> dict | list | str:
    kwargs["headers"] = _headers(kwargs.get("headers"))
    for attempt in range(retries + 1):
        try:
            resp = httpx.request(method, BASE + ADMIN_PREFIX + path, timeout=60, **kwargs)
            break
        except httpx.TransportError:
            if attempt == retries:
                sys.exit(f"daemon not reachable at {BASE}")
            time.sleep(2)
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            detail = payload.get("detail")
            if resp.status_code == 426 and payload.get("error", {}).get("code") == "client_update_required":
                detail = f"{detail}; run `harness update`"
        except ValueError:
            detail = resp.text
        sys.exit(f"error {resp.status_code}: {detail}")
    return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text


def server_version() -> dict:
    try:
        response = httpx.get(BASE + "/health", headers={
            "X-Agent-Harness-Client": f"cli/{CLIENT_PROTOCOLS['cli']}"}, timeout=20)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"daemon not reachable at {BASE}: {exc}") from exc


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
    with httpx.stream("GET", f"{BASE}{ADMIN_PREFIX}/sessions/{sid}/events", params={"after": after},
                      headers=_headers(), timeout=httpx.Timeout(None, connect=10)) as resp:
        if resp.status_code != 200:
            sys.exit(f"error {resp.status_code}: {resp.read().decode()}")
        data = []
        for line in resp.iter_lines():
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "" and data:
                yield json.loads("\n".join(data))
                data = []


def _print_delta(d: dict, streamed: dict) -> None:
    """One streamed token, opening the `assistant:` line and the dim reasoning block as needed."""
    if not any(streamed.values()):
        print(f"{CYAN}assistant:{RESET} ", end="")
    if d["kind"] == "reasoning" and not streamed["reasoning"]:
        print(DIM, end="")
    if d["kind"] == "content" and streamed["reasoning"] and not streamed["content"]:
        print(RESET + "\n", end="")
    streamed[d["kind"]] = True
    print(d["text"], end="", flush=True)


def _print_assistant(d: dict, streamed: dict) -> str:
    """The finished turn, its tool calls and token counts. Returns the content for the answer comparison."""
    if any(streamed.values()):
        print(RESET)
    elif d["content"].strip():
        print(f"{CYAN}assistant:{RESET} {d['content'].strip()}")
    streamed.update(content=False, reasoning=False)
    for call in d["tool_calls"]:
        print(f"  {CYAN}→ {call['function']['name']}{RESET} {call['function']['arguments'][:300]}")
    tps = f", {d['gen_tps']} tok/s" if d.get("gen_tps") else ""
    print(f"  {DIM}[{d['prompt_tokens']} prompt / {d['completion_tokens']} completion tokens{tps}]{RESET}")
    return d["content"].strip()


def _print_event(t: str, d: dict, args) -> None:
    """Events that only print: no loop state depends on them."""
    if t == "user_message":
        print(f"{BOLD}user:{RESET} {d['content']}")
    elif t == "tool_call" and d["decision"] != "allow":
        print(f"  {YELLOW}policy {d['decision']}: {d['reason']}{RESET}")
    elif t == "tool_result":
        color = GREEN if d["ok"] else RED
        print(f"  {color}← {d['name']}{RESET} {DIM}({d['seconds']}s){RESET}")
        print(DIM + indent(d["output"], None if args.full else 12) + RESET)
    elif t == "compaction":
        print(f"{DIM}context compacted ({d['tier']}): ~{d['tokens_before']} → ~{d['tokens_after']} tokens{RESET}")
    elif t in ("error", "llm_retry"):
        print(f"{RED}{t}: {d.get('message') or d.get('error')}{RESET}")
    elif t == "model_waking":
        print(f"{YELLOW}model is asleep; waking it (about {d['expected_seconds']} s)...{RESET}")
    elif t == "model_ready":
        print(f"{DIM}model ready after {d['seconds']} s{RESET}")
    elif t == "resumed":
        print(f"{YELLOW}daemon restarted; session resumed{RESET}")


def _print_approval_request(d: dict) -> None:
    print(f"\n{YELLOW}{BOLD}approval needed [{d['id']}]{RESET}{YELLOW}: {d['tool']} — {d['reason']}{RESET}")
    print(indent(json.dumps(d["args"], indent=2), 40))
    if d.get("detail"):
        print(indent(d["detail"], 80))


def _terminal_exit(d: dict, sid: str, last_content: str) -> int | None:
    """The exit code once the session has really ended, else None."""
    print(f"{DIM}status: {d['status']}{(' (' + d['stop_reason'] + ')') if d.get('stop_reason') else ''}{RESET}")
    # Replayed history can contain earlier runs' endings; only stop if the session is still ended.
    if d["status"] not in TERMINAL or api("GET", f"/sessions/{sid}")["status"] not in TERMINAL:
        return None
    if d.get("answer") and d["answer"].strip() != last_content:
        print(f"\n{GREEN}{BOLD}answer:{RESET}\n{d['answer'].strip()}")
    return 0 if d["status"] == "done" else 1


def _decide_approval(sid: str, approval_id: str) -> None:
    answer = input(f"{YELLOW}approve {approval_id}? [y]es / [n]o / [s]kip: {RESET}").strip().lower()
    if answer.startswith("y"):
        api("POST", f"/sessions/{sid}/approvals/{approval_id}", json={"decision": "approve"})
    elif answer.startswith("n"):
        note = input("note for the agent (optional): ")
        api("POST", f"/sessions/{sid}/approvals/{approval_id}", json={"decision": "deny", "note": note})
    else:
        print(f"{DIM}left pending; approve later with: approve {sid} {approval_id}{RESET}")


def _approval_pending(sid: str, approval_id: str) -> bool:
    return approval_id in {a["id"] for a in api("GET", f"/sessions/{sid}/approvals")}


def _wants_prompt(prompt_for, t: str, args) -> bool:
    return bool(prompt_for) and t in ("approval_requested", "status") and sys.stdin.isatty() and not args.no_prompt


def _note_queue(d: dict, position):
    if d["position"] != position and d["position"] > 0:
        print(f"{DIM}queued: position {d['position']}{RESET}")
    return d["position"]


def _watch_event(e: dict, sid: str, args, state: dict) -> int | None:
    """Handle one event, updating `state`. Returns an exit code once the session has really ended."""
    t, d = e["type"], e["data"]
    if t == "delta":
        if d["kind"] != "reasoning" or args.reasoning:
            _print_delta(d, state["streamed"])
    elif t == "queue":
        state["position"] = _note_queue(d, state["position"])
    elif t == "compacting":
        print(f"{DIM}compacting {d['messages']} messages...{RESET}")
    elif t == "assistant":
        state["last_content"] = _print_assistant(d, state["streamed"])
    elif t == "approval_requested":
        _print_approval_request(d)
        state["prompt_for"] = d["id"]
    elif t == "approval_decided":
        print(f"{YELLOW}approval {d['id']} {d['status']}{RESET}")
        if state["prompt_for"] == d["id"]:
            state["prompt_for"] = None
    elif t == "status":
        return _terminal_exit(d, sid, state["last_content"])
    else:
        _print_event(t, d, args)
    return None


def _consume_stream(sid: str, args, state: dict) -> tuple[int | None, bool]:
    """Read one connection. Returns (exit code, True if the stream was left to ask about an approval)."""
    for e in iter_sse(sid, state["after"]):
        if e["seq"] is not None:
            state["after"] = e["seq"]
        code = _watch_event(e, sid, args, state)
        if code is not None:
            return code, False
        if _wants_prompt(state["prompt_for"], e["type"], args):
            if _approval_pending(sid, state["prompt_for"]):
                return None, True  # leave the stream to ask, then reconnect from `after`
            state["prompt_for"] = None
    return None, False


def watch(sid: str, args, after: int = 0) -> int:
    state = {"streamed": {"content": False, "reasoning": False}, "position": None, "last_content": "",
             "prompt_for": None, "after": after}
    while True:
        state["prompt_for"] = None
        code, ask = _consume_stream(sid, args, state)
        if code is not None:
            return code
        if ask:
            _decide_approval(sid, state["prompt_for"])
        else:
            time.sleep(2)  # stream ended or dropped; reconnect


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description="agent-harness client")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pair", help="pair this Mac using a one-time code from Settings")
    p.add_argument("server")
    p.add_argument("code")
    p.add_argument("--runner-config", default=str(DEFAULT_RUNNER_CONFIG), help=argparse.SUPPRESS)

    p = sub.add_parser("new", help="start a session")
    p.add_argument("prompt")
    p.add_argument("--project", default="scratch")
    p.add_argument("--model")
    p.add_argument("--backend", default="local")
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
    sub.add_parser("version", help="show installed client and connected server versions")
    sub.add_parser("update", help="verify and install the version-matched Mac client package")
    projects = sub.add_parser("projects", help="manage Mac runner project roots").add_subparsers(
        dest="projects_cmd", required=True)
    add = projects.add_parser("add", help="allow a local project directory")
    add.add_argument("path")
    add.add_argument("--runner-config", default=str(DEFAULT_RUNNER_CONFIG), help=argparse.SUPPRESS)
    runner = sub.add_parser("runner", help="manage the local launchd runner").add_subparsers(
        dest="runner_cmd", required=True)
    runner.add_parser("status")
    runner.add_parser("restart")
    logs = runner.add_parser("logs")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--lines", type=int, default=80)
    return parser


def _cmd_pair(args) -> int:
    paired = pair_native(args.server, args.code, args.config, args.runner_config)
    print(f"paired {paired['runner']['name']} with {paired['server']}")
    return 0


def _compatibility(protocol: int, supported: dict) -> str:
    if protocol < supported.get("min", protocol):
        return "client update required"
    if protocol > supported.get("max", protocol):
        return "Server update required"
    return "compatible"


def _cmd_version(args) -> int:
    print(f"Agent Harness CLI {MAC_CLIENT_VERSION} (admin protocol {CLIENT_PROTOCOLS['cli']})")
    try:
        remote = server_version()
    except RuntimeError as exc:
        print(str(exc))
        return 1
    supported = remote.get("protocols", {}).get("admin", {})
    state = _compatibility(CLIENT_PROTOCOLS["cli"], supported)
    print(f"Agent Harness Server {remote.get('release', 'unknown')} build {remote.get('build_id', 'unknown')}")
    print(f"compatibility: {state} (Server supports admin protocol "
          f"{supported.get('min', '?')}–{supported.get('max', '?')})")
    return 0


def _cmd_update(args) -> int:
    try:
        result = apply_update(BASE)
    except RuntimeError as exc:
        sys.exit(str(exc))
    print(f"updated Agent Harness for Mac to {result['version']}")
    return 0


def _cmd_projects(args) -> int:
    try:
        root = add_project_root(args.path, args.runner_config)
        launchctl("kickstart", "-k", check=True)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"could not add project: {exc}")
    print(f"allowed project root {root}")
    return 0


def _cmd_runner(args) -> int:
    if args.runner_cmd == "restart":
        launchctl("kickstart", "-k", check=True)
        print("runner restarted")
        return 0
    if args.runner_cmd == "logs":
        return _show_runner_logs(HARNESS_HOME / "logs" / "runner.log", args.lines, args.follow)
    local = launchctl("print")
    config = json.loads(DEFAULT_RUNNER_CONFIG.read_text(encoding="utf-8"))
    remote = next((row for row in api("GET", "/runners") if row["name"] == config.get("name")), None)
    print(f"launchd: {'loaded' if local.returncode == 0 else 'not loaded'}")
    print("daemon: " + (json.dumps(remote, indent=2) if remote else "runner not configured on daemon"))
    return 0


def _cmd_new(args) -> int:
    s = api("POST", "/sessions", json={"prompt": args.prompt, "project": args.project,
                                       "backend": args.backend, "model": args.model, "title": args.title})
    print(f"session {s['id']} ({s['project']}, {s['model']})")
    return 0 if args.detach else watch(s["id"], args)


def _cmd_watch(args) -> int:
    return watch(api("GET", f"/sessions/{args.session}")["id"], args)


def _cmd_list(args) -> int:
    for s in api("GET", "/sessions"):
        when = time.strftime("%m-%d %H:%M", time.localtime(s["created_at"]))
        print(f"{s['id']}  {when}  {s['status']:<16} {s['project']:<10} {s['title']}")
    return 0


def _cmd_show(args) -> int:
    print(json.dumps(api("GET", f"/sessions/{args.session}"), indent=2))
    return 0


def _cmd_transcript(args) -> int:
    print(api("GET", f"/sessions/{args.session}/transcript"))
    return 0


def _cmd_cancel(args) -> int:
    print(api("POST", f"/sessions/{args.session}/cancel")["status"])
    return 0


def _cmd_send(args) -> int:
    before = api("GET", f"/sessions/{args.session}")
    s = api("POST", f"/sessions/{args.session}/messages", json={"content": args.message})
    print(f"sent; session is {s['status']}")
    if args.watch:
        args.reasoning = args.full = args.no_prompt = False
        return watch(s["id"], args, after=before["last_event_seq"])
    return 0


def _cmd_decide(args) -> int:
    a = api("POST", f"/sessions/{args.session}/approvals/{args.approval}",
            json={"decision": args.cmd, "note": args.note})
    print(f"{a['id']} {a['status']}")
    return 0


def _cmd_queue(args) -> int:
    for q in api("GET", "/queue"):
        print(f"{q['position']}  {q['session_id']}")
    return 0


_COMMANDS = {"pair": _cmd_pair, "version": _cmd_version, "update": _cmd_update, "projects": _cmd_projects,
             "runner": _cmd_runner, "new": _cmd_new, "watch": _cmd_watch, "list": _cmd_list, "show": _cmd_show,
             "transcript": _cmd_transcript, "cancel": _cmd_cancel, "send": _cmd_send, "approve": _cmd_decide,
             "deny": _cmd_decide, "queue": _cmd_queue}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.system("")  # enable ANSI colors in the Windows console
    parser = _build_parser()
    args = parser.parse_args()
    args.config = _harness_file(parser, "--config", args.config)
    if getattr(args, "runner_config", None) is not None:
        args.runner_config = _harness_file(parser, "--runner-config", args.runner_config)
    configure(args.config)
    return _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
