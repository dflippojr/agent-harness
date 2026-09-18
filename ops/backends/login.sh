#!/usr/bin/env bash
# Log a hosted-provider CLI in inside its isolated Docker volume.
set -euo pipefail

usage() {
    echo "usage: $0 claude|codex|cursor [--status|--logout] [--image IMAGE]" >&2
    exit 64
}

[[ $# -ge 1 ]] || usage
backend=$1
shift
mode=login
image=agent-harness-cli:1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --status) mode=status ;;
        --logout) mode=logout ;;
        --image) shift; [[ $# -gt 0 ]] || usage; image=$1 ;;
        *) usage ;;
    esac
    shift
done

case "$backend" in
    claude)
        auth_dir=/home/agent/.claude
        env_args=(-e CLAUDE_CONFIG_DIR=/home/agent/.claude)
        login_cmd=(claude auth login --claudeai)
        status_cmd=(claude auth status)
        logout_cmd=(claude auth logout)
        ;;
    codex)
        auth_dir=/home/agent/.codex
        env_args=(-e CODEX_HOME=/home/agent/.codex)
        login_cmd=(codex login --device-auth)
        status_cmd=(codex login status)
        logout_cmd=(codex logout)
        ;;
    cursor)
        auth_dir=/home/agent/.cursor
        env_args=(-e NO_OPEN_BROWSER=1 -e HOME=/home/agent/.cursor/home -e CURSOR_CONFIG_DIR=/home/agent/.cursor/config)
        login_cmd=(agent login)
        status_cmd=(agent status)
        logout_cmd=(agent logout)
        ;;
    *) usage ;;
esac

docker image inspect "$image" >/dev/null 2>&1 || {
    echo "image $image not found; run install/install.sh --profile service first" >&2
    exit 1
}
[[ $(docker inspect -f '{{.State.Running}}' "harness-egress-$backend" 2>/dev/null || true) == true ]] || {
    echo "proxy harness-egress-$backend is not running" >&2
    exit 1
}
docker volume create "harness-auth-$backend" >/dev/null

tty_args=(-it)
command=("${login_cmd[@]}")
if [[ $mode == status ]]; then
    tty_args=()
    command=("${status_cmd[@]}")
elif [[ $mode == logout ]]; then
    command=("${logout_cmd[@]}")
fi

exec docker run --rm "${tty_args[@]}" --network "harness-cli-$backend" \
    -e "HTTPS_PROXY=http://harness-egress-$backend:8888" \
    -e "HTTP_PROXY=http://harness-egress-$backend:8888" -e NO_PROXY=localhost,127.0.0.1 \
    "${env_args[@]}" -v "harness-auth-$backend:$auth_dir" "$image" "${command[@]}"
