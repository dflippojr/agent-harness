#!/usr/bin/env bash
# Registers a pinned GitHub Actions runner on Linux as a systemd service under a dedicated user.
#
# The Linux counterpart of ops/github/install-runner.ps1 (#806). The registration token is read from standard input,
# used once and not saved. It never goes in a command line, where the process table and sudo's log would show it:
# config.sh gets it through the runner's ACTIONS_RUNNER_INPUT_TOKEN environment variable. Run it as root (sudo): it
# creates the service user if needed, unpacks the runner into that user's home, registers it as that user (config.sh
# refuses root), and installs a systemd unit with User= set to the service user, so jobs never run as root.
#
# The service user owns everything under its home, and a job running as that user could rewrite any of it. So root
# never acts on a path there: it refuses a destination with a symlink in it or under a parent others can write, the
# service user creates the directory and unpacks the runner, and root writes the unit itself instead of running the
# runner's svc.sh, which config.sh writes as the service user and which moves, copies and chowns files there as root.
#
# Each review pool member needs distinct --name and --install-dir, and the service user's Codex, Claude and Cursor
# CLIs logged in. Review runners also need PowerShell 7.5+ (pwsh) on PATH: review.yml runs every step in it. Keep the
# install directory under /home: the review's publication scan rejects a path through a home directory, so a
# leaked runner path cannot be posted.
#
#   gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token |
#     sudo ops/github/install-runner.sh --labels agent-harness-review --name dflippotower-review-1
#
# --dry-run prints the plan and changes nothing: no download, user, files or systemd unit.
set -euo pipefail

REPO="dflippojr/agent-harness"
LABELS="agent-harness-review"
NAME=""
RUNNER_USER="github-runner"
INSTALL_DIR=""
WORK_DIR="_work"
TOKEN=""
DRY_RUN=0
# The release ops/github/install-runner.ps1 pins, with GitHub's published SHA-256 of its linux-x64 archive.
VERSION="2.337.0"
SHA256="70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613"

usage() {
  cat <<'EOF'
Usage: <registration token> | sudo ops/github/install-runner.sh [options]

The one-time runner registration token is read from standard input (or prompted for, hidden, on a terminal).

  --repo OWNER/NAME    repository to register with (default dflippojr/agent-harness)
  --labels LIST        comma-separated runner labels (default agent-harness-review)
  --name NAME          runner name (default <short hostname>-<first label>)
  --user USER          service user the runner runs as (default github-runner; created if missing)
  --install-dir DIR    runner directory (default /home/<user>/runners/<name>)
  --work DIR           work directory, relative to the install directory (default _work)
  --version VERSION    runner release (default 2.337.0)
  --sha256 HASH        SHA-256 of that release's linux-x64 archive (required with --version)
  --dry-run            print the plan and change nothing
EOF
}

fail() { echo "install-runner: $*" >&2; exit 1; }

custom_version=0
custom_sha=0
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --token) fail "--token is not accepted: pass the registration token on standard input" ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --labels) LABELS="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --user) RUNNER_USER="${2:-}"; shift 2 ;;
    --install-dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --work) WORK_DIR="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; custom_version=1; shift 2 ;;
    --sha256) SHA256="${2:-}"; custom_sha=1; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown argument: $1" ;;
  esac
done

# The OS and architecture can be overridden so tests can exercise the checks on any host.
os="${HARNESS_INSTALLER_OS:-$(uname -s)}"
arch="${HARNESS_INSTALLER_ARCH:-$(uname -m)}"
[[ "$os" = "Linux" ]] || fail "this installer is for Linux; on Windows use ops/github/install-runner.ps1"
[[ "$arch" = "x86_64" ]] || fail "only x86_64 (the X64 runner label) is supported, not $arch"
# Tests run the install path without root: HARNESS_INSTALLER_FAKE_ROOT names a synthetic directory standing in for /,
# and the caller's uid stands in for root's. sudo resets the environment, so a real install never sees it.
fsroot="${HARNESS_INSTALLER_FAKE_ROOT:-}"
root_uid=0
if [[ -n "$fsroot" ]]; then
  root_uid="$(id -u)"
fi

if [[ -t 0 ]]; then
  read -rsp "registration token: " TOKEN || true
  echo >&2
else
  IFS= read -r TOKEN || true
fi
TOKEN="${TOKEN//[[:space:]]/}"
[[ -n "$TOKEN" ]] || fail "a registration token is required on standard input"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "invalid --repo: $REPO"
[[ "$LABELS" =~ ^[A-Za-z0-9_.-]+(,[A-Za-z0-9_.-]+)*$ ]] || fail "invalid --labels: $LABELS"
[[ "$RUNNER_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid --user: $RUNNER_USER"
[[ "$RUNNER_USER" != "root" ]] || fail "the runner must not run as root"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid --version: $VERSION"
if [[ "$custom_version" -eq 1 ]] && [[ "$custom_sha" -eq 0 ]]; then
  fail "--version needs the matching --sha256 from the runner release notes"
fi
[[ "$SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "invalid --sha256: $SHA256"
if [[ -z "$NAME" ]]; then
  host="$(hostname -s 2>/dev/null || hostname)"
  NAME="${host,,}-${LABELS%%,*}"
fi
[[ "$NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || fail "invalid --name: $NAME"
[[ "$WORK_DIR" =~ ^[A-Za-z0-9_.-]+$ ]] && [[ "$WORK_DIR" != ".." ]] || fail "invalid --work: $WORK_DIR"
[[ -n "$INSTALL_DIR" ]] || INSTALL_DIR="/home/$RUNNER_USER/runners/$NAME"
case "$INSTALL_DIR" in
  /home/?*/?*) ;;
  *) fail "--install-dir must be under /home, so the review's path scan covers it: $INSTALL_DIR" ;;
esac
case "$INSTALL_DIR" in
  *[!A-Za-z0-9_./-]*|*/../*|*/..|*/./*|*//*) fail "invalid --install-dir: $INSTALL_DIR" ;;
esac

dest="$fsroot$INSTALL_DIR"
# The unit name svc.sh would use, so the runner's own tools still find the service.
unit="actions.runner.${REPO//\//-}.$NAME.service"
unit_path="$fsroot/etc/systemd/system/$unit"
archive="actions-runner-linux-x64-$VERSION.tar.gz"
url="https://github.com/actions/runner/releases/download/v$VERSION/$archive"

echo "repository: $REPO"
echo "runner: $NAME (labels: $LABELS; GitHub adds self-hosted, Linux, X64)"
echo "service user: $RUNNER_USER"
echo "install directory: $INSTALL_DIR (work: $WORK_DIR)"
echo "runner release: $VERSION ($archive, sha256 $SHA256)"

if [[ -e "$dest/.runner" ]]; then
  fail "a runner is already configured in $INSTALL_DIR"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry run: would create user $RUNNER_USER if missing (no sudo or docker group)"
  echo "dry run: would download $url and check its sha256"
  echo "dry run: would unpack into $INSTALL_DIR, owned by $RUNNER_USER"
  echo "dry run: would run config.sh --unattended as $RUNNER_USER with --labels $LABELS"
  echo "dry run: would write, as root, the unit svc.sh install $RUNNER_USER writes ($unit) and start it"
  exit 0
fi

[[ "$(id -u)" -eq "$root_uid" ]] || fail "run as root (sudo); the runner itself runs as $RUNNER_USER"
for tool in curl sha256sum tar systemctl useradd runuser stat; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done

if ! id -u "$RUNNER_USER" >/dev/null 2>&1; then
  useradd --create-home --user-group --shell /bin/bash "$RUNNER_USER"
fi
if ! runuser -u "$RUNNER_USER" -- bash -lc 'command -v pwsh' >/dev/null 2>&1; then
  echo "warning: pwsh is not on $RUNNER_USER's PATH; review.yml needs PowerShell 7 (pwsh)" >&2
fi

# Checks each existing component of the destination without following links. / and /home must be root's alone; below
# them, every directory that exists must belong to the service user, who creates the rest.
trusted_dir() {
  local path="$1" label="${2:-${1#"$fsroot"}}" info
  [[ ! -L "$path" ]] && [[ -d "$path" ]] || fail "refusing $INSTALL_DIR: $label is not a plain directory"
  info="$(stat -c '%u %a' -- "$path")"
  [[ "${info% *}" -eq "$root_uid" ]] && (( (8#${info#* } & 8#022) == 0 )) \
    || fail "refusing $INSTALL_DIR: $label must be owned by root and writable only by root"
}
trusted_dir "$fsroot/" /
trusted_dir "$fsroot/home"
runner_uid="$(id -u "$RUNNER_USER")"
path="$fsroot/home"
IFS=/ read -ra parts <<< "${INSTALL_DIR#/home/}"
for part in "${parts[@]}"; do
  path="$path/$part"
  [[ ! -L "$path" ]] || fail "refusing $INSTALL_DIR: ${path#"$fsroot"} is a symlink"
  [[ -e "$path" ]] || break
  [[ -d "$path" ]] || fail "refusing $INSTALL_DIR: ${path#"$fsroot"} is not a directory"
  owner="$(stat -c %u -- "$path")"
  [[ "$owner" -eq "$runner_uid" ]] || fail "refusing $INSTALL_DIR: ${path#"$fsroot"} does not belong to $RUNNER_USER"
done
[[ ! -e "$unit_path" ]] && [[ ! -L "$unit_path" ]] || fail "$unit_path already exists"

download="$(mktemp -d)"
trap 'rm -rf "$download"' EXIT
curl --proto =https --fail --silent --show-error --location --output "$download/$archive" "$url"
echo "$SHA256  $download/$archive" | sha256sum --check --status || fail "runner archive hash mismatch"

# The service user creates its own directory, so a link swapped in after the checks only redirects its own mkdir.
runuser -u "$RUNNER_USER" -- mkdir -p -m 0750 -- "$dest"
# Root opens the verified archive from its private temp tree and the service user's tar reads it on stdin.
runuser -u "$RUNNER_USER" -- tar -xzf - -C "$dest" < "$download/$archive"

# runuser keeps the environment. The runner reads the token from it and drops it from what its children inherit.
ACTIONS_RUNNER_INPUT_TOKEN="$TOKEN" runuser -u "$RUNNER_USER" -- env -C "$dest" ./config.sh --unattended \
  --url "https://github.com/$REPO" --name "$NAME" --labels "$LABELS" --work "$WORK_DIR"

# What svc.sh install does, with root writing only under /etc/systemd/system and the service user doing the rest.
cat > "$unit_path" <<UNIT
[Unit]
Description=GitHub Actions Runner (${REPO//\//-}.$NAME)
After=network-online.target

[Service]
ExecStart=$INSTALL_DIR/runsvc.sh
User=$RUNNER_USER
WorkingDirectory=$INSTALL_DIR
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$unit_path"
runuser -u "$RUNNER_USER" -- install -m 0755 -- "$dest/bin/runsvc.sh" "$dest/runsvc.sh"
echo "$unit" | runuser -u "$RUNNER_USER" -- tee "$dest/.service" >/dev/null
systemctl daemon-reload
systemctl enable "$unit"
systemctl start "$unit"
systemctl --no-pager status "$unit" || true
