"""Issue #806: the Linux GitHub Actions runner installer (ops/github/install-runner.sh).
Issue #814: how its units, and the documented deploy runner unit, stop, and the runner health check beside them.

Most cases are dry runs. The install cases run the real install path without root: a synthetic filesystem root
stands in for / (HARNESS_INSTALLER_FAKE_ROOT), and fake runuser, curl and systemctl on PATH record every call. The
fake runuser locks root's private temp tree for as long as a service-user command runs, so a path from root's side
that the service user could not open fails here too.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.installer_support import BASH, ROOT

SCRIPT = ROOT / "ops" / "github" / "install-runner.sh"
WINDOWS_INSTALLER = ROOT / "ops" / "github" / "install-runner.ps1"
# GitHub's published SHA-256 of actions-runner-linux-x64-2.337.0.tar.gz (the release notes and the asset digest).
LINUX_X64_SHA256 = "70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613"


def run_install(
    *args: str, token: str = "t", os_name: str = "Linux", arch: str = "x86_64",
) -> subprocess.CompletedProcess[str]:
    if not BASH:
        pytest.skip("bash is not installed")
    env = {**os.environ, "HARNESS_INSTALLER_OS": os_name, "HARNESS_INSTALLER_ARCH": arch}
    return subprocess.run(
        [BASH, "ops/github/install-runner.sh", *args],
        cwd=ROOT, env=env, input=f"{token}\n", capture_output=True, text=True, timeout=30, check=False,
    )


def test_script_has_lf_and_parses_with_bash():
    assert b"\r\n" not in SCRIPT.read_bytes()
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    if BASH:
        parsed = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        assert parsed.returncode == 0, parsed.stderr


def test_dry_run_plans_a_review_runner_as_a_systemd_service_under_a_dedicated_user():
    result = run_install("--name", "tower-review-1", "--dry-run", token="synthetic-token")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "repository: dflippojr/agent-harness" in out
    assert "runner: tower-review-1 (labels: agent-harness-review;" in out
    assert "service user: github-runner" in out
    assert "install directory: /home/github-runner/runners/tower-review-1 (work: _work)" in out
    assert f"actions-runner-linux-x64-2.337.0.tar.gz, sha256 {LINUX_X64_SHA256}" in out
    assert "would run config.sh --unattended as github-runner with --labels agent-harness-review" in out
    assert "svc.sh install github-runner" in out
    # The token is used once and never printed.
    assert "synthetic-token" not in out + result.stderr


def test_dry_run_takes_a_caller_repository_label_and_install_directory():
    result = run_install(
        "--repo", "dflippojr/box-office", "--labels", "box-office-review", "--name", "tower-box-office",
        "--user", "review-runner", "--install-dir", "/home/review-runner/box-office", "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert "repository: dflippojr/box-office" in result.stdout
    assert "--labels box-office-review" in result.stdout
    assert "install directory: /home/review-runner/box-office" in result.stdout
    assert "would create user review-runner if missing" in result.stdout


def test_the_token_comes_on_stdin_and_is_refused_as_an_argument():
    # Arguments show in the process table and sudo's log; the token never goes there.
    missing = run_install("--dry-run", token="")
    assert missing.returncode != 0
    assert "a registration token is required on standard input" in missing.stderr
    argv = run_install("--token", "synthetic-token", "--dry-run")
    assert argv.returncode != 0
    assert "--token is not accepted: pass the registration token on standard input" in argv.stderr
    assert "synthetic-token" not in argv.stdout + argv.stderr
    assert "dry run:" not in missing.stdout + argv.stdout


def test_linux_and_windows_installers_pin_the_same_runner_release():
    windows = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    linux = SCRIPT.read_text(encoding="utf-8")
    windows_version = re.search(r"\[string\]\$Version = '([0-9.]+)'", windows).group(1)
    assert f'VERSION="{windows_version}"' in linux
    assert f'SHA256="{LINUX_X64_SHA256}"' in linux


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--user", "root"), "must not run as root"),
        # The review's publication scan rejects home-directory paths, so a runner outside /home could leak its path.
        (("--install-dir", "/opt/actions-runner"), "must be under /home"),
        (("--install-dir", "/home/github-runner/../etc"), "invalid --install-dir"),
        (("--labels", "review;rm -rf /"), "invalid --labels"),
        (("--repo", "not a repo"), "invalid --repo"),
        (("--name", "has space"), "invalid --name"),
        (("--work", ".."), "invalid --work"),
        (("--version", "2.400.0"), "needs the matching --sha256"),
        (("--sha256", "abc"), "invalid --sha256"),
        (("--bogus",), "unknown argument: --bogus"),
    ],
)
def test_invalid_arguments_are_refused_before_anything_changes(args, message):
    result = run_install(*args, "--dry-run")
    assert result.returncode != 0
    assert message in result.stderr
    assert "dry run:" not in result.stdout


@pytest.mark.parametrize(("os_name", "arch", "message"), [
    ("Darwin", "x86_64", "on Windows use ops/github/install-runner.ps1"),
    ("Linux", "aarch64", "only x86_64"),
])
def test_other_platforms_are_refused(os_name, arch, message):
    result = run_install("--dry-run", os_name=os_name, arch=arch)
    assert result.returncode != 0
    assert message in result.stderr


def test_the_module_imports_without_pwd():
    # The windows-latest test jobs collect this module, and CPython on Windows has no pwd.
    code = "import sys; sys.modules['pwd'] = None; import tests.test_runner_install"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


FAKE_COMMANDS = {
    "runuser": """#!/bin/sh
[ "$1" = -u ] && [ "$3" = -- ] || { echo "fake runuser: unexpected $*" >&2; exit 2; }
printf 'runuser %s\\n' "$*" >> "$FAKE_LOG"
shift 3
chmod 000 "$FAKE_ROOT_TMP"
FAKE_AS=service-user "$@"
rc=$?
chmod 700 "$FAKE_ROOT_TMP"
exit $rc
""",
    "curl": """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$FAKE_LOG"
while [ "$#" -gt 0 ]; do
  case "$1" in --output) out="$2"; shift 2 ;; *) shift ;; esac
done
cp "$FAKE_ARCHIVE" "$out"
""",
    "systemctl": """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$FAKE_LOG"
""",
    "useradd": """#!/bin/sh
printf 'useradd %s\\n' "$*" >> "$FAKE_LOG"
exit 1
""",
}

# The runner archive's config.sh writes .runner and generates svc.sh, as the real one does.
FAKE_ARCHIVE_FILES = {
    "config.sh": """#!/bin/sh
printf 'config.sh as=%s %s\\n' "${FAKE_AS:-root}" "$*" >> "$FAKE_LOG"
printf '%s' "${ACTIONS_RUNNER_INPUT_TOKEN:-}" > "$FAKE_TOKEN_SEEN"
echo '{}' > .runner
cp bin/svc.sh.template svc.sh
chmod 755 svc.sh
""",
    "bin/svc.sh.template": """#!/bin/sh
printf 'svc.sh as=%s %s\\n' "${FAKE_AS:-root}" "$*" >> "$FAKE_LOG"
unit=actions.runner.dflippojr-agent-harness.tower-review-1.service
case "$1" in
  install)
    printf '[Service]\\nUser=%s\\n' "$2" > "$HARNESS_INSTALLER_FAKE_ROOT/etc/systemd/system/$unit"
    systemctl daemon-reload
    systemctl enable "$unit" ;;
  start) systemctl start "$unit" ;;
esac
""",
    "bin/runsvc.sh": "#!/bin/sh\n",
}
UNIT = "actions.runner.dflippojr-agent-harness.tower-review-1.service"


def build_archive(path: Path) -> str:
    with tarfile.open(path, "w:gz") as archive:
        for name, text in FAKE_ARCHIVE_FILES.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeHost:
    """A synthetic root filesystem, private root temp tree and recording fake commands for one install run."""

    def __init__(self, tmp_path: Path, *, as_root: bool = False):
        if not BASH:
            pytest.skip("bash is not installed")
        if not sys.platform.startswith("linux"):
            pytest.skip("the install path needs Linux (GNU stat, env -C)")
        if os.geteuid() == 0 and not as_root:
            pytest.skip("root ignores the permissions that model the service user's limits")
        self.user = getpass.getuser()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.user) or self.user == "root":
            pytest.skip(f"the current user name {self.user!r} is not a valid --user")
        self.tmp = tmp_path
        self.fsroot = tmp_path / "fsroot"
        self.home = self.fsroot / "home" / self.user
        self.home.mkdir(parents=True, mode=0o755)
        (self.fsroot / "etc" / "systemd" / "system").mkdir(parents=True)
        for directory in (self.fsroot, self.fsroot / "home"):
            directory.chmod(0o755)
        self.root_tmp = tmp_path / "root-tmp"
        self.root_tmp.mkdir(mode=0o700)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in FAKE_COMMANDS.items():
            (self.bin / name).write_text(text, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.log = tmp_path / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.token_seen = tmp_path / "token-seen"
        self.archive = tmp_path / "archive.tar.gz"
        self.sha256 = build_archive(self.archive)
        self.install_dir = f"/home/{self.user}/runners/tower-review-1"
        self.dest = self.fsroot / self.install_dir.lstrip("/")

    def fake_env(self) -> dict[str, str]:
        """The variables that point the installer at this synthetic host and its recording fake commands."""
        return {
            "HARNESS_INSTALLER_OS": "Linux",
            "HARNESS_INSTALLER_ARCH": "x86_64",
            "HARNESS_INSTALLER_FAKE_ROOT": str(self.fsroot),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(self.root_tmp),
            "FAKE_LOG": str(self.log),
            "FAKE_ROOT_TMP": str(self.root_tmp),
            "FAKE_ARCHIVE": str(self.archive),
            "FAKE_TOKEN_SEEN": str(self.token_seen),
        }

    def install(
        self, *args: str, token: str = "synthetic-token", prefix: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, **self.fake_env()}
        try:
            return subprocess.run(
                [*prefix, BASH, str(SCRIPT), "--name", "tower-review-1", "--user", self.user,
                 "--version", "9.9.9", "--sha256", self.sha256, *args],
                cwd=self.tmp, env=env, input=f"{token}\n", capture_output=True, text=True, timeout=60, check=False,
            )
        finally:
            self.root_tmp.chmod(0o700)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


def test_install_crosses_from_root_to_the_service_user_and_reaches_registration_and_service_setup(tmp_path):
    host = FakeHost(tmp_path)
    result = host.install()
    assert result.returncode == 0, result.stdout + result.stderr
    calls = host.calls()
    # The archive is downloaded into root's private temp tree, yet the service user unpacks it.
    assert (host.dest / "config.sh").is_file()
    assert stat.S_IMODE(host.root_tmp.stat().st_mode) == 0o700
    config = [line for line in calls if line.startswith("config.sh ")]
    assert len(config) == 1, calls
    assert config[0].startswith("config.sh as=service-user --unattended ")
    for expected in ("--url https://github.com/dflippojr/agent-harness", "--name tower-review-1",
                     "--labels agent-harness-review", "--work _work"):
        assert expected in config[0]
    unit = host.fsroot / "etc" / "systemd" / "system" / UNIT
    assert f"User={host.user}" in unit.read_text(encoding="utf-8").splitlines()
    assert f"systemctl enable {UNIT}" in calls
    assert f"systemctl start {UNIT}" in calls
    assert not any(line.startswith("useradd") for line in calls)


def plant_protected_directory(host: FakeHost) -> tuple[Path, tuple[int, int, int]]:
    protected = host.tmp / "protected"
    protected.mkdir(mode=0o755)
    protected.chmod(0o755)
    info = protected.stat()
    return protected, (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))


def assert_refused_before_any_change(host: FakeHost, result: subprocess.CompletedProcess[str], protected: Path,
                                     before: tuple[int, int, int], message: str) -> None:
    assert result.returncode != 0, result.stdout
    assert message in result.stderr
    info = protected.stat()
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == before
    assert list(protected.iterdir()) == []
    assert not any(line.startswith(("curl", "systemctl", "config.sh", "svc.sh")) for line in host.calls())


def test_install_refuses_a_destination_the_service_user_planted_as_a_symlink(tmp_path):
    # The existing service user can plant the next pool member's destination as a link to a protected directory.
    host = FakeHost(tmp_path)
    protected, before = plant_protected_directory(host)
    host.dest.parent.mkdir(parents=True)
    host.dest.symlink_to(protected, target_is_directory=True)
    result = host.install()
    assert_refused_before_any_change(host, result, protected, before, f"{host.install_dir} is a symlink")


def test_install_refuses_a_destination_with_a_symlinked_component(tmp_path):
    host = FakeHost(tmp_path)
    protected, before = plant_protected_directory(host)
    host.dest.parent.symlink_to(protected, target_is_directory=True)
    result = host.install()
    assert_refused_before_any_change(host, result, protected, before, f"/home/{host.user}/runners is a symlink")


def test_install_refuses_a_destination_under_a_parent_others_can_write(tmp_path):
    host = FakeHost(tmp_path)
    protected, before = plant_protected_directory(host)
    (host.fsroot / "home").chmod(0o777)
    try:
        result = host.install()
    finally:
        (host.fsroot / "home").chmod(0o755)
    message = "/home must be owned by root and writable only by root"
    assert_refused_before_any_change(host, result, protected, before, message)


def test_install_never_runs_a_file_from_the_service_users_tree_as_root(tmp_path):
    # The service user owns the install tree, so root must not create it, nor execute svc.sh or anything else in it.
    host = FakeHost(tmp_path)
    result = host.install()
    assert result.returncode == 0, result.stdout + result.stderr
    calls = host.calls()
    assert not [line for line in calls if " as=root" in line], calls
    assert f"runuser -u {host.user} -- mkdir -p -m 0750 -- {host.dest}" in calls
    unit = (host.fsroot / "etc" / "systemd" / "system" / UNIT).read_text(encoding="utf-8").splitlines()
    assert f"ExecStart={host.install_dir}/runsvc.sh" in unit
    assert f"WorkingDirectory={host.install_dir}" in unit
    assert (host.dest / "runsvc.sh").is_file()
    assert (host.dest / ".service").read_text(encoding="utf-8").strip() == UNIT


def test_install_hands_the_token_to_config_sh_without_any_command_line(tmp_path):
    host = FakeHost(tmp_path)
    token = "AABBSYNTHETICREGISTRATIONTOKEN"
    result = host.install(token=token)
    assert result.returncode == 0, result.stdout + result.stderr
    # Every recorded runuser, curl, systemctl, config.sh and svc.sh argument list, and the output.
    assert token not in host.log.read_text(encoding="utf-8")
    assert token not in result.stdout + result.stderr
    # config.sh gets it from the runner's ACTIONS_RUNNER_INPUT_TOKEN, which it then removes from its environment.
    assert host.token_seen.read_text(encoding="utf-8") == token
    assert not [line for line in host.calls() if "--token" in line]


def uid0_prefix(host: FakeHost) -> tuple[str, ...]:
    """A command prefix that runs the installer with EUID 0.

    None as real root, else a user namespace mapping us to 0. ubuntu-latest blocks unprivileged user namespaces through
    AppArmor, so in CI, where passwordless sudo works, real root runs it with only the fake host's variables.
    """
    if os.geteuid() == 0:
        return ()
    unshare = shutil.which("unshare")
    probe = [unshare, "--user", "--map-root-user", "--", "sh", "-c", 'test "$(id -u)" = 0'] if unshare else None
    if probe and subprocess.run(probe, capture_output=True, check=False).returncode == 0:
        return (unshare, "--user", "--map-root-user", "--")
    in_ci = os.environ.get("CI") == "true" or bool(os.environ.get("GITHUB_ACTIONS"))
    sudo = shutil.which("sudo")
    if in_ci and sudo and subprocess.run([sudo, "-n", "true"], capture_output=True, check=False).returncode == 0:
        return (sudo, "-n", "env", "-i", *(f"{name}={value}" for name, value in host.fake_env().items()))
    pytest.skip("neither root nor an unprivileged user namespace (unshare --map-root-user) is available")


def tree(path: Path) -> list[tuple[str, int, int, int]]:
    """Every path under path with its owner, mode and size, so a write by root shows."""
    entries = []
    for entry in sorted([path, *path.rglob("*")]):
        info = entry.lstat()
        entries.append((str(entry.relative_to(path)), info.st_uid, info.st_mode, info.st_size))
    return entries


@pytest.mark.parametrize("dry_run", [False, True])
def test_root_refuses_the_fake_root_seam_before_any_host_change(tmp_path, dry_run):
    # sudo -E or a sudoers env_keep can hand the seam to real root, whose unit write would then follow a prefix the
    # caller can repoint after the checks. At EUID 0 the script refuses it outright.
    host = FakeHost(tmp_path, as_root=True)
    import pwd  # Linux only, and FakeHost has skipped elsewhere; CPython on Windows has no pwd.

    unit = host.fsroot / "etc" / "systemd" / "system" / UNIT
    system_units = Path("/etc/systemd/system")
    before = (tree(tmp_path), sorted(user.pw_name for user in pwd.getpwall()), sorted(os.listdir(system_units)))
    result = host.install(*(("--dry-run",) if dry_run else ()), prefix=uid0_prefix(host))
    assert result.returncode != 0, result.stdout
    assert "HARNESS_INSTALLER_FAKE_ROOT is for unprivileged tests; refusing it as root" in result.stderr
    # No fake runuser, useradd, curl or systemctl ran, and nothing changed on the fake root or the real host.
    assert host.calls() == []
    assert not unit.exists()
    assert not host.dest.exists()
    assert "dry run:" not in result.stdout
    after = (tree(tmp_path), sorted(user.pw_name for user in pwd.getpwall()), sorted(os.listdir(system_units)))
    assert after == before


# Issue #814: a stop takes down the whole runner tree, and someone hears when the deploy runner goes offline.

HEALTH_SCRIPT = ROOT / "ops" / "github" / "runner-health.sh"
HEALTH_SERVICE = ROOT / "ops" / "github" / "runner-health.service"
HEALTH_TIMER = ROOT / "ops" / "github" / "runner-health.timer"
CI_CD_DOC = ROOT / "docs/CI-CD.md"
STOP_SETTINGS = ("KillMode=mixed", "KillSignal=SIGINT", "TimeoutStopSec=5min")
DEPLOY_RUNNER = "dflippotower-linux-agent-harness"
DEPLOY_SERVICE = "github-runner-agent-harness.service"
GH_SENTINEL = "ghp_SENTINELGITHUBTOKEN814"
NTFY_SENTINEL = "tk_SENTINELNTFYTOKEN814"


def tower_runner_doc() -> str:
    text = CI_CD_DOC.read_text(encoding="utf-8")
    return text[text.index("## Tower runner"):text.index("## Review runner pool")]


def doc_prose() -> str:
    """The section with its line wrapping undone, so a phrase matches wherever the lines happen to break."""
    return " ".join(tower_runner_doc().split())


def documented_unit() -> list[str]:
    blocks = re.findall(r"```\n(.*?)```", tower_runner_doc(), flags=re.DOTALL)
    units = [block for block in blocks if "[Service]" in block and "ExecStart=<runner dir>" in block]
    assert len(units) == 1, units
    return units[0].splitlines()


def test_dry_run_shows_the_stop_settings_that_take_down_the_whole_tree():
    result = run_install("--name", "tower-review-1", "--dry-run")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for setting in STOP_SETTINGS:
        assert setting in lines
    assert "KillMode=process" not in lines
    assert "KillSignal=SIGTERM" not in lines


def test_the_installed_unit_is_the_unit_the_dry_run_prints(tmp_path):
    host = FakeHost(tmp_path)
    planned = host.install("--dry-run")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    result = host.install()
    assert result.returncode == 0, result.stdout + result.stderr
    unit = (host.fsroot / "etc" / "systemd" / "system" / UNIT).read_text(encoding="utf-8")
    assert unit in planned.stdout
    lines = unit.splitlines()
    for setting in STOP_SETTINGS:
        assert setting in lines
    # mixed signals only the main process, so that process has to be the wrapper that forwards it to the listener.
    assert f"ExecStart={host.install_dir}/runsvc.sh" in lines


def test_the_documented_unit_stops_the_tree_through_the_process_that_forwards_the_signal():
    unit = documented_unit()
    for setting in STOP_SETTINGS:
        assert setting in unit
    assert "ExecStart=<runner dir>/runsvc.sh" in unit
    assert "WorkingDirectory=<runner dir>" in unit
    assert "KillMode=process" not in unit
    assert "ExecStart=<runner dir>/run.sh" not in unit


@pytest.mark.parametrize("explained", [
    "KillMode=process", "orphaned", ".credentials", "control-group", "mixed", "every process", "main process",
    "cancels the running job",
])
def test_the_doc_explains_the_stop_settings(explained):
    assert explained in doc_prose()


def test_re_registration_stops_the_service_and_checks_for_a_listener_before_config_sh():
    doc = tower_runner_doc()
    section = doc[doc.index("### Re-registering the deploy runner"):doc.index("### Runner health check")]
    # The commands someone runs, not the prose around them, have to be in this order.
    commands = "".join(re.findall(r"```\n(.*?)```", section, flags=re.DOTALL))
    stop = commands.find(f"systemctl --user stop {DEPLOY_SERVICE}")
    check = commands.find("pgrep")
    config = commands.find("./config.sh")
    assert stop != -1, "the steps never stop the service"
    assert check != -1, "the steps never check for a leftover listener"
    assert config != -1, "the steps never run config.sh"
    assert "Runner.Listener" in commands[check:config]
    assert stop < check < config


def test_owner_steps_list_the_unit_the_timer_the_credential_and_the_manual_checks():
    doc = tower_runner_doc()
    section = " ".join(doc[doc.index("### Applying this on the tower"):].split())
    # systemd doesn't expand specifiers in an EnvironmentFile, so a %h there would name a file that doesn't exist.
    assert "_FILE=%h" not in section
    for needed in (
        "runner-health.timer", "enable --now", "Administration", "RUNNER_HEALTH_GH_TOKEN_FILE",
        "systemctl --user restart", "Runner.Listener", ".credentials", "Workers don't change the live runner",
    ):
        assert needed in section, needed


def test_the_health_timer_checks_within_the_stated_interval():
    timer = HEALTH_TIMER.read_text(encoding="utf-8").splitlines()
    # OnCalendar keeps firing after a failed check; every 5 minutes leaves room for GitHub's delay in the 10 stated.
    assert "OnCalendar=*:0/5" in timer
    assert "AccuracySec=30s" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert "raises an ntfy alert within 10 minutes" in doc_prose()


def test_the_health_service_runs_the_script_without_a_token_in_its_unit():
    service = HEALTH_SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in service.splitlines()
    assert "ops/github/runner-health.sh" in service
    assert "EnvironmentFile=%h/.config/agent-harness/runner-health.env" in service.splitlines()
    assert "TOKEN=" not in service


def test_the_health_script_has_lf_is_executable_and_parses_with_bash():
    assert b"\r\n" not in HEALTH_SCRIPT.read_bytes()
    if sys.platform.startswith("linux"):
        assert os.access(HEALTH_SCRIPT, os.X_OK)
    if BASH:
        parsed = subprocess.run([BASH, "-n", str(HEALTH_SCRIPT)], capture_output=True, text=True, check=False)
        assert parsed.returncode == 0, parsed.stderr


HEALTH_FAKES = {
    "systemctl": """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$FAKE_LOG"
if [ -z "$FAKE_SERVICE_STATE" ]; then echo 'Failed to connect to bus' >&2; exit 1; fi
echo "$FAKE_SERVICE_STATE"
[ "$FAKE_SERVICE_STATE" = active ]
""",
    # A real jq runs the script's filter on a real answer, so the name match is what's tested. The failing modes print
    # the token, the worst a failing dependency could do, to show that nothing from gh reaches the output or the alert.
    "gh": """#!/bin/sh
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
[ "$GH_TOKEN" = "$FAKE_EXPECTED_GH_TOKEN" ] && echo yes >> "$FAKE_GH_TOKEN_SEEN"
case "$FAKE_GH_MODE" in
  unauthorized) echo "gh: Bad credentials for $GH_TOKEN (HTTP 401)" >&2; exit 1 ;;
  forbidden) echo "gh: Resource not accessible by $GH_TOKEN (HTTP 403)" >&2; exit 1 ;;
  unreachable) echo "dial tcp: connection refused, token $GH_TOKEN" >&2; exit 1 ;;
  garbage) echo "<html>$GH_TOKEN</html>"; exit 0 ;;
esac
while [ "$#" -gt 0 ]; do
  case "$1" in --jq) filter="$2"; shift 2 ;; *) shift ;; esac
done
jq -r "$filter" "$FAKE_RUNNERS"
""",
    # Records the real command line, the place a bearer token would show in the process table, then runs real curl.
    "curl": """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$FAKE_LOG"
exec "$REAL_CURL" "$@"
""",
}


class FakeNtfy:
    """A real loopback HTTP endpoint, so the script's real curl call and its Authorization header are exercised."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": json.loads(body),
                })
                self.send_response(owner.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class names it format
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)


@pytest.fixture
def ntfy():
    server = FakeNtfy()
    yield server
    server.close()


class HealthHost:
    """Runs ops/github/runner-health.sh with fake systemctl and gh, real curl and jq, and a loopback ntfy."""

    def __init__(self, tmp_path: Path, ntfy_server: FakeNtfy):
        if not BASH:
            pytest.skip("bash is not installed")
        if not sys.platform.startswith("linux"):
            pytest.skip("the health check runs on the Linux tower")
        for tool in ("curl", "jq", "timeout"):
            if not shutil.which(tool):
                pytest.skip(f"{tool} is not installed")
        self.tmp = tmp_path
        self.ntfy = ntfy_server
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in HEALTH_FAKES.items():
            (self.bin / name).write_text(text, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.log = tmp_path / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.gh_token_seen = tmp_path / "gh-token-seen"
        self.gh_token_file = tmp_path / "gh-token"
        self.gh_token_file.write_text(f"{GH_SENTINEL}\n", encoding="utf-8")
        self.ntfy_token_file = tmp_path / "ntfy-token"
        self.ntfy_token_file.write_text(f"{NTFY_SENTINEL}\n", encoding="utf-8")
        self.state = tmp_path / "state" / "runner-health.state"
        self.runners = tmp_path / "runners.json"
        self.set_runner_status("online")
        self.results: list[subprocess.CompletedProcess[str]] = []

    def set_runner_status(self, status: str | None) -> None:
        # Another runner stays online, so a filter that doesn't match on the name would call an offline runner healthy.
        runners = [{"id": 1, "name": "dflippotower-agent-harness-review-1", "status": "online", "busy": False}]
        if status is not None:
            runners.append({"id": 2, "name": DEPLOY_RUNNER, "status": status, "busy": False})
        self.runners.write_text(json.dumps({"total_count": len(runners), "runners": runners}), encoding="utf-8")

    def run(self, service: str = "active", gh_mode: str = "json", **overrides: str) -> subprocess.CompletedProcess[str]:
        # A clean environment: nothing inherited can stand in for the settings or send curl through a proxy.
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "RUNNER_HEALTH_NTFY_SERVER": self.ntfy.url,
            "RUNNER_HEALTH_NTFY_TOPIC": "synthetic-topic",
            "RUNNER_HEALTH_NTFY_TOKEN_FILE": str(self.ntfy_token_file),
            "RUNNER_HEALTH_GH_TOKEN_FILE": str(self.gh_token_file),
            "RUNNER_HEALTH_STATE_FILE": str(self.state),
            "FAKE_LOG": str(self.log),
            "FAKE_SERVICE_STATE": service,
            "FAKE_GH_MODE": gh_mode,
            "FAKE_RUNNERS": str(self.runners),
            "FAKE_EXPECTED_GH_TOKEN": GH_SENTINEL,
            "FAKE_GH_TOKEN_SEEN": str(self.gh_token_seen),
            "REAL_CURL": shutil.which("curl"),
            **overrides,
        }
        result = subprocess.run(
            [BASH, str(HEALTH_SCRIPT)], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        self.results.append(result)
        return result

    def alerts(self) -> list[dict]:
        return [request["payload"] for request in self.ntfy.requests]


@pytest.fixture
def health(tmp_path, ntfy):
    return HealthHost(tmp_path, ntfy)


def test_a_healthy_runner_raises_no_alert(health):
    result = health.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert health.alerts() == []
    calls = health.log.read_text(encoding="utf-8")
    assert f"systemctl --user is-active {DEPLOY_SERVICE}" in calls
    assert "repos/dflippojr/agent-harness/actions/runners" in calls
    assert health.gh_token_seen.is_file(), "gh never received the token from the configured file"


@pytest.mark.parametrize("state", ["inactive", "failed", "activating", "deactivating"])
def test_a_service_that_is_not_active_raises_an_alert(health, state):
    result = health.run(service=state)
    assert result.returncode == 0, result.stdout + result.stderr
    alerts = health.alerts()
    assert len(alerts) == 1
    assert alerts[0]["topic"] == "synthetic-topic"
    assert "service" in alerts[0]["title"].lower()
    assert DEPLOY_SERVICE in alerts[0]["message"]
    assert state in alerts[0]["message"]


def test_an_inactive_service_is_named_even_when_github_cannot_be_asked(health):
    health.run(service="inactive", gh_mode="unreachable")
    alerts = health.alerts()
    assert len(alerts) == 1
    assert "can't tell" not in alerts[0]["title"].lower()


def test_an_active_service_with_an_offline_runner_raises_an_alert(health):
    # The incident: the service looked fine and the runner was dead.
    health.set_runner_status("offline")
    result = health.run()
    assert result.returncode == 0, result.stdout + result.stderr
    alerts = health.alerts()
    assert len(alerts) == 1
    assert "offline" in alerts[0]["title"].lower()
    assert DEPLOY_RUNNER in alerts[0]["message"]


def test_a_runner_missing_from_the_list_raises_the_offline_alert(health):
    health.set_runner_status(None)
    health.run()
    alerts = health.alerts()
    assert len(alerts) == 1
    assert "offline" in alerts[0]["title"].lower()
    assert "not registered" in alerts[0]["message"]


@pytest.mark.parametrize(("mode", "reason"), [
    ("unreachable", "unreachable"),
    ("unauthorized", "refused"),
    ("forbidden", "refused"),
    ("garbage", "unexpected"),
])
def test_an_api_that_cannot_answer_raises_a_distinct_cannot_tell_alert(health, mode, reason):
    result = health.run(gh_mode=mode)
    assert result.returncode == 0, result.stdout + result.stderr
    alerts = health.alerts()
    assert len(alerts) == 1
    assert "can't tell" in alerts[0]["title"].lower()
    assert "offline" not in alerts[0]["title"].lower()
    assert reason in alerts[0]["message"]


def test_a_missing_github_token_file_raises_the_cannot_tell_alert(health):
    health.gh_token_file.unlink()
    health.run()
    alerts = health.alerts()
    assert len(alerts) == 1
    assert "can't tell" in alerts[0]["title"].lower()
    assert "RUNNER_HEALTH_GH_TOKEN_FILE" in alerts[0]["message"]
    assert "gh " not in health.log.read_text(encoding="utf-8")


def test_a_systemctl_that_cannot_answer_raises_the_cannot_tell_alert(health):
    health.run(service="")
    alerts = health.alerts()
    assert len(alerts) == 1
    assert "can't tell" in alerts[0]["title"].lower()


def test_one_outage_raises_one_alert_and_the_next_outage_raises_another(health):
    health.set_runner_status("offline")
    for _ in range(3):
        assert health.run().returncode == 0
    assert len(health.alerts()) == 1

    health.set_runner_status("online")
    assert health.run().returncode == 0
    alerts = health.alerts()
    assert len(alerts) == 2
    assert "recovered" in alerts[1]["title"].lower()
    assert health.run().returncode == 0
    assert len(health.alerts()) == 2

    health.set_runner_status("offline")
    assert health.run().returncode == 0
    assert len(health.alerts()) == 3
    assert "offline" in health.alerts()[2]["title"].lower()


def test_the_same_cannot_tell_outage_does_not_repeat_when_its_reason_changes(health):
    health.run(gh_mode="unreachable")
    health.run(gh_mode="unauthorized")
    assert len(health.alerts()) == 1


def test_a_different_kind_of_outage_raises_its_own_alert(health):
    health.run(gh_mode="unreachable")
    health.run(service="inactive")
    assert len(health.alerts()) == 2


def test_an_alert_ntfy_refused_is_sent_again_on_the_next_check(health):
    # Recording the outage before delivery would turn one failed post into silence for the rest of it.
    health.ntfy.status = 503
    health.set_runner_status("offline")
    failed = health.run()
    assert failed.returncode != 0
    assert "ntfy did not accept" in failed.stderr
    assert not health.state.exists()
    refused = len(health.alerts())  # curl retried a 503 itself.
    assert refused >= 1
    health.ntfy.status = 200
    assert health.run().returncode == 0
    assert len(health.alerts()) == refused + 1
    assert health.run().returncode == 0
    assert len(health.alerts()) == refused + 1


@pytest.mark.parametrize("status", [301, 302, 308])
def test_a_redirect_from_ntfy_is_not_a_delivered_alert(health, status):
    # curl --fail only rejects 400 and up, and a redirect isn't followed, so nothing was published.
    health.ntfy.status = status
    health.set_runner_status("offline")
    failed = health.run()
    assert failed.returncode != 0
    assert "ntfy did not accept" in failed.stderr
    assert not health.state.exists()
    health.ntfy.status = 200
    assert health.run().returncode == 0
    assert health.state.read_text(encoding="utf-8").strip() == "runner-offline"


def test_an_unreachable_ntfy_fails_the_check_so_systemd_shows_it(health):
    health.ntfy.close()
    health.set_runner_status("offline")
    result = health.run()
    assert result.returncode != 0
    assert "ntfy did not accept" in result.stderr
    assert not health.state.exists()


@pytest.mark.parametrize(("name", "value", "message"), [
    ("RUNNER_HEALTH_NTFY_SERVER", "", "RUNNER_HEALTH_NTFY_SERVER"),
    ("RUNNER_HEALTH_NTFY_SERVER", "file:///etc/passwd", "RUNNER_HEALTH_NTFY_SERVER"),
    ("RUNNER_HEALTH_NTFY_SERVER", "https://user:secret@ntfy.invalid/", "RUNNER_HEALTH_NTFY_SERVER"),
    ("RUNNER_HEALTH_NTFY_TOPIC", "", "RUNNER_HEALTH_NTFY_TOPIC"),
    ("RUNNER_HEALTH_NTFY_TOPIC", 'bad","topic":"other', "RUNNER_HEALTH_NTFY_TOPIC"),
    ("RUNNER_HEALTH_REPO", "not a repo", "RUNNER_HEALTH_REPO"),
    ("RUNNER_HEALTH_RUNNER", 'x") | "online', "RUNNER_HEALTH_RUNNER"),
    ("RUNNER_HEALTH_SERVICE", "--all", "RUNNER_HEALTH_SERVICE"),
])
def test_bad_settings_fail_the_check_before_any_command_runs(health, name, value, message):
    result = health.run(**{name: value})
    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr
    assert health.log.read_text(encoding="utf-8") == ""
    assert health.alerts() == []


def test_an_unreadable_ntfy_token_file_fails_instead_of_posting_without_the_token(health):
    health.ntfy_token_file.unlink()
    health.set_runner_status("offline")
    result = health.run()
    assert result.returncode != 0
    assert health.alerts() == []
    assert not health.state.exists()


def test_a_token_that_could_change_curls_config_is_refused(health):
    health.ntfy_token_file.write_text('x"\nurl = "http://attacker.invalid/\n', encoding="utf-8")
    health.set_runner_status("offline")
    result = health.run()
    assert result.returncode != 0
    assert health.alerts() == []
    assert "attacker" not in health.log.read_text(encoding="utf-8")


def test_no_token_reaches_the_output_the_alerts_the_state_or_a_command_line(health):
    scenarios = [
        {},
        {"service": "inactive"},
        {"gh_mode": "unauthorized"},
        {"gh_mode": "forbidden"},
        {"gh_mode": "unreachable"},
        {"gh_mode": "garbage"},
        {},
    ]
    health.set_runner_status("offline")
    for scenario in scenarios:
        health.run(**scenario)
        for sentinel in (GH_SENTINEL, NTFY_SENTINEL):
            assert sentinel not in health.state.read_text(encoding="utf-8")
    captured = (
        [result.stdout + result.stderr for result in health.results]
        + [json.dumps(payload) for payload in health.alerts()]
        + [health.log.read_text(encoding="utf-8")]
    )
    for text in captured:
        assert GH_SENTINEL not in text
        assert NTFY_SENTINEL not in text
    # It wasn't missing because nothing used it: ntfy got its token, and gh got its own.
    assert len(health.ntfy.requests) >= 3
    assert {request["authorization"] for request in health.ntfy.requests} == {f"Bearer {NTFY_SENTINEL}"}
    assert health.gh_token_seen.is_file()


def test_curl_does_not_inherit_the_github_token(health, tmp_path):
    # ntfy has no business with the GitHub token, so the process that talks to it doesn't get it.
    seen = tmp_path / "curl-env"
    (health.bin / "curl").write_text(
        f'#!/bin/sh\nprintf "%s" "${{GH_TOKEN:-}}" > "{seen}"\nexec "$REAL_CURL" "$@"\n', encoding="utf-8",
    )
    health.set_runner_status("offline")
    health.run()
    assert len(health.alerts()) == 1
    assert seen.read_text(encoding="utf-8") == ""
