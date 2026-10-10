"""Daemon installer's optional Hub lifecycle; distribution settings await #546.

The configured Hub owns PKCE and redemption. It atomically writes only its request
id and match_code to --claim-file, never a verifier, token, or host secret.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import uuid

PROMPT = "Install the Hub admin console and link it to this daemon? [Y/n] "
LATER = "Add the Hub later: rerun the daemon installer with --with-hub (PowerShell: -WithHub)."
PARITY = "Every Hub action is available from the CLI: harness --help; docs/management-parity.md."


def choose(with_hub: bool, no_hub: bool, stream=None, ask=None) -> bool:
    if with_hub and no_hub:
        raise ValueError("--with-hub and --no-hub are mutually exclusive")
    if with_hub or no_hub:
        return with_hub
    stream = sys.stdin if stream is None else stream
    if not stream.isatty():
        return False
    ask = input if ask is None else ask
    while True:
        try:
            answer = ask(PROMPT).strip().lower()
        except EOFError:
            return False
        if answer in ("", "y", "yes", "n", "no"):
            return answer in ("", "y", "yes")
        print("Please answer yes or no.")


def command(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, text=True, **kwargs)


def cli_env(config: Path, port: int) -> dict:
    # Select this install's local owner credentials, not a previously paired client.
    env = dict(os.environ, HARNESS_CONFIG_DIR=str(config), HARNESS_URL=f"http://127.0.0.1:{port}")
    env.pop("HARNESS_TOKEN", None)
    env.pop("HARNESS_LOCAL_TOKEN", None)
    return env


def hub_cli(args, env):
    # A fresh nonexistent client path avoids credentials from the owner's paired client.
    client = Path.home() / ".agent-harness" / ("installer-" + uuid.uuid4().hex + ".json")
    result = command([sys.executable, "-m", "harness.cli", "--config", client, *args], env=env, capture_output=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return json.loads(result.stdout)


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Service:
    """A dedicated per-install per-user service. Names are opaque installer ids."""

    def __init__(self, service_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", service_id):
            raise ValueError("invalid Hub service id")
        self.name = "agent-harness-hub-" + service_id

    def start(self, argv: list[str], workdir: Path, log: Path):
        if sys.platform == "win32":
            import base64
            arguments = subprocess.list2cmdline(argv[1:])
            script = (
                "$ErrorActionPreference = 'Stop'; "
                f"$a = New-ScheduledTaskAction -Execute {ps_quote(argv[0])} "
                f"-Argument {ps_quote(arguments)} -WorkingDirectory {ps_quote(str(workdir))}; "
                "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) "
                "-MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1)); "
                f"Register-ScheduledTask -TaskName {ps_quote(self.name)} -Action $a "
                "-Trigger (New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)) "
                "-Settings $s -ErrorAction Stop | Out-Null; "
                f"Start-ScheduledTask -TaskName {ps_quote(self.name)} -ErrorAction Stop"
            )
            command(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                     base64.b64encode(script.encode("utf-16le")).decode()])
        elif sys.platform == "darwin":
            path = Path.home() / "Library/LaunchAgents" / (self.name + ".plist")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps({"Label": self.name, "ProgramArguments": argv,
                "WorkingDirectory": str(workdir), "RunAtLoad": True, "KeepAlive": True,
                "StandardOutPath": str(log), "StandardErrorPath": str(log)}))
            command(["launchctl", "bootstrap", f"gui/{os.getuid()}", path])
        else:
            path = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
            path.mkdir(parents=True, exist_ok=True)
            def escape(value):
                return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n")
            executable = " ".join('"' + escape(a).replace("$", "$$") + '"' for a in argv)
            (path / (self.name + ".service")).write_text(
                "[Unit]\nDescription=Agent harness Hub\n[Service]\nType=simple\n"
                f'WorkingDirectory="{escape(workdir)}"\nExecStart={executable}\n'
                "Restart=on-failure\nRestartSec=10\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
            command(["systemctl", "--user", "daemon-reload"])
            command(["systemctl", "--user", "enable", "--now", self.name + ".service"])

    def stop(self):
        if sys.platform == "win32":
            command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"$ErrorActionPreference = 'Stop'; $t = Get-ScheduledTask -TaskName {ps_quote(self.name)} -ErrorAction SilentlyContinue; "
                "if ($t) { $t | Stop-ScheduledTask -ErrorAction Stop; "
                "$t | Unregister-ScheduledTask -Confirm:$false -ErrorAction Stop }"])
        elif sys.platform == "darwin":
            path = Path.home() / "Library/LaunchAgents" / (self.name + ".plist")
            if path.exists():
                loaded = command(["launchctl", "list"], capture_output=True).stdout
                if any(line.split()[-1:] == [self.name] for line in loaded.splitlines()):
                    command(["launchctl", "bootout", f"gui/{os.getuid()}", path])
                path.unlink()
        else:
            path = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user" / (self.name + ".service")
            if path.exists():
                command(["systemctl", "--user", "disable", "--now", self.name + ".service"])
                path.unlink()
                command(["systemctl", "--user", "daemon-reload"])


def wait_for_claim(path: Path, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            # The Hub must publish atomically; reject incomplete or unexpected data.
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError("invalid Hub claim file")
            claim = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(claim, dict) or set(claim) != {"id", "match_code"}
                    or not isinstance(claim["id"], str) or not re.fullmatch(r"pr-[A-Za-z0-9_-]+", claim["id"])
                    or not isinstance(claim["match_code"], str) or not re.fullmatch(r"[0-9]{6}", claim["match_code"])):
                raise ValueError("Hub claim file must contain only id and a six-digit match_code")
            return claim
        time.sleep(0.5)
    raise TimeoutError("Hub did not publish its claim; check the Hub service and harness hub status")


def distribution_choice(args) -> tuple[str, str]:
    method = args.hub_method
    if method == "auto":
        method = "pip"
        if sys.stdin.isatty() and shutil.which("docker") and args.hub_image:
            method = "docker" if input("Hub distribution: pip or docker? [pip] ").strip().lower() == "docker" else "pip"
    distribution = args.hub_package if method == "pip" else args.hub_image
    setting = "HARNESS_HUB_PACKAGE" if method == "pip" else "HARNESS_HUB_IMAGE"
    if not distribution or distribution.startswith("-"):
        raise ValueError(f"Set {setting} to the released Hub distribution (#546) before installing it")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", args.hub_module):
        raise ValueError("invalid HARNESS_HUB_MODULE")
    return method, distribution


def start_hub(args, method, distribution, service_id, hub_dir, claim_file):
    launch = ["--daemon-url", f"http://127.0.0.1:{args.port}", "--claim-file"]
    if method == "docker":
        if not shutil.which("docker"):
            raise ValueError("Docker is required for --hub-method docker")
        identity = ["--user", f"{os.getuid()}:{os.getgid()}"] if sys.platform != "win32" else []
        command(["docker", "run", "-d", "--restart", "unless-stopped", *identity, "--name", Service(service_id).name,
                 "--network", "host", "--mount", f"type=bind,src={hub_dir},dst=/hub-state",
                 distribution, *launch, "/hub-state/claim.json", "--state-dir", "/hub-state"])
    else:
        venv = hub_dir / "venv"
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        command([args.uv, "venv", "--python", sys.executable, venv])
        command([args.uv, "pip", "install", "--python", python, distribution])
        Service(service_id).start([str(python), "-m", args.hub_module, *launch, str(claim_file),
                                 "--state-dir", str(hub_dir)], hub_dir, hub_dir / "hub.log")


def approve_and_wait(claim_file, env):
    claim = wait_for_claim(claim_file)
    print(f"Hub request: {claim['id']}; match code: {claim['match_code']}", flush=True)
    print(f"harness hub approve {claim['id']} --match {claim['match_code']}", flush=True)
    hub_cli(["hub", "approve", claim["id"], "--match", claim["match_code"]], env)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = hub_cli(["hub", "status"], env)
        if status["claimed"]:
            if status["hub"]["request_id"] != claim["id"]:
                raise ValueError("Another Hub claimed the daemon; use harness hub status")
            claim_file.unlink(missing_ok=True)
            print("Hub installed and linked.")
            return
        time.sleep(1)
    raise TimeoutError("Hub did not redeem its approved claim; check harness hub status")


def install(args) -> int:
    if not choose(args.with_hub, args.no_hub):
        print(PARITY)
        print(LATER)
        return 0
    if args.no_start:
        print("Hub setup deferred: startup services are disabled. Start the daemon and rerun without --no-start / -NoTasks.")
        print(LATER)
        return 0
    env = cli_env(args.config_dir, args.port)
    if hub_cli(["hub", "status"], env)["claimed"]:
        print("A Hub is already claimed. Release it explicitly with harness hub release --confirm.")
        return 0
    state = args.install_dir / "hub-install.json"
    if state.exists():
        raise ValueError("This install already has a Hub service; remove it with harness.install_hub uninstall first")
    method, distribution = distribution_choice(args)
    service_id = uuid.uuid4().hex
    hub_dir = args.install_dir / ("hub-" + service_id)
    hub_dir.mkdir(mode=0o700)
    claim_file = hub_dir / "claim.json"
    # Record ownership before starting: a failed/partial install remains removable.
    state.write_text(json.dumps({"id": service_id, "method": method, "port": args.port}) + "\n", encoding="utf-8")
    start_hub(args, method, distribution, service_id, hub_dir, claim_file)
    approve_and_wait(claim_file, env)
    return 0


def uninstall(args) -> int:
    state = args.install_dir / "hub-install.json"
    record = json.loads(state.read_text(encoding="utf-8")) if state.exists() else None
    from .config import load
    port = record["port"] if record else load(args.config_dir).port
    if record:
        service = Service(record["id"])
        if record["method"] not in ("pip", "docker"):
            raise ValueError("invalid Hub install record")
    env = cli_env(args.config_dir, port)
    status = hub_cli(["hub", "status"], env)
    if status["claimed"]:
        print("harness hub release --confirm (before removing the daemon)")
        hub_cli(["hub", "release", "--confirm"], env)
    if record:
        if record["method"] == "docker":
            containers = command(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True).stdout
            if service.name in containers.splitlines():
                command(["docker", "rm", "-f", service.name])
        elif record["method"] == "pip":
            service.stop()
        directory = args.install_dir / ("hub-" + record["id"])
        if directory.is_symlink():
            raise ValueError("refusing to remove a linked Hub directory")
        if directory.exists():
            shutil.rmtree(directory)
        state.unlink()
        print("Removed the Hub installed by this daemon installer.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8100)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--with-hub", action="store_true")
    group.add_argument("--no-hub", action="store_true")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--hub-method", choices=("auto", "pip", "docker"), default="auto")
    parser.add_argument("--hub-package", default=os.environ.get("HARNESS_HUB_PACKAGE", ""))
    parser.add_argument("--hub-image", default=os.environ.get("HARNESS_HUB_IMAGE", ""))
    parser.add_argument("--hub-module", default=os.environ.get("HARNESS_HUB_MODULE", "harness_hub"))
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args(argv)
    try:
        return install(args) if args.action == "install" else uninstall(args)
    except (ValueError, OSError, TimeoutError, subprocess.CalledProcessError) as exc:
        print(f"Hub setup failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        print(LATER, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
