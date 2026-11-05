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

# One run at a time, preparation included: updating the clone or the venv under a run in progress would change the code
# its later subprocesses load. This is the lane's own lock, <base>/lane.lock, the one a manual `real_env_lane.py run`
# takes, so a timer run and a manual run never overlap in either order. A small Python holder takes it (fcntl.flock, the
# lane's own lock; no util-linux `flock` needed), runs the rest of this script as its child and keeps the lock until
# that child, the hand-over and the final exec included, has ended. It writes a fresh token to lane.lock.owner and
# passes it in REAL_ENV_LOCK_TOKEN, which tells the lane run that its wrapper holds the lock. REAL_ENV_LOCK_WAIT
# (seconds) waits for a run in progress instead of exiting with status 75; the weekly hosted unit sets it.
hold_lock='
import os, secrets, signal, subprocess, sys, time
lock, wait, command = sys.argv[1], float(sys.argv[2] or 0), sys.argv[3:]
handle = open(lock, "a+b")

def take():
    try:
        import fcntl
    except ImportError:  # Windows, where the tests run this script under Git Bash: the first byte, as the lane locks it
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

deadline = time.monotonic() + wait
while True:
    try:
        take()
        break
    except OSError:
        if time.monotonic() >= deadline:
            print(f"another real-environment lane run holds {lock}", file=sys.stderr)
            sys.exit(75)
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
token = secrets.token_hex(16)
with open(lock + ".owner", "w", encoding="utf-8") as owner:
    owner.write(token)
child = subprocess.Popen(command, env={**os.environ, "REAL_ENV_LOCK_TOKEN": token})
signal.signal(signal.SIGINT, signal.SIG_IGN)  # a terminal interrupt reaches the child itself; the lock outlives it
signal.signal(signal.SIGTERM, lambda *_: child.terminate())
code = child.wait()
sys.exit(128 - code if code < 0 else code)
'
if [[ -z "${REAL_ENV_LOCK_TOKEN:-}" ]]; then
    self=("$BASH" "$0")
    lock="$base/lane.lock"
    if command -v cygpath >/dev/null 2>&1; then   # Git Bash on Windows (the tests): the holder is a Windows Python
        self=("$(cygpath -m "$BASH")" "$(cygpath -m "$0")")
        lock="$(cygpath -m "$lock")"
    fi
    exec "$python" -c "$hold_lock" "$lock" "${REAL_ENV_LOCK_WAIT:-0}" "${self[@]}" "$@"
fi

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
