"""Fake Git Credential Manager, fake secure store, and fake Git/GitHub for issue #63 contract tests.

The broker drops every environment variable outside its allowlist, so the fakes find their state next to
their own script (one install directory per test). Nothing here touches a real store or the network.

fake GCM: `--version`, `get`, `store`, `erase` against store.json keyed by (GCM_NAMESPACE, store, host).
In connect mode (GCM_INTERACTIVE != never, GUI prompts on, GCM_GITHUB_HELPER set) it runs the configured
helper as `device --code <code> --url https://github.com/login/device`, waits for an approval file, kills the
helper, and prints the credential. Tokens it issues are recorded in issued.json for the fake GitHub.

fake git: records argv and environment, asks the configured credential helper for a github.com credential
(the way git does), checks it against the fake GitHub (issued, unrevoked, allowed for the repo), and then
performs the clone/fetch/push against a local bare repository with the real git. Other subcommands pass
through to the real git.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from waits import scaled

FAKE_GCM = r'''
import json, os, secrets, subprocess, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
STORE = HERE / "store.json"
MODE = HERE / "mode.json"

def load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default

def save(p, data):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)

def append(path, row):
    # One file per record, renamed into place: concurrent O_APPEND writes from several fake
    # processes are not atomic on Windows and could interleave (and lose) a long line.
    import time as _time
    d = path.with_name(path.name + ".d")
    d.mkdir(exist_ok=True)
    name = "%020d-%d-%s" % (_time.time_ns(), os.getpid(), os.urandom(4).hex())
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps(row), encoding="utf-8")
    os.replace(tmp, d / (name + ".json"))

def record(action):
    append(HERE / "calls.jsonl", {"argv": sys.argv[1:], "action": action, "env": dict(os.environ)})

def read_input():
    out = {}
    for line in sys.stdin.read().splitlines():
        if not line:
            break
        k, _, v = line.partition("=")
        out[k] = v
    return out

mode = load(MODE, {})
action = sys.argv[1] if len(sys.argv) > 1 else ""
if action == "--version":
    print(mode.get("version", "2.9.0+fake"))
    sys.exit(0)
record(action)
ns = os.environ.get("GCM_NAMESPACE", "git")
store = os.environ.get("GCM_CREDENTIAL_STORE", "")
if mode.get("store_broken"):
    sys.stderr.write("fatal: store unavailable\n"); sys.exit(1)
data = read_input()
host = data.get("host", "")
key = f"{ns}|{store}|{host}"
creds = load(STORE, {})
if action == "store":
    if mode.get("store_noop"):
        sys.exit(0)
    if not data.get("username") or not data.get("password"):
        sys.exit(1)
    creds[key] = {"username": data["username"], "password": data["password"]}
    save(STORE, creds)
    sys.exit(0)
if action == "erase":
    if not mode.get("erase_noop"):
        creds.pop(key, None)
        save(STORE, creds)
    sys.exit(0)
if action != "get":
    sys.exit(2)
if key in creds:
    c = creds[key]
    sys.stdout.write(f"protocol=https\nhost={host}\nusername={c['username']}\npassword={c['password']}\n")
    sys.exit(0)
if os.environ.get("GCM_INTERACTIVE", "auto").lower() in ("never", "false", "0"):
    sys.stderr.write("fatal: Cannot prompt because user interactivity has been disabled.\n"); sys.exit(1)
helper = os.environ.get("GCM_GITHUB_HELPER", "")
if os.environ.get("GCM_GUI_PROMPT", "true") == "false" or not helper or mode.get("no_desktop"):
    sys.stderr.write("fatal: Cannot prompt because the terminal prompts have been disabled.\n"); sys.exit(1)
code = mode.get("code", "WDJB-MJHT")
proc = subprocess.Popen([helper, "device", "--code", code, "--url", "https://github.com/login/device"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
safe = ns.replace("/", "_")
approve = HERE / "approvals" / safe
deny = HERE / "denials" / safe
end = time.time() + float(mode.get("gcm_timeout", 60))
while time.time() < end:
    login = approve.read_text(encoding="utf-8").strip() if approve.exists() else ""
    if login:
        token = "gho_" + secrets.token_hex(18)
        issued = load(HERE / "issued.json", {})
        issued[token] = login
        save(HERE / "issued.json", issued)
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        proc.kill()
        sys.stdout.write(f"protocol=https\nhost={host}\nusername={login}\npassword={token}\n")
        sys.exit(0)
    if deny.exists():
        proc.kill()
        sys.stderr.write("fatal: access_denied: The user has denied your request.\n"); sys.exit(1)
    if proc.poll() is not None:
        sys.stderr.write("fatal: User canceled device code authentication\n"); sys.exit(1)
    time.sleep(0.05)
proc.kill()
sys.stderr.write("fatal: device code expired\n"); sys.exit(1)
'''

FAKE_GIT = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
GH = HERE.parent / "gcm"
REAL = json.loads((HERE / "real.json").read_text(encoding="utf-8"))["git"]

def load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default

def append(path, row):
    # One file per record, renamed into place: concurrent O_APPEND writes from several fake
    # processes are not atomic on Windows and could interleave (and lose) a long line.
    import time as _time
    d = path.with_name(path.name + ".d")
    d.mkdir(exist_ok=True)
    name = "%020d-%d-%s" % (_time.time_ns(), os.getpid(), os.urandom(4).hex())
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps(row), encoding="utf-8")
    os.replace(tmp, d / (name + ".json"))

args = sys.argv[1:]
append(HERE / "calls.jsonl", {"argv": args, "env": dict(os.environ), "cwd": os.getcwd()})
configs, i, cwd = [], 0, None
while i < len(args):
    if args[i] == "-c":
        configs.append(args[i + 1]); i += 2
    elif args[i] == "-C":
        cwd = args[i + 1]; i += 2
    else:
        break
rest = args[i:]
sub = rest[0] if rest else ""
url = next((a for a in rest if a.startswith("https://")), "")
env = {k: v for k, v in os.environ.items() if k != "GIT_ALLOW_PROTOCOL"}
if sub not in ("clone", "fetch", "push") or not url:
    sys.exit(subprocess.run([REAL, *args], env=env).returncode)
helpers = [c.split("=", 1)[1] for c in configs if c.startswith("credential.helper=")]
helper = helpers[-1].removeprefix("!").strip('"') if helpers else ""
server = load(HERE / "server.json", {})
owner_repo = url.removeprefix("https://github.com/").removesuffix(".git")
repo = server.get("repos", {}).get(owner_repo)
if server.get("redirect", {}).get(owner_repo):
    sys.stderr.write(f"fatal: unable to access '{url}/': The requested URL returned error: 301\n"); sys.exit(128)
if repo is not None and repo.get("public") and sub != "push":
    login = None
else:
    if not helper:
        sys.stderr.write("fatal: could not read Username for 'https://github.com': terminal prompts disabled\n")
        sys.exit(128)
    q = "protocol=https\nhost=github.com\n\n"
    r = subprocess.run([helper, "get"], input=q, capture_output=True, text=True, env=os.environ)
    if r.returncode != 0:
        sys.stderr.write("fatal: could not read Username for 'https://github.com': terminal prompts disabled\n")
        sys.exit(128)
    cred = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    issued = load(GH / "issued.json", {})
    revoked = set(server.get("revoked", []))
    token = cred.get("password", "")
    if token not in issued or token in revoked:
        subprocess.run([helper, "erase"], input=q + "", capture_output=True, text=True, env=os.environ)
        sys.stderr.write(f"remote: Invalid username or password.\nfatal: Authentication failed for '{url}/'\n")
        sys.exit(128)
    login = issued[token]
    append(HERE / "auth.jsonl", {"sub": sub, "repo": owner_repo, "login": login})
if repo is None or (login is not None and login not in repo.get("allowed", [])) or (login is None and not repo.get("public")):
    sys.stderr.write(f"remote: Repository not found.\nfatal: repository '{url}/' not found\n"); sys.exit(128)
local = repo["path"]
real_args = [REAL, "-c", "protocol.file.allow=always"]
if cwd:
    real_args += ["-C", cwd]
mapped = [local if a == url else a for a in rest]
mapped = [a for a in mapped if not a.startswith("--template=")]
r = subprocess.run([*real_args, *mapped], env=env, capture_output=True, text=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr); sys.exit(r.returncode)
if sub == "clone":
    dest = rest[-1]
    subprocess.run([REAL, "-C", dest, "remote", "set-url", "origin", url], env=env, check=True)
sys.exit(0)
'''


def _launcher(directory: Path, name: str, script: Path) -> Path:
    py = Path(sys.executable).resolve()
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text(f'@"{py}" -I "{script}" %*\r\n', encoding="utf-8")
    else:
        path = directory / name
        path.write_text(f'#!/bin/sh\nexec "{py}" -I "{script}" "$@"\n', encoding="utf-8")
        os.chmod(path, 0o755)
    return path


class FakeGitHub:
    """One install of the fakes. `root` must be outside the daemon's data_dir (the broker refuses those)."""

    def __init__(self, root: Path):
        self.root = root
        self.gcm_dir = root / "gcm"
        self.git_dir = root / "git"
        for d in (self.gcm_dir, self.git_dir, self.gcm_dir / "approvals", self.gcm_dir / "denials"):
            d.mkdir(parents=True, exist_ok=True)
        (self.gcm_dir / "fake_gcm.py").write_text(FAKE_GCM, encoding="utf-8")
        (self.git_dir / "fake_git.py").write_text(FAKE_GIT, encoding="utf-8")
        (self.git_dir / "real.json").write_text(json.dumps({"git": shutil.which("git")}), encoding="utf-8")
        self.gcm = _launcher(self.gcm_dir, "git-credential-manager", self.gcm_dir / "fake_gcm.py")
        self.git = _launcher(self.git_dir, "git", self.git_dir / "fake_git.py")
        self.server = {"repos": {}, "revoked": [], "redirect": {}}
        self._save_server()

    # --- configuration ---------------------------------------------------------------------------------
    def configure(self, cfg, store: str | None = None) -> None:
        from harness import github_auth as ga
        cfg.github_member_auth.gcm_path = str(self.gcm)
        cfg.github_member_auth.git_path = str(self.git)
        cfg.github_member_auth.credential_store = (store if store is not None
                                                   else ga.STORES_BY_PLATFORM[ga.platform_key()][0])

    def mode(self, **flags) -> None:
        (self.gcm_dir / "mode.json").write_text(json.dumps(flags), encoding="utf-8")

    def _save_server(self) -> None:
        (self.git_dir / "server.json").write_text(json.dumps(self.server), encoding="utf-8")

    # --- fake GitHub ------------------------------------------------------------------------------------
    def add_repo(self, owner_repo: str, *, allowed=(), public: bool = False, files=None) -> Path:
        git = shutil.which("git")
        work = self.root / "src" / owner_repo.replace("/", "__")
        bare = self.root / "remote" / (owner_repo.replace("/", "__") + ".git")
        work.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        subprocess.run([git, "init", "-q", "-b", "main", str(work)], check=True, env=env)
        for name, text in (files or {"README.md": "private\n"}).items():
            (work / name).write_text(text, encoding="utf-8")
        subprocess.run([git, "-C", str(work), "add", "-A"], check=True, env=env)
        subprocess.run([git, "-C", str(work), "commit", "-qm", "init"], check=True, env=env)
        subprocess.run([git, "clone", "-q", "--bare", str(work), str(bare)], check=True, env=env)
        self.server["repos"][owner_repo] = {"path": str(bare), "allowed": list(allowed), "public": public}
        self._save_server()
        return bare

    def commit_upstream(self, owner_repo: str, name: str, text: str) -> None:
        git = shutil.which("git")
        work = self.root / "src" / owner_repo.replace("/", "__")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        (work / name).write_text(text, encoding="utf-8")
        subprocess.run([git, "-C", str(work), "add", "-A"], check=True, env=env)
        subprocess.run([git, "-C", str(work), "commit", "-qm", f"add {name}"], check=True, env=env)
        bare = self.server["repos"][owner_repo]["path"]
        subprocess.run([git, "-C", str(work), "push", "-q", bare, "main"], check=True, env=env)

    def revoke_all(self) -> None:
        self.server["revoked"] = list(self.issued())
        self._save_server()

    def redirect(self, owner_repo: str) -> None:
        self.server["redirect"][owner_repo] = True
        self._save_server()

    def issued(self) -> dict:
        try:
            return json.loads((self.gcm_dir / "issued.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # --- device flow --------------------------------------------------------------------------------
    def approve(self, user_id: str, login: str = "octocat") -> None:
        self._publish(self.gcm_dir / "approvals" / f"agent-harness_v1_{user_id}", login)

    def deny(self, user_id: str) -> None:
        self._publish(self.gcm_dir / "denials" / f"agent-harness_v1_{user_id}", "x")

    @staticmethod
    def _publish(path: Path, text: str) -> None:
        # Atomic: the fake GCM polls for the file and must never see it empty.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # --- inspection -------------------------------------------------------------------------------------
    def store(self) -> dict:
        try:
            return json.loads((self.gcm_dir / "store.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def namespaces(self) -> set[str]:
        return {k.split("|")[0] for k in self.store()}

    def wipe_store(self) -> None:
        (self.gcm_dir / "store.json").write_text("{}", encoding="utf-8")

    def gcm_calls(self) -> list[dict]:
        return _lines(self.gcm_dir / "calls.jsonl")

    def git_calls(self) -> list[dict]:
        return _lines(self.git_dir / "calls.jsonl")

    def auth_log(self) -> list[dict]:
        return _lines(self.git_dir / "auth.jsonl")


def _lines(path: Path) -> list[dict]:
    """Records the fakes wrote for `path` (one renamed-into-place file each), oldest first."""
    d = path.with_name(path.name + ".d")
    try:
        names = sorted(p for p in d.iterdir() if p.suffix == ".json")
    except OSError:
        return []
    rows = []
    for p in names:
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return rows


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.05, describe=None):
    """Poll until `predicate` is truthy; the deadline scales on slow runners (issue #312).

    On timeout, `describe()` (when given) is printed to stderr so the failure shows the last observed state."""
    end = time.monotonic() + scaled(timeout)
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    value = predicate()
    if not value and describe is not None:
        print(f"wait_for timed out; last observed: {describe()!r}", file=sys.stderr)
    return value
