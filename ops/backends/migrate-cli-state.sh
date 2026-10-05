#!/usr/bin/env bash
# One-time owner step for per-domain CLI state (#371); see migrate-cli-state.ps1 for what moves and why.
#   ops/backends/migrate-cli-state.sh --check   # report what would move, change nothing
#   ops/backends/migrate-cli-state.sh           # move the Claude and Cursor logins into harness-login-<backend>
set -euo pipefail

mode=move
image=agent-harness-cli:1
prefix=harness  # volume name prefix; only tests change it
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) mode=check ;;
        --image) shift; image=$1 ;;
        --prefix) shift; prefix=$1 ;;
        *) echo "usage: $0 [--check] [--image IMAGE]" >&2; exit 64 ;;
    esac
    shift
done

# Runs as root in a throwaway container with the old volume at /old and the login volume at /new.
script='set -e; mode=$1; backend=$2; old=$3; new=$4; src=/old/$5
if [ -e $src ]; then
  if [ $mode = check ]; then echo $backend: would move $old/$5 to $new; exit 0; fi
  if [ -d $src ]; then cp -a $src/. /new/; else cp -p $src /new/; fi
  chown -R 1000:1000 /new; rm -rf $src; echo $backend: moved $old/$5 to $new
elif ls -A /new | grep -q .; then echo $backend: already moved to $new
else echo $backend: no login in $old, log in with ops/backends/login.sh $backend
fi
[ $mode = check ] || chown 1000:1000 /new'

has_volume() { docker volume ls -q | grep -qx "$1"; }

docker image inspect "$image" >/dev/null 2>&1 || { echo "image $image not found" >&2; exit 1; }
failed=0
for spec in "claude .credentials.json" "cursor home/.config/cursor"; do
    read -r backend path <<<"$spec"
    old="$prefix-auth-$backend"
    new="$prefix-login-$backend"
    if ! has_volume "$old"; then echo "$backend: no $old volume, nothing to move"; continue; fi
    if [[ $mode == check ]]; then
        volumes=(-v "$old:/old:ro")
        if has_volume "$new"; then volumes+=(-v "$new:/new:ro"); else volumes+=(--mount type=tmpfs,target=/new); fi
    else
        docker volume create "$new" >/dev/null
        volumes=(-v "$old:/old" -v "$new:/new")
    fi
    docker run --rm --network none --user 0:0 "${volumes[@]}" "$image" sh -c "$script" sh "$mode" "$backend" \
        "$old" "$new" "$path" || { failed=1; echo "$backend: the move failed; check $old and $new" >&2; }
done
exit $failed
