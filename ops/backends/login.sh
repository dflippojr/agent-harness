#!/usr/bin/env bash
# Log a hosted-provider CLI in inside its isolated Docker volume.
#
# Claude Code and Cursor keep the credential apart from their state, so one login (harness-login-<backend>) serves
# the owner's sessions and every App. Codex can't: the owner's login is harness-auth-codex and each App needs its own
# (`login.sh codex --app <app id>`, #371); Codex stays unavailable to an App until then.
set -euo pipefail

usage() {
    echo "usage: $0 claude|codex|cursor [--app APP_ID] [--status|--logout] [--image IMAGE]" >&2
    exit 64
}

[[ $# -ge 1 ]] || usage
backend=$1
shift
mode=login
image=agent-harness-cli:1
app=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) shift; [[ $# -gt 0 ]] || usage; app=$1 ;;
        --status) mode=status ;;
        --logout) mode=logout ;;
        --image) shift; [[ $# -gt 0 ]] || usage; image=$1 ;;
        *) usage ;;
    esac
    shift
done

# auth_dir: where the login volume is mounted. Claude and Cursor get a throwaway state directory in the container.
case "$backend" in
    claude)
        auth_dir=/home/agent/.claude-login
        env_args=(-e CLAUDE_CONFIG_DIR=/home/agent/.claude -e CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude-login)
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
        # Cursor's login is $XDG_CONFIG_HOME/cursor/auth.json (~/.config/cursor).
        auth_dir=/home/agent/.config/cursor
        env_args=(-e NO_OPEN_BROWSER=1)
        login_cmd=(agent login)
        status_cmd=(agent status)
        logout_cmd=(agent logout)
        ;;
    *) usage ;;
esac

if [[ -n $app ]]; then
    if [[ $backend != codex ]]; then
        echo "$backend has one login for every App; run it without --app" >&2
        exit 64
    fi
    # The daemon's App ids (k-<hex>) are their own volume name part (harness/cli_domains.py domain_slug).
    if [[ ! $app =~ ^[a-z0-9][a-z0-9_.-]{0,47}$ || $app =~ ^h-[0-9a-f]{24}$ ]]; then
        echo "not an App id: $app" >&2
        exit 64
    fi
    volume="harness-cli-codex-app-$app"
elif [[ $backend == codex ]]; then
    volume=harness-auth-codex
else
    volume="harness-login-$backend"
fi

docker image inspect "$image" >/dev/null 2>&1 || {
    echo "image $image not found; run install/install.sh --profile service first" >&2
    exit 1
}
[[ $(docker inspect -f '{{.State.Running}}' "harness-egress-$backend" 2>/dev/null || true) == true ]] || {
    echo "proxy harness-egress-$backend is not running" >&2
    exit 1
}
docker volume create "$volume" >/dev/null
# A volume mounted where the image has no directory starts out root's; hand it to the agent user.
docker run --rm --network none --user 0:0 -v "$volume:/login" "$image" chown 1000:1000 /login

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
    "${env_args[@]}" -v "$volume:$auth_dir" "$image" "${command[@]}"
