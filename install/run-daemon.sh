#!/usr/bin/env bash
# Restarting supervisor used by systemd --user and launchd installs.
# Advertises supervised restart by setting HARNESS_SUPERVISED=1 on the daemon process.
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
        exec env HARNESS_CONFIG_DIR="$config_dir" HARNESS_SUPERVISED=1 "$python" -u -m harness
    ) >>"$daemon_log" 2>&1 &
    child=$!
    wait "$child"
    status=$?
    child=
    echo "$(stamp) daemon exited with code $status; restarting in 10s" >>"$supervisor_log"
    sleep 10
done
