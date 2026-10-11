"""A disposable Agent Harness Server in fake model mode, for UI testing without the GPU (#588, docs/ui-testing.md).

    python -m harness.fake_instance --dir DIR [--port N] [--web-port N] [--fixtures PATH] [--keep] [--no-seed]
    python -m harness.fake_instance stop --dir DIR

Start writes a throwaway config and data directory (with the marker fake model mode requires), starts the real daemon
on loopback with the scripted model, serves a separately hosted copy of Agent Harness Web next to it, seeds sessions,
a chat and jobs, waits until they settle and prints a READY line. Ctrl+C stops it and deletes the directory unless
--keep. `stop` ends the instance recorded in DIR from another shell and deletes DIR; it is safe to repeat. Nothing here
reads or changes the real config, data, GPU or llama-server.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
import yaml

from . import fake_model

PROJECT = "ui"
HANDLE = "instance.json"     # in the instance dir: how `stop` finds this instance's process
CLIENT = {"X-Agent-Harness-Client": "cli/2"}  # the seeding talks as a tool on this machine
SETTLE_SECONDS = 120
# (title, prompt, status once settled). The prompts pick scripts in harness/fake_fixtures/ui.yaml; runs queue one at
# a time, in this order.
SEED_SESSIONS = (
    ("Tab bar tour", "Hello! Give me a quick tour of the tab bar.", "done"),
    ("Tool rows", "Show tool rows: write, read and search some files.", "done"),
    ("Retry after a model error", "Simulate a retry after a model error.", "done"),
    ("Failed run", "Simulate a failure.", "failed"),
    ("Long task with compaction", "Run a long task that triggers compaction.", "done"),
    ("Protected settings", "Edit the protected settings file.", "waiting_approval"),
)
SEED_CHATS = ("Hello from Chat. What can you do?",)
SEED_JOBS = (
    {"name": "Nightly report", "prompt": "Write the nightly report.", "cron": "0 3 * * *", "project": PROJECT},
    {"name": "Weekly cleanup", "prompt": "Write the nightly report.", "cron": "0 4 * * 1", "project": PROJECT,
     "enabled": False},
)


class StartError(RuntimeError):
    pass


def available_port(requested: int) -> int | None:
    """A loopback port that is free right now: `requested` (0: any), or None if it is taken. Not held after return."""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", requested))
        except OSError:
            return None
        return s.getsockname()[1]


def write_instance(root: Path, port: int) -> tuple[Path, Path]:
    """The throwaway config and data dirs. Optional modules that reach other services are off."""
    config_dir, data_dir = root / "config", root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / fake_model.MARKER).write_text("disposable fake-model instance (python -m harness.fake_instance)\n",
                                              encoding="utf-8")
    config_dir.mkdir(parents=True, exist_ok=True)
    (root / "repos").mkdir(exist_ok=True)
    config = {
        "listen": {"host": "127.0.0.1", "port": port},
        "data_dir": data_dir.as_posix(),
        "repos_dir": (root / "repos").as_posix(),
        # Replaced by fake_model.apply; present so the config also loads on its own.
        "default_model": fake_model.MODEL_NAME,
        "models": {fake_model.MODEL_NAME: {"base_url": fake_model.UNROUTABLE}},
        "modules": {"homelab": False, "memory_library": False, "images": False, "image_edit": False,
                    "gpu_guard": False, "endpoint": False, "web": False, "notifications": False,
                    "backup": False, "remote_control": False, "hub": False},
        "jobs": {"enabled": True},
        "search": {"enabled": True},
        "skills": {"enabled": True, "local_review": False},
        "canary": {"enabled": False},
        "budgets": {"approval_timeout_seconds": 86400},
    }
    projects = {"projects": {
        PROJECT: {"description": "UI testing: an empty workspace; writes under protected/ ask first.",
                  "policy": {"rules": [{"tool": ["write_file", "edit_file"], "path": "protected/**",
                                        "action": "ask", "reason": "protected file (fake-model UI testing)"}]}},
        "scratch": {"description": "Empty workspace for each session."},
    }}
    (config_dir / "harness.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (config_dir / "projects.yaml").write_text(yaml.safe_dump(projects, sort_keys=False), encoding="utf-8")
    return config_dir, data_dir


def connect_page(server: str, token: str) -> str:
    """Signs the separately hosted copy in (the keys Agent Harness Web's client reads) and opens it."""
    values = json.dumps({"harness.daemonUrl": server, "harness.ownerToken": token})
    return ("<!doctype html><meta charset=utf-8><title>Connect</title>"
            f"<p>Connecting to {html.escape(server)}...</p><script>const v={values};"
            "for (const k in v) localStorage.setItem(k, v[k]); location.replace('/');</script>")


def web_app(server: str, token: str, state: dict):
    """The separately hosted Web copy, its /connect sign-in page, and /ready (503 until seeded and settled)."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles
    web = Path(__file__).resolve().parent / "web"

    async def connect(_request):
        return HTMLResponse(connect_page(server, token), headers={"Cache-Control": "no-store"})

    async def ready(_request):
        return JSONResponse(state, status_code=200 if state.get("status") == "ready" else 503,
                            headers={"Cache-Control": "no-store"})
    # /static too, as the daemon serves it: the manifest and older installs refer to it.
    return Starlette(routes=[Route("/connect", connect), Route("/ready", ready),
                             Mount("/static", StaticFiles(directory=web)),
                             Mount("/", StaticFiles(directory=web, html=True))])


async def seed(client: httpx.AsyncClient) -> list[str]:
    """Sessions, a chat and jobs through the real API, as the owner; then wait for the runs to settle."""
    problems: list[str] = []
    expected: dict[str, tuple[str, str, str]] = {}   # id -> (title, status once settled, path to poll)
    for title, prompt, status in SEED_SESSIONS:
        r = await client.post("/api/v1/sessions", json={"prompt": prompt, "project": PROJECT, "title": title})
        if r.status_code != 201:
            problems.append(f"session {title!r}: HTTP {r.status_code} {r.text[:200]}")
        else:
            expected[r.json()["id"]] = (title, status, f"/api/v1/sessions/{r.json()['id']}")
    for prompt in SEED_CHATS:
        r = await client.post("/api/admin/v1/chats", json={"prompt": prompt})
        if r.status_code != 201:
            problems.append(f"chat: HTTP {r.status_code} {r.text[:200]}")
        else:
            expected[r.json()["id"]] = (prompt, "done", f"/api/admin/v1/chats/{r.json()['id']}")
    for job in SEED_JOBS:
        r = await client.post("/api/admin/v1/jobs", json=job)
        if r.status_code != 201:
            problems.append(f"job {job['name']!r}: HTTP {r.status_code} {r.text[:200]}")
    return problems or await settle(client, expected)


async def settle(client: httpx.AsyncClient, expected: dict[str, tuple[str, str, str]]) -> list[str]:
    deadline = time.monotonic() + SETTLE_SECONDS
    statuses: dict[str, str] = {}
    while time.monotonic() < deadline:
        for sid, (_, _, path) in expected.items():
            r = await client.get(path)
            statuses[sid] = r.json().get("status", "") if r.status_code == 200 else f"HTTP {r.status_code}"
        if all(statuses[sid] == want for sid, (_, want, _) in expected.items()):
            return []
        await asyncio.sleep(0.25)
    return [f"{title!r} is {statuses.get(sid)!r}, expected {want!r} after {SETTLE_SECONDS}s"
            for sid, (title, want, _) in expected.items() if statuses.get(sid) != want]


def write_handle(root: Path, state: dict) -> None:
    (root / HANDLE).write_text(json.dumps(state, indent=1), encoding="utf-8")


def summary(state: dict, token: str, local_token: str, keep: bool) -> str:
    return "\n".join([
        "",
        "Agent Harness fake model instance is up (scripted model, no GPU).",
        f"  Web (signed in):  {state['connect']}",
        f"  Server:           {state['server']}   (bundled Web there needs the local token header)",
        f"  Owner token:      {token}",
        f"  Local token:      {local_token}   (header X-Agent-Harness-Local-Token)",
        f"  Fixtures:         {state['fixtures']}",
        f"  Instance dir:     {state['dir']}" + ("   (kept on exit)" if keep else "   (deleted on exit)"),
        f"  Process:          {state['pid']}   (stop: python -m harness.fake_instance stop --dir \"{state['dir']}\")",
        "READY " + json.dumps({k: state[k] for k in ("server", "web", "connect", "dir", "pid", "key_id")}),
        "Ctrl+C to stop.",
        "",
    ])


async def serve(args, root: Path) -> None:
    from . import config as config_mod
    from .api import create_app
    from .apps import normalize_origin
    from .manager import Manager

    port, web_port = available_port(args.port), available_port(args.web_port)
    if port is None or web_port is None:
        raise StartError("a port is in use; this command never reuses a running server")
    server, web = f"http://127.0.0.1:{port}", f"http://127.0.0.1:{web_port}"
    import psutil
    state = {"status": "starting", "pid": os.getpid(), "pid_created": psutil.Process().create_time(),
             "server": server, "web": web, "connect": f"{web}/connect", "dir": str(root),
             "fixtures": str(Path(args.fixtures).resolve()), "started_at": time.time()}
    config_dir, data_dir = write_instance(root, port)  # the marker first: it is what lets `stop` delete the dir
    write_handle(root, state)
    os.environ[fake_model.ENV] = state["fixtures"]
    manager = Manager(config_mod.load(config_dir, data_dir))
    try:
        key, token = manager.db.main.create_api_key("ui-testing", "admin", "owner", origins=[normalize_origin(web)])
        state["key_id"] = key["id"]  # the actor key_id of this instance's audit rows
        servers = [uvicorn.Server(uvicorn.Config(create_app(manager), host="127.0.0.1", port=port,
                                                 log_level="warning", proxy_headers=False, forwarded_allow_ips="")),
                   uvicorn.Server(uvicorn.Config(web_app(server, token, state), host="127.0.0.1", port=web_port,
                                                 log_level="warning"))]
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        try:
            await _started(servers, tasks)
            if not args.no_seed:
                # trust_env=False: a configured HTTP(S)_PROXY must not carry loopback requests elsewhere.
                async with httpx.AsyncClient(base_url=server, timeout=30, trust_env=False,
                                             headers={"Authorization": f"Bearer {token}", **CLIENT}) as client:
                    problems = await seed(client)
                if problems:
                    raise StartError("seeding failed: " + "; ".join(problems))
            state["status"] = "ready"
            write_handle(root, state)
            print(summary(state, token, manager.local_owner_token, args.keep), flush=True)
            # Ctrl+C reaches whichever server installed its signal handler last: when one stops, stop the other too.
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for s in servers:
                s.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        manager.db.close()  # releases the SQLite files, so the instance directory can be deleted


async def _started(servers, tasks) -> None:
    while not all(s.started for s in servers):
        if any(t.done() for t in tasks):
            raise StartError("a server did not start (see the log above)")
        await asyncio.sleep(0.1)


def remove(root: Path) -> bool:
    """Delete the instance directory, including the read-only objects of its checkpoint git repositories."""
    from .maintenance import remove_tree
    for _ in range(20):  # SQLite handles can linger a moment on Windows
        try:
            remove_tree(root)
            return True
        except OSError:
            time.sleep(0.5)
    return not root.exists()


def owned(root: Path) -> bool:
    """Whether `root` is a fake-model instance directory this command may delete (or an empty leftover)."""
    return (root / "data" / fake_model.MARKER).is_file() or (root.is_dir() and not any(root.iterdir()))


def _instance_process(handle: dict):
    """The live process the handle recorded, or None: the pid must still be that same process (its start time
    matches), so a reused pid, even another instance's, is never killed."""
    import psutil
    try:
        proc = psutil.Process(int(handle["pid"]))
        if abs(proc.create_time() - float(handle["pid_created"])) > 0.01:
            return None
        return proc if "harness.fake_instance" in " ".join(proc.cmdline()) else None
    except (KeyError, TypeError, ValueError, psutil.Error):
        return None


def stop(root: Path) -> int:
    """End the instance recorded in `root` and delete `root`. Safe to repeat, and after a partial start."""
    import psutil
    root = root.resolve()
    if not root.exists():
        print(f"nothing to stop: {root} does not exist")
        return 0
    if not owned(root):
        print(f"refusing: {root} is not a fake-model instance directory", file=sys.stderr)
        return 2
    try:
        handle = json.loads((root / HANDLE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        handle = {}
    proc = _instance_process(handle) if isinstance(handle, dict) else None
    if proc is not None:
        try:
            for p in proc.children(recursive=True) + [proc]:
                p.kill()
            psutil.wait_procs([proc], timeout=15)
            print(f"stopped process {proc.pid}")
        except psutil.Error as e:
            print(f"process {proc.pid}: {e}", file=sys.stderr)
    if not remove(root):
        print(f"could not delete {root}; is a process still using it?", file=sys.stderr)
        return 1
    print(f"deleted {root}")
    return 0


def _interrupt(_sig, _frame):
    raise KeyboardInterrupt


def start(args, ap) -> int:
    fake_model.load_fixtures(args.fixtures)  # fail before creating anything
    for flag, port in (("--port", args.port), ("--web-port", args.web_port)):
        if port and available_port(port) is None:
            ap.error(f"{flag} {port} is in use; this command never reuses a running server")
    if args.dir is not None and args.dir.exists() and any(args.dir.iterdir()):
        ap.error(f"{args.dir} is not empty (stop the instance there first: python -m harness.fake_instance stop "
                 f"--dir {args.dir})")
    if args.dir is not None:
        args.dir.mkdir(parents=True, exist_ok=True)
    root = (args.dir or Path(tempfile.mkdtemp(prefix="harness-fake-"))).resolve()
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # uvicorn re-raises the signal that stopped it; as KeyboardInterrupt, Ctrl+Break and SIGTERM still reach cleanup.
    for name in ("SIGBREAK", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    code = 0
    try:
        asyncio.run(serve(args, root))
    except KeyboardInterrupt:
        pass
    except StartError as e:
        print(f"FAILED {e}", file=sys.stderr, flush=True)
        code = 1
    finally:
        if not args.keep and not remove(root):
            print(f"could not delete {root}; remove it by hand", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["stop"]:
        ap = argparse.ArgumentParser(prog="python -m harness.fake_instance stop",
                                     description="Stop a fake-model instance and delete its directory")
        ap.add_argument("--dir", type=Path, required=True, help="the instance directory given to start")
        return stop(ap.parse_args(argv[1:]).dir)
    ap = argparse.ArgumentParser(prog="python -m harness.fake_instance",
                                 description="Disposable Agent Harness Server in fake model mode (UI testing)")
    ap.add_argument("--port", type=int, default=0, help="server port (default: a free one); refused if in use")
    ap.add_argument("--web-port", type=int, default=0, help="port of the separately hosted Web copy (default: free)")
    ap.add_argument("--fixtures", type=Path, default=fake_model.DEFAULT_FIXTURES,
                    help="fixture directory or file (default: harness/fake_fixtures)")
    ap.add_argument("--dir", type=Path, help="instance directory (default: a new temp dir); must be empty or absent")
    ap.add_argument("--keep", action="store_true", help="keep the instance directory on exit")
    ap.add_argument("--no-seed", action="store_true", help="start empty")
    return start(ap.parse_args(argv), ap)


if __name__ == "__main__":
    sys.exit(main())
