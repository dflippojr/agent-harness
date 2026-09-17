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
child=

mkdir -p "$log_dir"
stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }
stop_child() {
    docker stop -t 10 "$container" >/dev/null 2>&1 || true
    if [[ -n "$child" ]]; then
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}
trap stop_child TERM INT HUP

while :; do
    if [[ -e "$pause_flag" ]]; then
        echo "$(stamp) paused by GPU guard; waiting for $pause_flag to be removed" >>"$supervisor_log"
        while [[ -e "$pause_flag" ]]; do sleep 5; done
    fi
    [[ -f "$server_log" ]] && mv -f "$server_log" "$server_log.prev"
    echo "$(stamp) starting llama-server image $image" >>"$supervisor_log"
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker run --rm --name "$container" --gpus all --network host \
        -v "$model_path:/models/model.gguf:ro" "$image" \
        -m /models/model.gguf --alias "$model_name" --host 127.0.0.1 --port "$port" \
        --ctx-size "$context" --flash-attn on --parallel 1 --jinja --metrics \
        --sleep-idle-seconds 1800 "${extra_args[@]}" >>"$server_log" 2>&1 &
    child=$!
    wait "$child"
    status=$?
    child=
    echo "$(stamp) llama-server exited with code $status; restarting in 15s" >>"$supervisor_log"
    sleep 15
done
