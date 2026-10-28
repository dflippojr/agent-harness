#!/usr/bin/env bash
# Registers a pinned GitHub Actions runner on Linux as a systemd service under a dedicated user.
#
# The Linux counterpart of ops/github/install-runner.ps1 (#806). The registration token is used once and is not
# saved. Run it as root (sudo): it creates the service user if needed, unpacks the runner into that user's home,
# registers it as that user (config.sh refuses root), and installs the runner's own systemd unit (svc.sh) with
# User= set to the service user, so jobs never run as root.
#
# Each review pool member needs distinct --name and --install-dir, and the service user's Codex, Claude and Cursor
# CLIs logged in. Review runners also need PowerShell 7.5+ (pwsh) on PATH: review.yml runs every step in it. Keep the
# install directory under /home: the review's publication scan rejects a path through a home directory, so a
# leaked runner path cannot be posted.
#
#   token=$(gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token)
#   sudo ops/github/install-runner.sh --token "$token" --labels agent-harness-review --name dflippotower-review-1
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
Usage: sudo ops/github/install-runner.sh --token TOKEN [options]

  --token TOKEN        one-time runner registration token (required)
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
    --token) TOKEN="${2:-}"; shift 2 ;;
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

[[ -n "$TOKEN" ]] || fail "--token is required"
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
  *[!A-Za-z0-9_./-]*|*/../*|*/..|*/./*) fail "invalid --install-dir: $INSTALL_DIR" ;;
esac

dest="$fsroot$INSTALL_DIR"
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
  echo "dry run: would install and start the systemd service with svc.sh install $RUNNER_USER"
  exit 0
fi

[[ "$(id -u)" -eq "$root_uid" ]] || fail "run as root (sudo); the runner itself runs as $RUNNER_USER"
for tool in curl sha256sum tar systemctl useradd runuser; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done

if ! id -u "$RUNNER_USER" >/dev/null 2>&1; then
  useradd --create-home --user-group --shell /bin/bash "$RUNNER_USER"
fi
if ! runuser -u "$RUNNER_USER" -- bash -lc 'command -v pwsh' >/dev/null 2>&1; then
  echo "warning: pwsh is not on $RUNNER_USER's PATH; review.yml needs PowerShell 7 (pwsh)" >&2
fi

download="$(mktemp -d)"
trap 'rm -rf "$download"' EXIT
curl --proto =https --fail --silent --show-error --location --output "$download/$archive" "$url"
echo "$SHA256  $download/$archive" | sha256sum --check --status || fail "runner archive hash mismatch"

install -d -o "$RUNNER_USER" -g "$(id -gn "$RUNNER_USER")" -m 0750 "$dest"
# Root opens the verified archive from its private temp tree and the service user's tar reads it on stdin.
runuser -u "$RUNNER_USER" -- tar -xzf - -C "$dest" < "$download/$archive"

cd "$dest"
runuser -u "$RUNNER_USER" -- ./config.sh --unattended --url "https://github.com/$REPO" --token "$TOKEN" \
  --name "$NAME" --labels "$LABELS" --work "$WORK_DIR"
./svc.sh install "$RUNNER_USER"
./svc.sh start
./svc.sh status
