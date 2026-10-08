"""Small CLI client for testing the daemon: python -m harness.cli --help"""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote

import httpx

try:
    from .compat import CLIENT_PROTOCOLS, MAC_CLIENT_VERSION
    from .updater import apply_update
    from . import modules as _modules
except ImportError:  # installed native bundle imports these as sibling modules
    from harness_compat import CLIENT_PROTOCOLS, MAC_CLIENT_VERSION
    from harness_update import apply_update
    _modules = None
MODULE_COMMANDS_FILE = "harness_module_commands.json"  # the Mac bundle's copy of the modules' rows (mac_client.py)

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
        if d.get("tier") == "mask":
            saved = int(d.get("tokens_saved") or 0)
            print(f"{DIM}Replaced old tool outputs with recoverable receipts (~{saved} tokens saved){RESET}")
        elif d.get("tier") == "round_reset":
            print(f"{DIM}round reset: ~{d['tokens_before']} → ~{d['tokens_after']} tokens{RESET}")
        else:
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


# Issue #334: every owner setting and action Agent Harness Web offers is also a command here, so Web stays optional
# (docs/management-parity.md). A row is (words, method, owner API path, help, fields). Each `{param}` in the path is
# a positional argument. A field "name" is a positional argument and "--name" an option; a suffix sets its type:
# ":int", ":float", ":bool" (true/false), ":flag" (no value), ":list" (zero or more values), ":pairs" (key=value,
# JSON values) or ":file" (a multipart upload). GET fields go in the query string, others in the JSON body (in the
# form when the row uploads a file). --set key=value and --json '{...}' add fields a row doesn't name.
_REVISION = ("--revision:int", "--confirm:flag", "--dry_run:flag")
ADMIN_COMMANDS = (
    ("me", "GET", "/me", "show the identity the server sees", ()),
    ("capabilities", "GET", "", "show the owner API version, capabilities and operations", ()),
    ("profile show", "GET", "/profile", "show the server profile", ()),
    ("profile set-icon", "PUT", "/profile", "set the profile icon", ("emoji",)),
    ("accounts list", "GET", "/accounts", "list household members", ()),
    ("accounts add", "POST", "/accounts", "add a household member by Tailscale login",
     ("login", "display_name", "--disk_quota_bytes:int", "--max_running:int", "--max_queued:int")),
    ("accounts show", "GET", "/accounts/{user_id}", "show a household member", ()),
    ("accounts update", "PATCH", "/accounts/{user_id}", "rename, rebind, disable or re-enable a member, or set limits",
     ("--display_name", "--login", "--enabled:bool", "--disk_quota_bytes:int", "--max_running:int",
      "--max_queued:int")),
    ("accounts audit", "GET", "/accounts/audit", "show the household audit log", ("--limit:int",)),
    ("accounts github-reset", "POST", "/accounts/{user_id}/github-connection/reset",
     "erase a member's stored GitHub credential", ("--confirm:flag",)),
    ("accounts google-invite", "POST", "/accounts/{user_id}/google/invitation",
     "make a one-time Google link code for a member", ()),
    ("accounts google-cancel-invite", "DELETE", "/accounts/{user_id}/google/invitation",
     "cancel a member's pending Google link code", ()),
    ("accounts google-revoke-sessions", "POST", "/accounts/{user_id}/google/revoke-sessions",
     "sign a member out of every Google Web session", ()),
    ("accounts google-unlink", "DELETE", "/accounts/{user_id}/google", "unlink Google from a member",
     ("--confirm:flag",)),
    ("github-member-auth show", "GET", "/github-member-auth", "show whether members may connect GitHub", ()),
    ("github-member-auth set", "PUT", "/github-member-auth", "let members connect GitHub, or stop them",
     ("enabled:bool",)),
    ("google-signin status", "GET", "/google-signin", "show Google sign-in for members", ()),
    ("keys list", "GET", "/keys", "list App, device and owner keys", ()),
    ("keys create", "POST", "/keys", "create a key (its secret is shown once)",
     ("name", "--kind", "--scopes:list", "--origins:list")),
    ("keys revoke", "DELETE", "/keys/{kid}", "revoke a key", ()),
    ("apps erasures", "GET", "/apps/erasures", "list Apps waiting to be erased", ()),
    ("apps restore", "POST", "/apps/{app_id}/restore", "cancel a revoked App's pending erasure", ()),
    ("apps retention", "PUT", "/apps/{app_id}/retention", "set how many days an App's sessions are kept",
     ("--retention_days:float",)),
    ("provider-credentials list", "GET", "/provider-credentials", "list per-App provider credentials", ()),
    ("provider-credentials set", "POST", "/provider-credentials", "set an App's provider credential policy",
     ("app_id", "backend", "--secret_ref", "--policy", "--models:list")),
    ("provider-credentials revoke", "DELETE", "/provider-credentials/{credential_id}",
     "revoke a provider credential", ()),
    ("pairing-codes list", "GET", "/pairing-codes", "list active App pairing codes", ()),
    ("pairing-codes create", "POST", "/pairing-codes", "make a one-time App pairing code",
     ("name", "origin", "--scopes:list", "--ttl_seconds:int")),
    ("pairing-codes revoke", "DELETE", "/pairing-codes/{pid}", "revoke an App pairing code", ()),
    ("projects list", "GET", "/projects", "list the server's projects", ()),
    ("projects create", "POST", "/projects", "add a project to the server's catalog",
     ("name", "--description", "--target", "--repo", "--github:flag")),
    ("models list", "GET", "/models", "list local models", ()),
    ("models status", "GET", "/models/status", "show the local model's state", ()),
    ("models warm", "POST", "/models/warm", "load the local model ahead of use", ()),
    ("backends list", "GET", "/backends", "list backends (--auth skip skips sign-in checks)", ("--auth",)),
    ("backends set", "PUT", "/backends/{name}", "set a backend's default model and effort", ("--model", "--effort")),
    ("gpu status", "GET", "/gpu", "show the GPU hold", ()),
    ("gpu pause", "POST", "/gpu/pause", "hold the GPU (local models unload)", ("--duration_seconds:int",)),
    ("gpu resume", "POST", "/gpu/resume", "release the GPU hold", ()),
    ("resources status", "GET", "/resources", "show the resource guard and GPU hold", ()),
    ("resources diagnostics", "GET", "/resources/diagnostics", "show memory, VRAM and load details", ()),
    ("resources load", "POST", "/resources/load", "load the local model now",
     ("--duration_seconds:int", "--force:flag")),
    ("resources unload", "POST", "/resources/unload", "unload the local model", ()),
    ("resources pause", "POST", "/resources/pause", "hold the GPU", ("--duration_seconds:int",)),
    ("resources resume", "POST", "/resources/resume", "release the GPU hold", ()),
    ("smart-approvals show", "GET", "/smart-approvals", "show smart approvals status", ()),
    ("smart-approvals set", "PUT", "/smart-approvals", "set smart approvals: off, shadow or auto", ("mode",)),
    ("config show", "GET", "/config", "show daemon settings", ()),
    ("config schema", "GET", "/config/schema", "show the settings registry", ()),
    ("config validate", "POST", "/config/validate", "check settings changes without saving them",
     ("changes:pairs", "--revision:int")),
    ("config set", "PATCH", "/config", "change daemon settings: config set key=value ...",
     ("changes:pairs", "--revision:int", "--dry_run:flag")),
    ("config rollback", "POST", "/config/rollback", "roll settings back to the previous revision", _REVISION),
    ("config restart", "POST", "/config/restart", "restart the daemon to apply settings", _REVISION),
    ("maintenance status", "GET", "/maintenance", "show disk usage and maintenance state", ()),
    ("maintenance cleanup", "POST", "/maintenance/cleanup", "remove expired workspaces and data", ()),
    ("remote-control list", "GET", "/remote-control", "list Remote Control folders and sessions", ()),
    ("remote-control launch", "POST", "/remote-control/{project}", "start Remote Control in a folder", ()),
    ("remote-control stop", "POST", "/remote-control/{project}/stop", "stop Remote Control in a folder", ()),
    ("remote-control trust", "POST", "/remote-control/{project}/trust", "trust a folder for Remote Control", ()),
    ("remote-control scan", "POST", "/remote-control/discovery/scans", "scan for project folders", ()),
    ("remote-control scan-show", "GET", "/remote-control/discovery/scans/{scan_id}", "show a folder scan", ()),
    ("remote-control scan-cancel", "DELETE", "/remote-control/discovery/scans/{scan_id}", "cancel a folder scan", ()),
    ("remote-control promote", "POST", "/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote",
     "add a scanned folder to Remote Control", ("slug", "confirmed_path", "confirmed_markers:list")),
    ("remote-control forget", "DELETE", "/remote-control/folders/{slug}", "remove a discovered folder", ()),
    ("sessions rename", "PATCH", "/sessions/{ref}", "rename a session", ("title",)),
    ("sessions rerun", "POST", "/sessions/{ref}/rerun", "start a new session with the same prompt", ()),
    ("sessions approvals", "GET", "/sessions/{ref}/approvals", "list a session's approvals", ()),
    ("sessions metrics", "GET", "/sessions/{ref}/metrics", "show a session's context-efficiency metrics", ()),
    ("sessions changes", "GET", "/sessions/{ref}/changes", "show a session's diff and secret scan", ()),
    ("sessions merge", "POST", "/sessions/{ref}/review/merge", "merge a session's branch", ()),
    ("sessions push", "POST", "/sessions/{ref}/review/push", "push a session's branch", ()),
    ("sessions discard", "POST", "/sessions/{ref}/review/discard", "discard a session's changes", ()),
    ("sessions comments", "GET", "/sessions/{ref}/review-comments", "list drafted line comments", ()),
    ("sessions comment", "POST", "/sessions/{ref}/review-comments", "draft a line comment on the diff",
     ("path", "side", "start_line:int", "comment", "--end_line:int", "--quoted:list", "--repo", "--base", "--head")),
    ("sessions comment-delete", "DELETE", "/sessions/{ref}/review-comments/{comment_id}", "delete a drafted comment",
     ()),
    ("sessions comments-send", "POST", "/sessions/{ref}/review-comments/send",
     "send the drafted comments as one follow-up", ()),
    ("sessions secrets-fix", "POST", "/sessions/{ref}/secret-findings/fix", "ask the agent to fix secret findings",
     ()),
    ("sessions secret-dismiss", "POST", "/sessions/{ref}/secret-findings/{fingerprint}/dismiss",
     "dismiss a secret finding", ("reason",)),
    ("sessions checkpoints", "GET", "/sessions/{ref}/checkpoints", "list a session's checkpoints", ()),
    ("sessions rewind", "POST", "/sessions/{ref}/checkpoints/{turn}/rewind", "rewind a session to a checkpoint", ()),
    ("sessions fork", "POST", "/sessions/{ref}/checkpoints/{turn}/fork", "start a session from a checkpoint",
     ("prompt",)),
    ("sessions clear-taint", "POST", "/sessions/{ref}/taint/clear", "clear a session's untrusted-content taint", ()),
    ("github items", "GET", "/github/projects/{project}/items", "list a project's GitHub issues and PRs",
     ("--page:int", "--q")),
    ("github item", "GET", "/github/projects/{project}/items/{number}", "show a GitHub issue or PR", ()),
    ("github start", "POST", "/github/sessions", "start a session on a GitHub issue or PR",
     ("project", "number:int", "prompt", "--backend", "--model", "--title")),
    ("chats list", "GET", "/chats", "list chats", ("--limit:int",)),
    ("chats options", "GET", "/chats/options", "list chat backends and models", ()),
    ("chats snippet-languages", "GET", "/chats/snippet-languages", "list the languages chat snippets can run", ()),
    ("chats show", "GET", "/chats/{ref}", "show a chat", ()),
    ("chats new", "POST", "/chats", "start a chat", ("prompt", "--backend", "--model", "--effort")),
    ("chats send", "POST", "/chats/{ref}/messages", "send a chat message", ("content",)),
    ("chats cancel", "POST", "/chats/{ref}/cancel", "stop a chat reply", ()),
    ("chats rename", "PATCH", "/chats/{ref}", "rename a chat", ("title",)),
    ("chats delete", "DELETE", "/chats/{ref}", "delete a chat", ()),
    ("chats run", "POST", "/chats/{ref}/snippets", "run a code snippet in a chat", ("language", "source", "--origin")),
    ("chats run-cancel", "POST", "/chats/{ref}/snippets/{run_id}/cancel", "stop a running snippet", ()),
)
_GROUP_HELP = {
    "profile": "server profile", "accounts": "household members", "github-member-auth": "members' GitHub access",
    "google-signin": "members' Google sign-in", "keys": "App, device and owner keys", "apps": "App data retention",
    "provider-credentials": "per-App provider credentials", "pairing-codes": "App pairing codes",
    "models": "local models", "backends": "model backends",
    "gpu": "GPU hold", "resources": "resource guard and local model", "smart-approvals": "smart approvals",
    "config": "daemon settings", "maintenance": "disk cleanup and backups",
    "remote-control": "Remote Control folders",
    "sessions": "session review, checkpoints and comments", "github": "GitHub issues and PRs", "chats": "chats",
}


def module_commands() -> tuple[tuple, dict]:
    """The add-on modules' rows and group help (harness/modules.py; images adds `images ...`). A checkout asks the
    modules; the Mac bundle reads the copy the Server packed next to this file."""
    if _modules is not None:
        return _modules.cli_rows(), _modules.cli_groups()
    try:
        data = json.loads(Path(__file__).with_name(MODULE_COMMANDS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (), {}
    return tuple((*row[:4], tuple(row[4])) for row in data.get("rows", [])), dict(data.get("groups", {}))


def admin_commands() -> tuple:
    """ADMIN_COMMANDS and the add-on modules' rows."""
    return ADMIN_COMMANDS + module_commands()[0]


_PATH_PARAM = re.compile(r"\{([^}/]+)\}")


def _json_value(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value not in ("true", "false", "yes", "no", "on", "off", "1", "0"):
        raise argparse.ArgumentTypeError(f"expected true or false, not {raw!r}")
    return value in ("true", "yes", "on", "1")


def _pair(raw: str) -> tuple[str, object]:
    key, sep, value = raw.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected key=value, not {raw!r}")
    return key, _json_value(value)


def _field_spec(field: str) -> tuple[str, str, bool]:
    """'--max_running:int' -> ('max_running', 'int', True)."""
    name, _, kind = field.lstrip("-").partition(":")
    return name, kind or "str", field.startswith("--")


def _add_field(parser: argparse.ArgumentParser, field: str) -> None:
    name, kind, optional = _field_spec(field)
    if kind == "flag":
        parser.add_argument(f"--{name.replace('_', '-')}", dest=f"f_{name}", action="store_true", default=None)
        return
    opts = {"metavar": name.upper(), "type": {"int": int, "float": float, "bool": _bool, "pairs": _pair}.get(kind, str)}
    if kind in ("list", "pairs"):
        opts["nargs"] = "*" if kind == "list" else "+"
    if optional:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=f"f_{name}", **opts)
    else:
        parser.add_argument(f"f_{name}", **opts)


def _add_admin_commands(sub, groups: dict) -> None:
    """Register ADMIN_COMMANDS; `groups` maps a command prefix to its subparsers (existing groups are reused)."""
    help_text = _GROUP_HELP | module_commands()[1]
    for row in admin_commands():
        *prefix, leaf = row[0].split()
        parent = sub
        for depth, word in enumerate(prefix):
            key = " ".join(prefix[:depth + 1])
            if key not in groups:
                groups[key] = parent.add_parser(word, help=help_text.get(key, word)).add_subparsers(
                    dest=f"{word.replace('-', '_')}_cmd", required=True)
            parent = groups[key]
        p = parent.add_parser(leaf, help=row[3])
        for param in _PATH_PARAM.findall(row[2]):
            p.add_argument(f"p_{param}", metavar=param.upper())
        for field in row[4]:
            _add_field(p, field)
        p.add_argument("--set", dest="extra", action="append", type=_pair, default=[], metavar="KEY=VALUE",
                       help="another field (JSON values)")
        p.add_argument("--json", dest="body", type=json.loads, help="the whole request body as JSON")
        p.set_defaults(admin=row)


def admin_request(args) -> tuple[str, str, dict]:
    """The (method, path, httpx kwargs) an ADMIN_COMMANDS row and its parsed arguments ask for."""
    _, method, template, _, fields = args.admin
    path = _PATH_PARAM.sub(lambda m: quote(str(getattr(args, f"p_{m.group(1)}")), safe=""), template)
    body = dict(args.body or {})
    files = {}
    for field in fields:
        name, kind, _ = _field_spec(field)
        value = getattr(args, f"f_{name}")
        if value is None:
            continue
        if kind == "file":
            source = Path(value).expanduser()
            files[name] = (source.name, source.read_bytes())
        else:
            body[name] = dict(value) if kind == "pairs" else value
    body.update(args.extra)
    if method == "GET":
        return method, path, {"params": body} if body else {}
    if files:
        return method, path, {"files": files, "data": {k: v if isinstance(v, str) else json.dumps(v)
                                                       for k, v in body.items()}}
    return method, path, {"json": body}


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
    projects = sub.add_parser("projects", help="Mac runner project roots and the server's projects").add_subparsers(
        dest="projects_cmd", required=True)
    add = projects.add_parser("add", help="allow a local project directory")
    add.add_argument("path")
    add.add_argument("--runner-config", default=str(DEFAULT_RUNNER_CONFIG), help=argparse.SUPPRESS)
    runner = sub.add_parser("runner", help="the local launchd runner and the server's runners").add_subparsers(
        dest="runner_cmd", required=True)
    runner.add_parser("status")
    runner.add_parser("restart")
    logs = runner.add_parser("logs")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--lines", type=int, default=80)
    _add_admin_commands(sub, {"projects": projects, "runner": runner})
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


def _cmd_admin(args) -> int:
    method, path, kwargs = admin_request(args)
    result = api(method, path, **kwargs)
    if isinstance(result, str):
        print(result or "ok")
    else:
        print(json.dumps(result, indent=2))
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
    return _cmd_admin(args) if getattr(args, "admin", None) else _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
