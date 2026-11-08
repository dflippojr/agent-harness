#!/bin/sh
# Writes the models.json the driver passes in PI_MODELS_JSON (bakeoff/pi_driver.py: pi_env), so the task workspace
# stays the container's only mount, then runs the command (`pi --mode json ...`).
set -eu
mkdir -p "$PI_CODING_AGENT_DIR"
printf '%s' "$PI_MODELS_JSON" > "$PI_CODING_AGENT_DIR/models.json"
exec "$@"
