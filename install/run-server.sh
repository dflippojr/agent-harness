#!/usr/bin/env bash
# Linux/NVIDIA llama.cpp supervisor. The pinned official image avoids requiring a host CUDA toolkit.
set -u

if [[ $# -lt 7 ]]; then
    echo "usage: run-server.sh INSTANCE IMAGE MODEL_PATH MODEL_NAME PORT CONTEXT PAUSE_FLAG [LLAMA_ARGS ...]" >&2
    exit 64
fi

instance=$1
image=$2
model_path=$3
model_name=$4
port=$5
context=$6
pause_flag=$7
shift 7
extra_args=("$@")
container="agent-harness-$(printf '%s' "$instance" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_.-' '-')-llama"
log_dir=${HARNESS_LOG_DIR:-"$(dirname "$pause_flag")/logs"}
server_log="$log_dir/llama-server.log"
supervisor_log="$log_dir/llama-server-supervisor.log"
poll=${HARNESS_PAUSE_POLL_SECONDS:-5}
child=
watcher=

mkdir -p "$log_dir"
stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }
stop_child() {
    [[ -n "$watcher" ]] && kill "$watcher" 2>/dev/null
    docker stop -t 10 "$container" >/dev/null 2>&1 || true
    if [[ -n "$child" ]]; then
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}
# The daemon parks the model by writing the pause flag (ServerControl.stop). It can't signal the container's root
# process, so this stops the container once the flag appears, then the loop below waits for the flag to go (#835).
watch_flag() {
    until [[ -e "$pause_flag" ]]; do sleep "$poll"; done
    echo "$(stamp) pause flag appeared; stopping $container" >>"$supervisor_log"
    while :; do
        docker stop -t 10 "$container" >/dev/null 2>&1 || true
        sleep "$poll"  # a container still being created when the flag appeared isn't stopped yet
    done
}
trap stop_child TERM INT HUP

while :; do
    if [[ -e "$pause_flag" ]]; then
        echo "$(stamp) paused by GPU guard; waiting for $pause_flag to be removed" >>"$supervisor_log"
        while [[ -e "$pause_flag" ]]; do sleep "$poll"; done
    fi
    [[ -f "$server_log" ]] && mv -f "$server_log" "$server_log.prev"
    echo "$(stamp) starting llama-server image $image" >>"$supervisor_log"
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker run --rm --name "$container" --gpus all --network host \
        -v "$model_path:/models/model.gguf:ro" "$image" \
        -m /models/model.gguf --alias "$model_name" --host 127.0.0.1 --port "$port" \
        --ctx-size "$context" --flash-attn on --parallel 1 --jinja --metrics \
        --sleep-idle-seconds 600 "${extra_args[@]}" >>"$server_log" 2>&1 &
    child=$!
    watch_flag &
    watcher=$!
    wait "$child"
    status=$?
    kill "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    child=
    watcher=
    if [[ -e "$pause_flag" ]]; then
        echo "$(stamp) llama-server stopped for the pause flag (exit code $status)" >>"$supervisor_log"
        continue
    fi
    echo "$(stamp) llama-server exited with code $status; restarting in 15s" >>"$supervisor_log"
    sleep 15
done
