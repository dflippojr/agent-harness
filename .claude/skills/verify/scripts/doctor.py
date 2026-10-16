"""Doctor: is this disposable fake-model instance ready, loopback-only, isolated and seeded?

    python .claude/skills/verify/scripts/doctor.py --dir <instance-dir>

Prints one PASS or FAIL line per check and exits 1 if any check fails. It reads the instance's own handle
(`instance.json`), the Server and the separately hosted Web on their loopback URLs, and the recorded process; it
never starts, stops or changes anything. The seed check reads the API as the owner, with the token in
HARNESS_UI_OWNER_TOKEN (the instance prints it at start; verify.py passes it in-process). See docs/ui-testing.md,
"Scope and isolation" and "Seed and sign-in", for what each check expects.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from harness import fake_instance, fake_model  # noqa: E402 - the seed and the marker come from the instance itself

LOOPBACK = "127.0.0.1"
LIVE_PORTS = {8100}  # docs/ui-testing.md, "Scope and isolation": never point a test at the live harness
TOKEN_ENV = "HARNESS_UI_OWNER_TOKEN"
CLIENT = {"X-Agent-Harness-Client": "cli/2"}  # docs/ui-testing.md, "Seed and sign-in": API checks
# Variable names that carry credentials. verify.py keeps them from the instance; the instance needs none.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|AUTH|CREDENTIAL|PRIVATE_KEY|NTFY", re.I)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'} {self.name}: {self.detail}"


def _host_port(url: str) -> tuple[str | None, int | None]:
    parts = urlsplit(url or "")
    try:
        return parts.hostname, parts.port
    except ValueError:
        return parts.hostname, None


def url_problem(handle: dict) -> str | None:
    """Why the handle's URLs are unsafe to contact, or None: both must be 127.0.0.1 on a port that is not live."""
    for label in ("server", "web"):
        host, port = _host_port(handle.get(label, ""))
        if host != LOOPBACK or port is None:
            return f"{label} URL {handle.get(label)!r} is not http://{LOOPBACK}:<port>"
        if port in LIVE_PORTS:
            return f"{label} uses live port {port}"
    return None


def check_ready(handle: dict, http: httpx.Client) -> Check:
    """The handle says ready (the contract's readiness signal), the Server's /health is ok and Web's /ready
    answers 200. Unsafe URLs are refused before any request."""
    if problem := url_problem(handle):
        return Check("ready", False, f"not contacted: {problem}")
    if handle.get("status") != "ready":
        return Check("ready", False, f"instance.json status is {handle.get('status')!r}, not 'ready'")
    for label, url in (("Server", f"{handle['server']}/health"), ("Web", f"{handle['web']}/ready")):
        try:
            reply = http.get(url)
        except httpx.HTTPError as e:
            return Check("ready", False, f"{label} {url} did not answer ({type(e).__name__})")
        if reply.status_code != 200:
            return Check("ready", False, f"{label} {url} answered HTTP {reply.status_code}")
        if label == "Server" and not _json(reply).get("ok"):
            return Check("ready", False, f"{url} did not report ok")
    return Check("ready", True, "instance.json is ready; Server /health is ok and Web /ready answers 200")


def _json(reply: httpx.Response):
    try:
        return reply.json()
    except ValueError:
        return {}


def check_loopback(handle: dict, listeners: Iterable[tuple[str, int]]) -> Check:
    """Both URLs are 127.0.0.1 on non-live ports, and every socket the instance listens on is bound to 127.0.0.1.
    `listeners` is (ip, port) for each listening socket of the recorded process and its children."""
    if problem := url_problem(handle):
        return Check("loopback", False, problem)
    ports = [_host_port(handle[label])[1] for label in ("server", "web")]
    bound = list(listeners)
    exposed = sorted({f"{ip}:{port}" for ip, port in bound if ip != LOOPBACK})
    if exposed:
        return Check("loopback", False, f"the instance listens beyond loopback: {', '.join(exposed)}")
    missing = [str(p) for p in ports if p not in {port for _, port in bound}]
    if missing:
        return Check("loopback", False, f"the recorded process does not listen on {', '.join(missing)}")
    return Check("loopback", True, f"Server :{ports[0]} and Web :{ports[1]} listen on {LOOPBACK} only")


def _inside(path: str, root: Path) -> bool:
    try:
        return bool(path) and Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def check_isolation(root: Path, env: dict | None) -> Check:
    """The data lives only in the instance directory: the fake-model marker is there, the generated config's
    data_dir and repos_dir are inside it, and it is not this checkout (whose config/ is the real one). `env` is the
    instance process's environment, None if it could not be inspected; it must carry no credential variables."""
    root = root.resolve()
    problems = []
    if root == REPO or REPO.is_relative_to(root) or root.is_relative_to(REPO / "config"):
        problems.append(f"{root} is this checkout or its real config")
    if not (root / "data" / fake_model.MARKER).is_file():
        problems.append(f"the {fake_model.MARKER} marker is missing from data/")
    try:
        config = yaml.safe_load((root / "config" / "harness.yaml").read_text(encoding="utf-8")) or {}
        problems += [f"{key} {config.get(key)!r} is outside the instance" for key in ("data_dir", "repos_dir")
                     if not _inside(config.get(key, ""), root)]
    except (OSError, yaml.YAMLError, AttributeError):
        problems.append("the instance's config/harness.yaml could not be read")
    if env is None:
        problems.append("the instance process could not be inspected")
    else:
        leaked = sorted(k for k, v in env.items() if v and SECRET_NAME.search(k))
        if leaked:
            problems.append(f"the instance has credential variables {', '.join(leaked)}")
    if problems:
        return Check("isolation", False, "; ".join(problems))
    return Check("isolation", True, f"marker present; data_dir and repos_dir inside {root}; no credential variables")


def check_seed(http: httpx.Client) -> Check:
    """Every seed row from docs/ui-testing.md, "Seed and sign-in", with its settled status. `http` is an owner
    client on the Server."""
    absent = []
    sessions = {s.get("title"): s.get("status") for s in _list(http, "/api/v1/sessions")}
    for title, _, status in fake_instance.SEED_SESSIONS:
        if sessions.get(title) != status:
            absent.append(f"session {title!r} ({sessions.get(title) or 'missing'}, want {status})")
    chats = {c.get("title") for c in _list(http, "/api/admin/v1/chats")}
    absent += [f"chat {prompt!r}" for prompt in fake_instance.SEED_CHATS if prompt not in chats]
    jobs = {j.get("name") for j in _list(http, "/api/admin/v1/jobs")}
    absent += [f"job {job['name']!r}" for job in fake_instance.SEED_JOBS if job["name"] not in jobs]
    if absent:
        return Check("seed", False, "missing synthetic seed: " + "; ".join(absent))
    return Check("seed", True, f"{len(fake_instance.SEED_SESSIONS)} sessions, {len(fake_instance.SEED_CHATS)} chat "
                               f"and {len(fake_instance.SEED_JOBS)} jobs as seeded")


def _list(http: httpx.Client, path: str) -> list[dict]:
    reply = http.get(path)
    body = _json(reply) if reply.status_code == 200 else []
    return [row for row in body if isinstance(row, dict)] if isinstance(body, list) else []


# Live inspection ---------------------------------------------------------------------------------------------------
def instance_processes(handle: dict) -> list:
    """The recorded process and its children, only while it still has its recorded start time (a reused pid is
    someone else's)."""
    import psutil
    try:
        proc = psutil.Process(int(handle["pid"]))
        if abs(proc.create_time() - float(handle["pid_created"])) > 0.01:
            return []
        return [proc, *proc.children(recursive=True)]
    except (psutil.Error, KeyError, TypeError, ValueError):
        return []


def live_listeners(procs) -> list[tuple[str, int]]:
    import psutil
    bound = []
    for proc in procs:
        try:
            bound += [(c.laddr.ip, c.laddr.port) for c in proc.net_connections(kind="inet")
                      if c.status == psutil.CONN_LISTEN]
        except psutil.Error:
            continue
    return bound


def live_environment(procs) -> dict | None:
    import psutil
    try:
        return procs[0].environ() if procs else None
    except psutil.Error:
        return None


def owner_client(server: str, token: str) -> httpx.Client:
    # trust_env=False: a configured HTTP(S)_PROXY must not carry loopback requests elsewhere.
    return httpx.Client(base_url=server, timeout=10, trust_env=False,
                        headers={"Authorization": f"Bearer {token}", **CLIENT})


def run(root: Path, token: str = "", out: Callable[[str], None] = print) -> int:
    root = root.resolve()
    try:
        handle = json.loads((root / fake_instance.HANDLE).read_text(encoding="utf-8"))
        if not isinstance(handle, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        out(f"FAIL ready: no readable {fake_instance.HANDLE} in {root} ({type(e).__name__})")
        return 1
    # Nothing is contacted unless the URLs are safe, and the seed is read only from an instance that is ready,
    # loopback-only and isolated.
    with httpx.Client(timeout=5, trust_env=False) as http:
        checks = [check_ready(handle, http)]
    procs = instance_processes(handle)
    checks.append(check_loopback(handle, live_listeners(procs)))
    checks.append(check_isolation(root, live_environment(procs)))
    if not all(c.ok for c in checks):
        checks.append(Check("seed", False, "skipped: the seed is read only when ready, loopback and isolation pass"))
    elif not token:
        checks.append(Check("seed", False, f"no owner token: set {TOKEN_ENV} to the token the instance printed"))
    else:
        with owner_client(handle["server"], token) as http:
            try:
                checks.append(check_seed(http))
            except httpx.HTTPError as e:
                checks.append(Check("seed", False, f"the Server did not answer ({type(e).__name__})"))
    for check in checks:
        out(check.line())
    return 0 if all(c.ok for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doctor.py", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--dir", type=Path, required=True, help="the instance directory given to harness.fake_instance")
    args = ap.parse_args(argv)
    return run(args.dir, os.environ.get(TOKEN_ENV, ""))


if __name__ == "__main__":
    sys.exit(main())
