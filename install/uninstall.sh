#!/usr/bin/env bash
# Remove Unix per-user services. Data is preserved unless --remove-files is explicit.
set -euo pipefail

install_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/agent-harness
instance=Main
remove_files=0
dry_run=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) [[ $# -ge 2 ]] || exit 64; install_dir=$2; shift 2 ;;
        --instance) [[ $# -ge 2 ]] || exit 64; instance=$2; shift 2 ;;
        --remove-files) remove_files=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) echo "usage: $0 [--install-dir DIR] [--instance NAME] [--remove-files] [--dry-run]"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 64 ;;
    esac
done
[[ $instance =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "invalid instance" >&2; exit 64; }
safe_instance=$(printf '%s' "$instance" | tr '[:upper:]' '[:lower:]')

# Keep the daemon available for host-only release; remove only an installer-owned Hub.
if [[ $dry_run -eq 1 ]]; then
    echo "[dry run] check harness hub status; harness hub release --confirm before removing daemon"
    echo "[dry run] remove the recorded Hub service/container and its dedicated venv/state; preserve other Hubs"
    echo "[dry run] remove daemon services; remove files: $remove_files"
    exit 0
fi
[[ -n $install_dir ]] || { echo "empty install directory" >&2; exit 64; }
if [[ -d $install_dir ]]; then
    install_dir=$(CDPATH= cd -- "$install_dir" && pwd -L)
elif [[ $install_dir != /* ]]; then
    install_dir="$PWD/$install_dir"
fi
if [[ $remove_files -eq 1 ]]; then
    home_real=$(CDPATH= cd -- "$HOME" && pwd -P)
    case "$install_dir" in
        ""|/|"$HOME"|"$home_real") echo "refusing unsafe install directory: $install_dir" >&2; exit 1 ;;
    esac
    if [[ -d $install_dir ]]; then
        install_real=$(CDPATH= cd -- "$install_dir" && pwd -P)
        case "$install_real" in
            /|"$home_real") echo "refusing unsafe install directory: $install_dir" >&2; exit 1 ;;
        esac
    fi
fi
python="$install_dir/venv/bin/python"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_dir=$(dirname "$script_dir")
if [[ -x $python ]]; then
    (cd "$app_dir" && "$python" -m harness.install_hub uninstall --install-dir "$install_dir" --config-dir "$install_dir/config")
elif [[ -f $install_dir/hub-install.json ]]; then
    echo "restore the daemon Python environment to release and remove the installed Hub first" >&2
    exit 1
fi

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
    rm -rf -- "$install_dir"
    echo "removed services and $install_dir"
else
    echo "removed services; kept $install_dir"
fi
