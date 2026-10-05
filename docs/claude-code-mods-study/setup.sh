#!/usr/bin/env bash
# Throwaway study environment for #306: an internal network with no route out, the stub Messages API, the relay stub
# and a fresh state volume. `setup.sh down` removes all of it. Never touches harness-* containers or volumes.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
py=agent-harness-sandbox:py312

if [ "${1:-up}" = down ]; then
  docker rm -f modstudy-claude modstudy-relay modstudy-api >/dev/null 2>&1 || true
  docker volume rm modstudy-state >/dev/null 2>&1 || true
  docker network rm modstudy-net >/dev/null 2>&1 || true
  exit 0
fi

docker network create --internal modstudy-net >/dev/null
docker run -d --name modstudy-api --network modstudy-net --network-alias modstudy-api \
  --mount "type=bind,source=$here/stub_api.py,target=/stub/stub_api.py,readonly" \
  "$py" python -u /stub/stub_api.py 8080 >/dev/null
docker run -d --name modstudy-relay --network modstudy-net \
  --mount "type=bind,source=$here/stub_relay.py,target=/stub/stub_relay.py,readonly" \
  "$py" python -u /stub/stub_relay.py >/dev/null
# As cli_domains.prepare_command does: the state volume's root must be the agent user's.
docker run --rm --network none --user 0:0 -v modstudy-state:/state "$py" chown 1000:1000 /state
echo "up"
