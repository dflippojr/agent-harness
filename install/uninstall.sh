#!/usr/bin/env bash
# Remove Unix per-user services. Data is preserved unless --remove-files is explicit.
set -euo pipefail

install_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/agent-harness
instance=Main
remove_files=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) [[ $# -ge 2 ]] || exit 64; install_dir=$2; shift 2 ;;
        --instance) [[ $# -ge 2 ]] || exit 64; instance=$2; shift 2 ;;
        --remove-files) remove_files=1; shift ;;
        -h|--help) echo "usage: $0 [--install-dir DIR] [--instance NAME] [--remove-files]"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 64 ;;
    esac
done
[[ $instance =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "invalid instance" >&2; exit 64; }
safe_instance=$(printf '%s' "$instance" | tr '[:upper:]' '[:lower:]')

case $(uname -s) in
    Linux)
        unit_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
        units=("agent-harness-$safe_instance-daemon.service" "agent-harness-$safe_instance-llama.service")
        systemctl --user disable --now "${units[@]}" >/dev/null 2>&1 || true
        for unit in "${units[@]}"; do rm -f "$unit_dir/$unit"; done
        systemctl --user daemon-reload
        ;;
    Darwin)
        plist="$HOME/Library/LaunchAgents/com.agent-harness.$safe_instance.daemon.plist"
        launchctl bootout "gui/$UID" "$plist" >/dev/null 2>&1 || true
        rm -f "$plist"
        ;;
    *) echo "unsupported platform" >&2; exit 1 ;;
esac

if [[ $remove_files -eq 1 ]]; then
    case "$install_dir" in
        ""|/|"$HOME") echo "refusing unsafe install directory: $install_dir" >&2; exit 1 ;;
    esac
    rm -rf -- "$install_dir"
    echo "removed services and $install_dir"
else
    echo "removed services; kept $install_dir"
fi
