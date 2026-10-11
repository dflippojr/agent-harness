"""A disposable Agent Harness Server in fake model mode, for UI testing without the GPU (#588, docs/ui-testing.md).

    python -m harness.fake_instance [--port 18100] [--web-port 18101] [--fixtures DIR] [--dir DIR] [--keep]

It writes a throwaway config and data directory (with the marker fake model mode requires), starts the real daemon
on loopback with the scripted model, serves a separately hosted copy of Agent Harness Web next to it, seeds a few
sessions, chats and jobs, and prints the URLs and tokens. Ctrl+C stops it and deletes the directory unless --keep.
Nothing here reads or changes the real config, data, GPU or llama-server.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import shutil
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
CLIENT = {"X-Agent-Harness-Client": "cli/2"}  # the seeding talks as a tool on this machine
# The scripts in harness/fake_fixtures/ui.yaml these prompts pick, in the order they queue (the slow one last, so it
# stays Running while the tester looks around).
SEED_SESSIONS = (
    ("Tab bar tour", "Hello! Give me a quick tour of the tab bar."),
    ("Tool rows", "Show tool rows: write, read and search some files."),
    ("Retry after a model error", "Simulate a retry after a model error."),
    ("Failed run", "Simulate a failure."),
    ("Long task with compaction", "Run a long task that triggers compaction."),
    ("Protected settings", "Edit the protected settings file."),
    ("Slow running task", "Run a slow task so I can watch it running."),
)
SEED_CHATS = ("Hello from Chat. What can you do?",)
SEED_JOBS = (
    {"name": "Nightly report", "prompt": "Write the nightly report.", "cron": "0 3 * * *", "project": PROJECT},
    {"name": "Weekly cleanup", "prompt": "Write the nightly report.", "cron": "0 4 * * 1", "project": PROJECT,
     "enabled": False},
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_instance(root: Path, port: int) -> tuple[Path, Path]:
    """The throwaway config and data dirs. Optional modules that reach other services are off."""
    config_dir, data_dir = root / "config", root / "data"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (root / "repos").mkdir(exist_ok=True)
    (data_dir / fake_model.MARKER).write_text("disposable fake-model instance (python -m harness.fake_instance)\n",
                                              encoding="utf-8")
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


def web_app(server: str, token: str):
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles
    web = Path(__file__).resolve().parent / "web"

    async def connect(_request):
        return HTMLResponse(connect_page(server, token), headers={"Cache-Control": "no-store"})
    # /static too, as the daemon serves it: the manifest and older installs refer to it.
    return Starlette(routes=[Route("/connect", connect),
                             Mount("/static", StaticFiles(directory=web)),
                             Mount("/", StaticFiles(directory=web, html=True))])


async def seed(server: str, token: str) -> list[str]:
    """Sessions, a chat and jobs through the real API, as the owner. Returns what failed, if anything."""
    problems = []
    headers = {"Authorization": f"Bearer {token}", **CLIENT}
    async with httpx.AsyncClient(base_url=server, headers=headers, timeout=30) as client:
        for title, prompt in SEED_SESSIONS:
            r = await client.post("/api/v1/sessions", json={"prompt": prompt, "project": PROJECT, "title": title})
            if r.status_code != 201:
                problems.append(f"session {title!r}: HTTP {r.status_code} {r.text[:200]}")
        for prompt in SEED_CHATS:
            r = await client.post("/api/admin/v1/chats", json={"prompt": prompt})
            if r.status_code != 201:
                problems.append(f"chat: HTTP {r.status_code} {r.text[:200]}")
        for job in SEED_JOBS:
            r = await client.post("/api/admin/v1/jobs", json=job)
            if r.status_code != 201:
                problems.append(f"job {job['name']!r}: HTTP {r.status_code} {r.text[:200]}")
    return problems


def summary(server: str, web: str, token: str, local_token: str, root: Path, fixtures: Path, keep: bool) -> str:
    return "\n".join([
        "",
        "Agent Harness fake model instance is up (scripted model, no GPU).",
        f"  Web (signed in):  {web}/connect",
        f"  Server:           {server}   (bundled Web there needs the local token header)",
        f"  Owner token:      {token}",
        f"  Local token:      {local_token}   (header X-Agent-Harness-Local-Token)",
        f"  Fixtures:         {fixtures}",
        f"  Instance dir:     {root}" + ("   (kept on exit)" if keep else "   (deleted on exit)"),
        "Ctrl+C to stop.",
        "",
    ])


async def serve(args, root: Path) -> None:
    from . import config as config_mod
    from .api import create_app
    from .apps import normalize_origin
    from .manager import Manager

    port = args.port or free_port()
    web_port = args.web_port or free_port()
    server, web = f"http://127.0.0.1:{port}", f"http://127.0.0.1:{web_port}"
    config_dir, data_dir = write_instance(root, port)
    os.environ[fake_model.ENV] = str(args.fixtures)
    cfg = config_mod.load(config_dir, data_dir)
    manager = Manager(cfg)
    _, token = manager.db.main.create_api_key("ui-testing", "admin", "owner", origins=[normalize_origin(web)])
    app = create_app(manager)
    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                             proxy_headers=False, forwarded_allow_ips="")),
               uvicorn.Server(uvicorn.Config(web_app(server, token), host="127.0.0.1", port=web_port,
                                             log_level="warning"))]
    try:
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        while not all(s.started for s in servers):
            if any(t.done() for t in tasks):
                await asyncio.gather(*tasks)
                raise SystemExit("the fake instance did not start")
            await asyncio.sleep(0.1)
        problems = [] if args.no_seed else await seed(server, token)
        print(summary(server, web, token, manager.local_owner_token, root, Path(args.fixtures), args.keep), flush=True)
        for problem in problems:
            print(f"  seed failed: {problem}", file=sys.stderr, flush=True)
        # Ctrl+C reaches whichever server installed its signal handler last: when one stops, stop the other too.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        manager.db.close()  # releases the SQLite files, so the instance directory can be deleted


def remove(root: Path) -> None:
    for _ in range(10):  # SQLite and log handles can linger a moment on Windows
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            return
        time.sleep(0.5)
    print(f"could not delete {root}; remove it by hand", file=sys.stderr)


def _interrupt(_sig, _frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Disposable Agent Harness Server in fake model mode (UI testing)")
    ap.add_argument("--port", type=int, default=0, help="server port (default: a free one)")
    ap.add_argument("--web-port", type=int, default=0, help="port of the separately hosted Web copy (default: free)")
    ap.add_argument("--fixtures", type=Path, default=fake_model.DEFAULT_FIXTURES,
                    help="fixture directory or file (default: harness/fake_fixtures)")
    ap.add_argument("--dir", type=Path, help="instance directory (default: a new temp dir); must be empty or absent")
    ap.add_argument("--keep", action="store_true", help="keep the instance directory on exit")
    ap.add_argument("--no-seed", action="store_true", help="start empty")
    args = ap.parse_args(argv)
    fake_model.load_fixtures(args.fixtures)  # fail before creating anything
    if args.dir is not None and args.dir.exists() and any(args.dir.iterdir()):
        ap.error(f"{args.dir} is not empty")
    root = (args.dir or Path(tempfile.mkdtemp(prefix="harness-fake-"))).resolve()
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # uvicorn re-raises the signal that stopped it; as KeyboardInterrupt, Ctrl+Break and SIGTERM still reach cleanup.
    for name in ("SIGBREAK", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    try:
        asyncio.run(serve(args, root))
    except KeyboardInterrupt:
        pass
    finally:
        if not args.keep:
            remove(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
