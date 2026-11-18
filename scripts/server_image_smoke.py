"""Check a built Server image the way docs/INSTALL.md runs it (#334 stage e).

    docker build -t agent-harness-server .
    python scripts/server_image_smoke.py agent-harness-server

It starts a container from the image with no network and a new volume, and checks five things:

1. The container answers GET /health.
2. The daemon runs as the image's unprivileged account.
3. Nothing listens beyond 127.0.0.1.
4. The config, data and local owner token are in the volume.
5. A fresh container from the image holds no token, secret or config.

It removes the container and the volume whatever happens, prints one PASS or FAIL line per check, and exits 0 only
when every check passes. The CI job that builds the image runs it (#334).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable

VOLUME = "/var/lib/agent-harness"
USER = "10001:10001"
UID = "10001"
# Run inside the image with its own Python. Both print JSON.
HEALTH = ("import json, urllib.request\n"
          "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
          "from harness.config import resolve_port\n"
          "print(opener.open(f'http://127.0.0.1:{resolve_port()}/health', timeout=10).read().decode())\n")
LISTENING = ("import json\n"
             "out = []\n"
             "for name in ('/proc/net/tcp', '/proc/net/tcp6'):\n"
             "    try:\n"
             "        rows = open(name).read().splitlines()[1:]\n"
             "    except OSError:\n"
             "        continue\n"
             "    out += [row.split()[1] for row in rows if row.split()[3] == '0A']\n"
             "print(json.dumps(out))\n")
SECRETS = ("import json, os\n"
           "found = []\n"
           "for root, dirs, files in os.walk('/'):\n"
           "    dirs[:] = [d for d in dirs if os.path.join(root, d) not in ('/proc', '/sys', '/dev')]\n"
           "    found += [os.path.join(root, f) for f in files if f.endswith(('.token', '.secret'))\n"
           "              or f in ('harness.yaml', 'harness.local.yaml', 'harness.sqlite3', '.credentials.json')]\n"
           f"found += [os.path.join('{VOLUME}', f) for f in os.listdir('{VOLUME}')]\n"
           "print(json.dumps(sorted(found)))\n")
# A listening address in /proc/net/tcp{,6} hex: IPv4 127.0.0.1 and IPv6 ::1, any port.
LOOPBACK = ("0100007F:", "00000000000000000000000001000000:")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def image_config(run: Runner, image: str) -> list[Check]:
    out = run(["image", "inspect", "--format", "{{json .Config}}", image])
    if out.returncode != 0:
        return [Check("image", False, out.stderr.strip() or "docker image inspect failed")]
    config = json.loads(out.stdout)
    env = dict(item.split("=", 1) for item in config.get("Env") or [])
    in_volume = all(env.get(name, "").startswith(VOLUME + "/") for name in ("HARNESS_CONFIG_DIR", "HARNESS_DATA_DIR"))
    return [
        Check("unprivileged user", config.get("User") == USER, f"User {config.get('User')!r}"),
        Check("volume", VOLUME in (config.get("Volumes") or {}) and in_volume,
              f"Volumes {sorted(config.get('Volumes') or {})}; config and data under {VOLUME}: {in_volume}"),
        Check("no published port", not config.get("ExposedPorts"), f"ExposedPorts {config.get('ExposedPorts')}"),
    ]


def fresh_image(run: Runner, image: str) -> Check:
    # As root, so no folder is unreadable to the scan.
    out = run(["run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "python", image, "-c", SECRETS])
    if out.returncode != 0:
        return Check("no secrets in the image", False, out.stderr.strip()[-300:])
    found = json.loads(out.stdout)
    return Check("no secrets in the image", found == [], f"found {found}" if found else "no token, secret or config")


def wait_healthy(run: Runner, name: str, timeout: float, sleep: Callable[[float], None]) -> Check:
    deadline = time.monotonic() + timeout
    while True:
        out = run(["exec", name, "python", "-c", HEALTH])
        if out.returncode == 0:
            body = json.loads(out.stdout)
            ok = body.get("ok") is True and body.get("profile") == "service"
            return Check("GET /health", ok, f"ok={body.get('ok')} profile={body.get('profile')} "
                                           f"release={body.get('release')}")
        if time.monotonic() > deadline:
            return Check("GET /health", False, f"no answer in {timeout:.0f} s: {out.stderr.strip()[-300:]}")
        sleep(1.0)


def running(run: Runner, name: str) -> list[Check]:
    uid = run(["exec", name, "python", "-c",
               "print([l.split()[1] for l in open('/proc/1/status') if l.startswith('Uid:')][0])"]).stdout.strip()
    listening = json.loads(run(["exec", name, "python", "-c", LISTENING]).stdout or "[]")
    files = run(["exec", name, "python", "-c",
                 "import json, os, sys; v = sys.argv[1]; print(json.dumps({p: os.stat(os.path.join(v, p)).st_uid "
                 "for p in ('config/harness.yaml', 'data/harness.sqlite3', 'data/local-owner.token') "
                 "if os.path.exists(os.path.join(v, p))}))", VOLUME]).stdout
    owned = json.loads(files or "{}")
    wanted = {"config/harness.yaml", "data/harness.sqlite3", "data/local-owner.token"}
    return [
        Check("daemon is unprivileged", uid == UID, f"PID 1 runs as uid {uid or '?'}"),
        Check("loopback only", bool(listening) and all(a.startswith(LOOPBACK) for a in listening),
              f"listening on {listening}"),
        Check("state in the volume", set(owned) == wanted and set(owned.values()) == {int(UID)},
              f"{sorted(owned)} owned by {sorted(set(owned.values()))}"),
    ]


def smoke(image: str, run: Runner = docker, timeout: float = 120.0,
          sleep: Callable[[float], None] = time.sleep) -> list[Check]:
    checks = image_config(run, image)
    if not checks[0].ok and checks[0].name == "image":
        return checks
    checks.append(fresh_image(run, image))
    name = f"ah-server-smoke-{uuid.uuid4().hex[:12]}"
    volume = f"{name}-state"
    try:
        out = run(["run", "-d", "--name", name, "--network", "none", "-v", f"{volume}:{VOLUME}", image])
        if out.returncode != 0:
            return checks + [Check("start", False, out.stderr.strip()[-300:])]
        health = wait_healthy(run, name, timeout, sleep)
        checks.append(health)
        if health.ok:
            checks += running(run, name)
        else:
            logs = run(["logs", "--tail", "40", name])
            checks.append(Check("logs", False, (logs.stdout + logs.stderr).strip()[-2000:]))
    finally:
        run(["rm", "-f", name])
        run(["volume", "rm", "-f", volume])
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="the image to check, as `docker build -t` named it")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for /health")
    args = parser.parse_args(argv)
    checks = smoke(args.image, timeout=args.timeout)
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    return 0 if checks and all(check.ok for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
