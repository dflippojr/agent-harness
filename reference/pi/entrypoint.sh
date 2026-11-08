#!/bin/sh
# Writes the models.json the driver passes in PI_MODELS_JSON (bakeoff/pi_driver.py: pi_env), so the task workspace
# stays the container's only host mount. With PI_LLM_PORT set, relays 127.0.0.1:$PI_LLM_PORT to the run's socket
# (/run/llm/llm.sock, served by the host-network forwarder): the container itself has no network. Then runs the
# command (`pi --mode json ...`).
set -eu
mkdir -p "$PI_CODING_AGENT_DIR"
printf '%s' "$PI_MODELS_JSON" > "$PI_CODING_AGENT_DIR/models.json"
if [ -n "${PI_LLM_PORT:-}" ]; then
    i=0
    while [ ! -S /run/llm/llm.sock ] && [ "$i" -lt 50 ]; do sleep 0.2; i=$((i + 1)); done
    socat "TCP-LISTEN:${PI_LLM_PORT},bind=127.0.0.1,fork,reuseaddr" UNIX-CONNECT:/run/llm/llm.sock &
fi
exec "$@"
