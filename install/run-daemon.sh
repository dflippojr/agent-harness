#!/usr/bin/env bash
# Restarting supervisor used by systemd --user and launchd installs.
# Advertises supervised restart by setting HARNESS_SUPERVISED=1 on the daemon process.
# A CI deployment (ops/harness/deploy-ci.ps1) switches the daemon's environment and Stables copy through pointer files
# in APP_DIR: .venv-path replaces PYTHON with that environment's interpreter (its name carries the deployed commit,
# HARNESS_BUILD_COMMIT), and .stables-path becomes HARNESS_STABLES_DIR.
set -u

if [[ $# -ne 4 ]]; then
    echo "usage: run-daemon.sh APP_DIR CONFIG_DIR PYTHON LOG_DIR" >&2
    exit 64
fi

app_dir=$1
config_dir=$2
python=$3
log_dir=$4
mkdir -p "$log_dir"
daemon_log="$log_dir/daemon.log"
daemon_env=()
if [[ -f "$app_dir/.venv-path" ]]; then
    venv=
    read -r venv <"$app_dir/.venv-path" || true
    [[ $venv == /* ]] || { echo "invalid deployment virtual environment pointer: $app_dir/.venv-path" >&2; exit 1; }
    python=$venv/bin/python
    leaf=${venv%/}
    leaf=${leaf##*/}
    deployed='^\.agent-harness\.venv\.deploy-([0-9a-fA-F]{40})$'
    if [[ $leaf =~ $deployed ]]; then
        daemon_env+=("HARNESS_BUILD_COMMIT=$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:upper:]' '[:lower:]')")
    fi
fi
if [[ -f "$app_dir/.stables-path" ]]; then
    stables=
    read -r stables <"$app_dir/.stables-path" || true
    [[ $stables == /* ]] || { echo "invalid Stables pointer: $app_dir/.stables-path" >&2; exit 1; }
    daemon_env+=("HARNESS_STABLES_DIR=$stables")
fi
supervisor_log="$log_dir/daemon-supervisor.log"
child=

stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }
stop_child() {
    if [[ -n "$child" ]]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}
trap stop_child TERM INT HUP

while :; do
    if [[ -f "$daemon_log" ]] && [[ $(wc -c <"$daemon_log") -gt 20971520 ]]; then
        mv -f "$daemon_log" "$daemon_log.prev"
    fi
    echo "$(stamp) starting daemon" >>"$supervisor_log"
    (
        cd "$app_dir" || exit 1
        # The guarded expansion keeps an empty array from tripping set -u in macOS's bash 3.2.
        exec env HARNESS_CONFIG_DIR="$config_dir" HARNESS_SUPERVISED=1 ${daemon_env[@]+"${daemon_env[@]}"} \
            "$python" -u -m harness
    ) >>"$daemon_log" 2>&1 &
    child=$!
    wait "$child"
    status=$?
    child=
    echo "$(stamp) daemon exited with code $status; restarting in 10s" >>"$supervisor_log"
    sleep 10
done
