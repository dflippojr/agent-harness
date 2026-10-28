"""Issue #806: the Linux GitHub Actions runner installer (ops/github/install-runner.sh).

Most cases are dry runs. The install cases run the real install path without root: a synthetic filesystem root
stands in for / (HARNESS_INSTALLER_FAKE_ROOT), and fake runuser, curl and systemctl on PATH record every call. The
fake runuser locks root's private temp tree for as long as a service-user command runs, so a path from root's side
that the service user could not open fails here too.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
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
