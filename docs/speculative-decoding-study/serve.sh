#!/usr/bin/env bash
# #772: one throwaway llama-server container on 127.0.0.1:8081 with the production flags, ctx 32768 (a later
# --ctx-size in the extra args overrides it). Usage: serve.sh NAME LOGFILE [extra llama-server args...]
# MODELS_DIR holds the main GGUF; DRAFTERS_DIR is mounted at /drafters for -md.
name=$1; log=$2; shift 2
port=8081
repo=$(cd "$(dirname "$0")/../.." && pwd)
models=${MODELS_DIR:-$HOME/.local/share/agent-harness/models}
drafters=${DRAFTERS_DIR:-$repo/.tmp/models}
docker rm -f "ah772-$name" >/dev/null 2>&1
# The image's HEALTHCHECK probes :8080, not this port (#858), so probe the port the server listens on. A --port in the
# extra args would move the server but not this probe.
exec docker run --rm --name "ah772-$name" --gpus all --network host \
  --health-cmd "curl -f http://127.0.0.1:$port/health" --health-start-period 300s \
  -v "$models:/models:ro" -v "$drafters:/drafters:ro" \
  ghcr.io/ggml-org/llama.cpp:server-cuda-b10830 \
  -m /models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --alias qwen3.6-35b-a3b --host 127.0.0.1 --port "$port" \
  --ctx-size 32768 --flash-attn on --parallel 1 --jinja --metrics \
  --fit on --cache-type-k q8_0 --cache-type-v q8_0 "$@" >"$log" 2>&1
