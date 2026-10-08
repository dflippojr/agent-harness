"""Diagnose an install: python -m harness.doctor [--config-dir DIR] [--instance NAME]

Read-only. Prints OK / WARN / FAIL per check and exits 1 if anything failed. Generic counterpart of the tower's
ops/check-stack.ps1 (a `doctor` command as in Hermes Agent, docs/phase6a-hermes-study.md).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


class Report:
    def __init__(self):
        self.failed = 0
        self.warned = 0

    def ok(self, name: str, detail: str = "") -> None:
        print(f"{GREEN}[ OK ]{RESET} {name}  {detail}")

    def warn(self, name: str, detail: str) -> None:
        self.warned += 1
        print(f"{YELLOW}[WARN]{RESET} {name}  {detail}")

    def fail(self, name: str, detail: str) -> None:
        self.failed += 1
        print(f"{RED}[FAIL]{RESET} {name}  {detail}")


def run(args: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def check_data_dir(r: Report, cfg) -> None:
    try:
        data = cfg.data_dir
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        free = shutil.disk_usage(data).free / 2**30
        (r.ok if free >= cfg.cleanup.min_free_gb else r.warn)(
            "Data directory", f"{data} writable, {free:.0f} GB free (sessions need {cfg.cleanup.min_free_gb})")
    except OSError as e:
        r.fail("Data directory", str(e))


SCHEMA_CHECK = "Database schema"


def check_schema_version(r: Report, cfg) -> None:
    """Compare the database's `user_version` with the migrations this code ships. Read-only; never migrates."""
    import sqlite3

    from . import migrations

    path = Path(cfg.db_path)
    if not path.is_file():
        r.ok(SCHEMA_CHECK, "skipped: no database yet (created on first start)")
        return
    try:
        steps = migrations.discover()
    except Exception as e:  # a broken NNNN_*.py can raise anything at import; report it, don't abort the doctor run
        r.fail(SCHEMA_CHECK, f"could not load migrations: {type(e).__name__}: {e}")
        return
    try:
        latest = migrations.latest_version(steps)
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            current = migrations.user_version(conn)
        finally:
            conn.close()
    except (sqlite3.Error, migrations.MigrationError) as e:
        r.fail(SCHEMA_CHECK, f"{path}: {e}")
        return
    if current > latest:
        r.fail(SCHEMA_CHECK, migrations.too_new_message(current, latest))
    elif current < latest:
        r.warn(SCHEMA_CHECK, f"v{current}, will migrate to v{latest} on the next daemon start")
    else:
        r.ok(SCHEMA_CHECK, f"v{current} (current)")


def check_github_token(r: Report, cfg) -> None:
    """Never print the token or its path."""
    if not cfg.github.token_file:
        r.ok("GitHub task token", "not configured")
        return
    from .github_tasks import token
    try:
        token(cfg)
    except Exception:
        r.warn("GitHub task token", "configured but unreadable or insecure")
    else:
        r.ok("GitHub task token", "configured and readable")


def check_claude_token(r: Report, cfg) -> None:
    """The owner's `claude setup-token` token (#390): presence and expiry only, never the value or its path."""
    from . import claude_token
    claude = cfg.backends.get("claude")
    path = getattr(claude, "oauth_token_file", "") if claude is not None else ""
    if not path:
        return
    if not claude_token.read_token(path):
        r.warn("Claude token", "configured but missing or empty; run ops/backends/login.ps1 claude -Token")
        return
    end = claude_token.expiry(path)
    note = claude_token.reminder(path)
    if end is None:
        r.warn("Claude token", "present, but its expiry date is unknown (no .expires file)")
    elif note:
        r.warn("Claude token", note)
    else:
        r.ok("Claude token", f"present, expires {end}")


def check_provider_containers(r: Report, cfg) -> None:
    """Provider CLI images and their egress proxies; service profile only."""
    for image in sorted({backend.image for backend in cfg.backends.values() if backend.enabled}):
        code, _ = run(["docker", "image", "inspect", image])
        (r.ok if code == 0 else r.fail)("Provider CLI image", image if code == 0 else f"{image} missing")
    for name, backend in cfg.backends.items():
        if not backend.enabled:
            continue
        container = f"harness-egress-{name}"
        code, status = run(["docker", "inspect", "--format", "{{.State.Status}}", container])
        (r.ok if code == 0 and status == "running" else r.fail)(
            "Provider egress", f"{name}: {status}" if code == 0 else f"{container} missing")


def check_docker(r: Report, cfg) -> None:
    code, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        r.fail("Docker", "engine not reachable: install and start Docker Engine or Docker Desktop")
        return
    r.ok("Docker", f"engine {out}")
    code, _ = run(["docker", "image", "inspect", cfg.sandbox.image])
    if code == 0:
        r.ok("Sandbox image", cfg.sandbox.image)
    else:
        r.fail("Sandbox image", f"{cfg.sandbox.image} missing: docker build -t {cfg.sandbox.image} sandbox")
    from .snippets import LANGUAGES
    missing = [lang.tag for lang in LANGUAGES.values() if run(["docker", "image", "inspect", lang.image])[0] != 0]
    if missing:
        r.warn("Chat snippet runner", f"pinned toolchain images missing ({', '.join(missing)}); Run fails for those "
                                      "languages until you run: python -m harness.snippets pull")
    else:
        r.ok("Chat snippet runner", ", ".join(lang.tag for lang in LANGUAGES.values()))
    if cfg.profile == "service":
        check_provider_containers(r, cfg)


def check_daemon_profile(r: Report, cfg, base: str) -> None:
    """Health, plus model state or provider logins - whichever the profile runs."""
    health = httpx.get(f"{base}/health", timeout=5).json()
    if health.get("profile") != cfg.profile:
        raise ValueError(f"daemon reports profile {health.get('profile')!r}, expected {cfg.profile!r}")
    if cfg.module_effective("local_model"):
        state = httpx.get(f"{base}/models/status", timeout=10).json()[0]["state"]
        r.ok("Daemon", f"{base} up; model state {state}")
        return
    r.ok("Daemon", f"{base} up; {cfg.profile} profile")
    if cfg.modules.local_model:
        return  # The core local backend can use a server supervised outside the harness.
    backends = httpx.get(f"{base}/backends", timeout=100).json()
    logged_in = [backend["name"] for backend in backends if backend.get("logged_in")]
    (r.ok if logged_in else r.warn)(
        "Provider login", ", ".join(logged_in) if logged_in else
        "none detected; run ops/backends/login.sh (Unix) or ops\\backends\\login.ps1 (Windows)")


def check_daemon(r: Report, cfg) -> None:
    base = f"http://127.0.0.1:{cfg.port}"
    try:
        check_daemon_profile(r, cfg, base)
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        r.fail("Daemon", f"{base} not answering ({type(e).__name__}); see {cfg.data_dir / 'logs'}")


def _autostart_windows(r: Report, args, with_server: bool) -> None:
    for suffix in (("LlamaServer", "Daemon") if with_server else ("Daemon",)):
        task = f"AgentHarness-{args.instance}-{suffix}"
        code, out = run(["schtasks", "/Query", "/TN", task, "/FO", "CSV", "/NH"])
        if code == 0:
            r.ok("Autostart", f"{task}: {out.split(',')[-1].strip(chr(34))}")
        else:
            r.warn("Autostart", f"{task} not registered (run install.ps1 without -NoTasks)")


def _autostart_linux(r: Report, args, with_server: bool) -> None:
    for suffix in (("llama", "daemon") if with_server else ("daemon",)):
        unit = f"agent-harness-{args.instance.lower()}-{suffix}.service"
        code, status = run(["systemctl", "--user", "is-active", unit])
        (r.ok if code == 0 and status == "active" else r.warn)(
            "Autostart", f"{unit}: {status or 'not active'}")


def _autostart_macos(r: Report, args) -> None:
    label = f"com.agent-harness.{args.instance.lower()}.daemon"
    code, _ = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    (r.ok if code == 0 else r.warn)("Autostart", f"{label}: " + ("loaded" if code == 0 else "not loaded"))


def check_autostart(r: Report, cfg, args) -> None:
    """Scheduled task / systemd unit / launchd agent, whichever the platform installs."""
    if not args.instance:
        return
    with_server = not (args.existing_server or not cfg.modules.local_model)
    if sys.platform == "win32":
        _autostart_windows(r, args, with_server)
    elif sys.platform.startswith("linux"):
        _autostart_linux(r, args, with_server)
    elif sys.platform == "darwin":
        _autostart_macos(r, args)


def check_secret_scanner(r: Report, cfg) -> None:
    """Review push/merge fail closed without the pinned gitleaks (issue #263); the daemon fetches it at start."""
    from . import secret_scan
    scanner = secret_scan.Scanner(secret_scan.tools_dir(cfg))
    problem = scanner.problem()
    if not problem:
        r.ok("Secret scanner", f"{secret_scan.SCANNER} at {scanner.binary}")
        return
    last = scanner.bootstrap_error()
    r.warn("Secret scanner", f"{problem}; Review push and merge are blocked until it is fixed "
                             f"(python -m harness.secret_scan install)" + (f". Last fetch: {last}" if last else ""))


def check_optional(r: Report, cfg) -> None:
    if cfg.web.enabled:
        try:
            n = len(httpx.get(f"{cfg.web.searxng_url}/search", params={"q": "test", "format": "json"},
                              timeout=15).json().get("results", []))
            r.ok("Web search", f"SearXNG answered with {n} results")
        except (httpx.HTTPError, ValueError) as e:
            r.fail("Web search", f"SearXNG at {cfg.web.searxng_url} not answering ({type(e).__name__})")
    from .modules import present
    for module in present(cfg):  # add-on modules' own checks (harness/modules.py)
        if module.doctor is not None:
            module.doctor(r, cfg)
    code, out = run(["tailscale", "serve", "status", "--json"])
    if code == 0 and str(cfg.port) in out:
        r.ok("Phone access", "tailscale serve publishes the daemon")
    else:
        r.warn("Phone access", "not published on a tailnet (optional; see docs/INSTALL.md)")


def check_canary(r: Report, cfg) -> None:
    reason = getattr(getattr(cfg, "canary", None), "disabled_reason", "")
    if reason:
        r.fail("Canary", f"disabled: {reason}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose an agent-harness install")
    ap.add_argument("--config-dir")
    ap.add_argument("--instance", default="", help="scheduled task name prefix used by install.ps1, e.g. Main")
    ap.add_argument("--existing-server", action="store_true", help="the install uses a model server it doesn't run")
    args = ap.parse_args(argv)
    r = Report()

    from . import config as config_mod
    try:
        cfg = config_mod.load(args.config_dir)
        mode = f"model {cfg.default_model}" if cfg.modules.local_model else "hosted providers only"
        r.ok("Config", f"{args.config_dir or config_mod.ROOT / 'config'}; {cfg.profile} profile, {mode}, port {cfg.port}")
    except Exception as e:  # noqa: BLE001 - report any config problem
        r.fail("Config", f"{type(e).__name__}: {e}")
        return 1

    check_data_dir(r, cfg)
    check_schema_version(r, cfg)
    check_github_token(r, cfg)
    check_claude_token(r, cfg)
    check_canary(r, cfg)
    check_docker(r, cfg)
    check_daemon(r, cfg)
    check_autostart(r, cfg, args)
    check_secret_scanner(r, cfg)
    check_optional(r, cfg)

    print(f"\n{r.failed} failed, {r.warned} warnings")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
