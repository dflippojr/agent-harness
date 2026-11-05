#!/usr/bin/env bash
# Real-environment lane (#731) entry point for the scheduled task (docs/real-env-lane.md).
#
#   ops/real-env/run-lane.sh [lane run options...]      e.g. --suite hosted-cli --pair, or --sha <PR SHA>
#
# Keeps the lane's own clone and venv under $REAL_ENV_BASE (default ~/.local/share/agent-harness-real-env), never the
# deployed checkout or its venv: the lane only reads the live daemon's /metrics and config. Updates the clone to
# origin/main, installs that commit's requirements into the lane venv, then runs scripts/real_env_lane.py from main
# against the commit to test (origin/main unless --sha says otherwise). Statuses and issues go out through `gh`, as
# whoever this user's gh is logged in as.
set -euo pipefail

base="${REAL_ENV_BASE:-${XDG_DATA_HOME:-$HOME/.local/share}/agent-harness-real-env}"
repo="$base/repo"
venv="$base/venv"
python="${REAL_ENV_PYTHON:-python3}"
mkdir -p "$base/tmp"
export TMPDIR="$base/tmp"

if [[ ! -d "$repo/.git" ]]; then
    git clone -q https://github.com/dflippojr/agent-harness.git "$repo"
fi
git -C "$repo" fetch -q origin main
git -C "$repo" checkout -q --detach origin/main
# The installed copy of this script hands over to main's, so a change to it reaches the scheduled task.
if [[ -z "${REAL_ENV_HANDED_OVER:-}" ]] && ! cmp -s "$0" "$repo/ops/real-env/run-lane.sh"; then
    REAL_ENV_HANDED_OVER=1 exec /bin/bash "$repo/ops/real-env/run-lane.sh" "$@"
fi

if [[ ! -x "$venv/bin/python" ]]; then
    "$python" -m venv "$venv"
fi
"$venv/bin/python" -m pip install -q --disable-pip-version-check \
    -r "$repo/requirements.txt" -r "$repo/requirements-repomap.txt" -r "$repo/requirements-telemetry.txt" pytest-xdist

exec "$venv/bin/python" "$repo/scripts/real_env_lane.py" run --repo "$repo" --base "$base" --python "$venv/bin/python" "$@"
