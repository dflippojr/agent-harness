#!/usr/bin/env bash
# Install agent-harness on Linux/NVIDIA or Apple Silicon macOS. Run from a repository checkout.
set -euo pipefail

UV_VERSION=0.12.14
LLAMA_BUILD=b10830
LLAMA_IMAGE="ghcr.io/ggml-org/llama.cpp:server-cuda-$LLAMA_BUILD"
QWEN_FILE=Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
QWEN_URL="https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/$QWEN_FILE"
GPT_OSS_FILE=gpt-oss-20b-MXFP4.gguf
GPT_OSS_URL="https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/$GPT_OSS_FILE"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_dir=$(dirname "$script_dir")
install_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/agent-harness
data_dir=
profile=auto
enable_modules=
model=auto
model_path=
existing_server=
instance=Main
port=8100
server_port=8090
no_start=0
dry_run=0
force=0

usage() {
    cat <<'EOF'
usage: install/install.sh [options]

  --install-dir DIR       tools, models, config, and logs
  --data-dir DIR          session database, workspaces, and backups
  --profile auto|full|service
                          full is Linux/NVIDIA only; macOS is service-only
  --enable-modules LIST   comma-separated service-profile modules
  --model auto|qwen|gpt-oss
  --model-path FILE       use an existing GGUF
  --existing-server URL   use an existing llama-server
  --instance NAME         service name suffix (default: Main)
  --port N                daemon port (default: 8100)
  --server-port N         llama-server port (default: 8090)
  --no-start              do not install or start systemd/launchd services
  --dry-run               validate choices and print actions only
  --force                 rewrite base config files
EOF
}

need_value() { [[ $# -ge 2 ]] || { echo "$1 needs a value" >&2; exit 64; }; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) need_value "$@"; install_dir=$2; shift 2 ;;
        --data-dir) need_value "$@"; data_dir=$2; shift 2 ;;
        --profile) need_value "$@"; profile=$2; shift 2 ;;
        --enable-modules) need_value "$@"; enable_modules=$2; shift 2 ;;
        --model) need_value "$@"; model=$2; shift 2 ;;
        --model-path) need_value "$@"; model_path=$2; shift 2 ;;
        --existing-server) need_value "$@"; existing_server=$2; shift 2 ;;
        --instance) need_value "$@"; instance=$2; shift 2 ;;
        --port) need_value "$@"; port=$2; shift 2 ;;
        --server-port) need_value "$@"; server_port=$2; shift 2 ;;
        --no-start) no_start=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        --force) force=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
    esac
done

os=${HARNESS_INSTALLER_OS:-$(uname -s)}
arch=${HARNESS_INSTALLER_ARCH:-$(uname -m)}
case "$profile" in auto|full|service) ;; *) echo "invalid profile: $profile" >&2; exit 64 ;; esac
case "$model" in auto|qwen|gpt-oss) ;; *) echo "invalid model: $model" >&2; exit 64 ;; esac
[[ $port =~ ^[0-9]+$ && $server_port =~ ^[0-9]+$ ]] || { echo "ports must be integers" >&2; exit 64; }
[[ $instance =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "instance may contain only letters, digits, dot, underscore, and dash" >&2; exit 64; }

case "$os/$arch" in
    Linux/x86_64|Linux/amd64) platform=linux ;;
    Darwin/arm64|Darwin/aarch64) platform=macos ;;
    *) echo "unsupported platform $os/$arch (supported: Linux x86_64, macOS Apple Silicon)" >&2; exit 1 ;;
esac

data_dir=${data_dir:-"$install_dir/data"}
config_dir="$install_dir/config"
log_dir="$install_dir/logs"
pause_flag="$install_dir/llama-server.paused"
profile_file="$config_dir/profile.yaml"
if [[ $profile == auto ]]; then
    if [[ -f $profile_file ]] && grep -Eq '^profile:[[:space:]]*service[[:space:]]*$' "$profile_file"; then
        profile=service
    elif [[ $platform == macos ]]; then
        profile=service
    else
        profile=full
    fi
fi

contains_module() {
    case ",$enable_modules," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}
requested_modules=()
if [[ -n $enable_modules ]]; then
    IFS=, read -r -a requested_modules <<<"$enable_modules"
    for requested_module in "${requested_modules[@]}"; do
        [[ -z $requested_module ]] && continue
        case "$requested_module" in
            local_model|homelab|memory_library|images|image_edit|jobs|gpu_guard|runners|remote_control|web|search|endpoint|notifications|backup) ;;
            *) echo "unknown module: $requested_module" >&2; exit 64 ;;
        esac
    done
fi
needs_local=0
[[ $profile == full ]] && needs_local=1
contains_module local_model && needs_local=1
for dependent in endpoint images image_edit gpu_guard; do contains_module "$dependent" && needs_local=1; done
contains_module image_edit && enable_modules=${enable_modules:+$enable_modules,}images

if [[ $platform == macos && $needs_local -eq 1 ]]; then
    echo "macOS Apple Silicon supports the hosted-provider service profile only; local_model is unavailable" >&2
    exit 1
fi
if [[ $platform == macos && $profile != service ]]; then
    echo "macOS Apple Silicon requires --profile service" >&2
    exit 1
fi

step() { printf '\n==> %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }
die() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
act() {
    description=$1
    shift
    if [[ $dry_run -eq 1 ]]; then info "[dry run] $description"; else info "$description"; "$@"; fi
}
download() {
    url=$1
    destination=$2
    [[ -f $destination ]] && { info "already downloaded: $destination"; return; }
    if [[ $dry_run -eq 1 ]]; then info "[dry run] download $url"; return; fi
    mkdir -p "$(dirname "$destination")"
    curl -fL --retry 5 --retry-delay 10 -C - -o "$destination.part" "$url" ||
        die "download failed: $url (run the installer again to resume)"
    mv -f "$destination.part" "$destination"
}
xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}
systemd_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g'
}

printf 'Agent harness installer (platform: %s, profile: %s, app: %s, install: %s)\n' \
    "$platform" "$profile" "$app_dir" "$install_dir"

step "Checking the machine"
if [[ $dry_run -eq 0 ]]; then
    for command in git docker curl tar; do have "$command" || die "$command is required"; done
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || die "Docker must be installed and running"
fi

vram_mib=0
ram_gib=0
if [[ $needs_local -eq 1 ]]; then
    [[ $platform == linux ]] || die "the full profile is supported only on Linux/NVIDIA"
    if [[ $dry_run -eq 0 ]]; then
        have nvidia-smi || die "nvidia-smi is required for the local_model module"
        gpu=$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits | head -n 1)
        IFS=, read -r gpu_name driver vram_mib <<<"$gpu"
        vram_mib=$(printf '%s' "$vram_mib" | tr -d ' ')
        driver=$(printf '%s' "$driver" | tr -d ' ')
        ram_gib=$(awk '/MemTotal/ {printf "%d", $2 / 1024 / 1024}' /proc/meminfo)
        info "GPU:$gpu_name, $((vram_mib / 1024)) GiB, driver $driver; RAM ${ram_gib} GiB"
        [[ ${driver%%.*} -ge 525 ]] || die "NVIDIA driver $driver is too old for the CUDA 12 image (525 or newer required)"
        [[ $vram_mib -ge 11776 || -n $existing_server ]] || die "at least 12 GB VRAM is required"
    else
        vram_mib=16384
        ram_gib=32
    fi
    if [[ $model == auto ]]; then
        if [[ $vram_mib -ge 15872 && $ram_gib -ge 30 ]]; then model=qwen; else model=gpt-oss; fi
    fi
else
    [[ $model == auto ]] && model=gpt-oss
fi

if [[ $model == qwen ]]; then
    model_name=qwen3.6-35b-a3b
    model_file=$QWEN_FILE
    model_url=$QWEN_URL
    context=65536
    llama_args=(--fit on --cache-type-k q8_0 --cache-type-v q8_0)
else
    model_name=gpt-oss-20b
    model_file=$GPT_OSS_FILE
    model_url=$GPT_OSS_URL
    context=32768
    llama_args=(--n-gpu-layers 99 --reasoning-effort medium)
fi
[[ $needs_local -eq 1 ]] && info "local model: $model_name" || info "local model: disabled (hosted-provider service profile)"
if [[ $dry_run -eq 0 ]]; then
    mkdir -p "$install_dir"
    free_gib=$(df -Pk "$install_dir" | awk 'NR == 2 {printf "%d", $4 / 1024 / 1024}')
    need_gib=5
    if [[ $needs_local -eq 1 && -z $model_path && -z $existing_server ]]; then
        [[ $model == qwen ]] && need_gib=28 || need_gib=18
    fi
    if contains_module image_edit; then
        need_gib=$((need_gib + 22))
        if [[ $needs_local -eq 1 ]]; then
            [[ $vram_mib -ge 15872 ]] || die "image_edit needs about 16 GB of VRAM"
            [[ $ram_gib -ge 30 ]] || info "warning: image_edit was tested with 32 GB RAM; this machine reports ${ram_gib} GiB"
        fi
    fi
    [[ $free_gib -ge $need_gib ]] || die "only $free_gib GiB free at $install_dir; about $need_gib GiB is required"
    info "disk: $free_gib GiB free (need about $need_gib GiB)"
fi

step "Python environment (uv $UV_VERSION)"
bin_dir="$install_dir/bin"
uv="$bin_dir/uv"
venv="$install_dir/venv"
python="$venv/bin/python"
if [[ ! -x $uv ]]; then
    if [[ $platform == linux ]]; then uv_target=x86_64-unknown-linux-gnu; else uv_target=aarch64-apple-darwin; fi
    uv_archive="$install_dir/downloads/uv-$UV_VERSION-$uv_target.tar.gz"
    download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_target.tar.gz" "$uv_archive"
    if [[ $dry_run -eq 0 ]]; then
        mkdir -p "$bin_dir"
        tar -xzf "$uv_archive" -C "$bin_dir" --strip-components=1
    fi
fi
if [[ $dry_run -eq 1 ]]; then
    info "[dry run] create $venv with Python 3.12 and install requirements"
else
    UV_PYTHON_INSTALL_DIR="$install_dir/python" "$uv" venv --python 3.12 --allow-existing "$venv"
    "$uv" pip install --python "$python" -r "$app_dir/requirements.txt"
fi

if [[ $needs_local -eq 1 && -z $existing_server ]]; then
    step "llama.cpp $LLAMA_BUILD CUDA image"
    if [[ $dry_run -eq 1 ]]; then
        info "[dry run] pull and validate $LLAMA_IMAGE with NVIDIA Container Toolkit"
    else
        docker pull "$LLAMA_IMAGE"
        docker run --rm --gpus all "$LLAMA_IMAGE" --version >/dev/null ||
            die "Docker cannot use the NVIDIA GPU; install NVIDIA Container Toolkit and configure Docker"
    fi
    step "Model $model_name"
    if [[ -n $model_path ]]; then
        [[ $dry_run -eq 1 || -f $model_path ]] || die "model file not found: $model_path"
        info "using $model_path"
    else
        model_path="$install_dir/models/$model_file"
        download "$model_url" "$model_path"
    fi
fi

step "Docker sandbox image"
if [[ $dry_run -eq 1 ]]; then
    info "[dry run] docker build -t agent-harness-sandbox:py312 sandbox"
else
    docker build -q -t agent-harness-sandbox:py312 "$app_dir/sandbox"
fi
if [[ $profile == service ]]; then
    step "Hosted-provider CLI image and egress proxies"
    if [[ $dry_run -eq 1 ]]; then
        info "[dry run] build agent-harness-cli:1 and start provider allowlist proxies"
    else
        docker build -q -t agent-harness-cli:1 -f "$app_dir/sandbox/cli.Dockerfile" "$app_dir/sandbox"
        docker network inspect harness-egress >/dev/null 2>&1 || docker network create harness-egress >/dev/null
        docker compose -f "$app_dir/ops/egress/compose.yaml" up -d --build
    fi
fi

step "Configuration"
server_url=${existing_server:-"http://127.0.0.1:$server_port"}
if [[ $dry_run -eq 1 ]]; then
    info "[dry run] write config in $config_dir"
else
    mkdir -p "$config_dir" "$data_dir" "$log_dir" "$bin_dir"
    config_args=(-m harness.setup_config --config-dir "$config_dir" --data-dir "$data_dir" --model "$model"
        --profile "$profile" --port "$port" --llama-url "$server_url" --pause-flag "$pause_flag")
    if [[ -n $enable_modules ]]; then
        for module in "${requested_modules[@]}"; do
            [[ -n $module ]] && config_args+=(--enable-module "$module")
        done
    fi
    [[ -n $existing_server || $needs_local -eq 0 ]] && config_args+=(--no-gpu-guard)
    [[ $force -eq 1 ]] && config_args+=(--force)
    (cd "$app_dir" && "$python" "${config_args[@]}")
    install -m 0755 "$script_dir/run-daemon.sh" "$bin_dir/run-daemon.sh"
    [[ $needs_local -eq 1 && -z $existing_server ]] && install -m 0755 "$script_dir/run-server.sh" "$bin_dir/run-server.sh"
fi

if contains_module image_edit; then
    step "Optional image-edit component (Qwen-Image-Edit, Apache 2.0)"
    edit_rev=7d41107b653d3039be20972fb82398b01b3213eb
    edit_sha=393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021
    edit_url="https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/$edit_rev/split_files/diffusion_models/qwen_image_edit_fp8_e4m3fn.safetensors"
    models_root="$install_dir/comfy-models"
    edit_dest="$models_root/diffusion_models/qwen_image_edit_fp8_e4m3fn.safetensors"
    info "artifact sha256 $edit_sha (revision $edit_rev)"
    if [[ $dry_run -eq 1 ]]; then
        info "[dry run] download $edit_url"
    else
        download "$edit_url" "$edit_dest"
        got=$(sha256sum "$edit_dest" | awk '{print $1}')
        [[ $got == "$edit_sha" ]] || die "image-edit checksum mismatch: got $got want $edit_sha"
        info "verified $edit_dest"
    fi
fi

safe_instance=$(printf '%s' "$instance" | tr '[:upper:]' '[:lower:]')
if [[ $no_start -eq 0 ]]; then
    step "Per-user startup services"
    if [[ $dry_run -eq 1 ]]; then
        [[ $platform == linux ]] && info "[dry run] install systemd --user units for $instance" || info "[dry run] install launchd agent for $instance"
    elif [[ $platform == linux ]]; then
        unit_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
        mkdir -p "$unit_dir"
        daemon_unit="agent-harness-$safe_instance-daemon.service"
        cat >"$unit_dir/$daemon_unit" <<EOF
[Unit]
Description=Agent harness daemon ($instance)
After=default.target

[Service]
Type=simple
WorkingDirectory="$(systemd_escape "$app_dir")"
ExecStart=/bin/bash "$(systemd_escape "$bin_dir/run-daemon.sh")" "$(systemd_escape "$app_dir")" "$(systemd_escape "$config_dir")" "$(systemd_escape "$python")" "$(systemd_escape "$log_dir")"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
        units=("$daemon_unit")
        if [[ $needs_local -eq 1 && -z $existing_server ]]; then
            server_unit="agent-harness-$safe_instance-llama.service"
            extra_line=
            for llama_arg in "${llama_args[@]}"; do
                extra_line="$extra_line \"$(systemd_escape "$llama_arg")\""
            done
            cat >"$unit_dir/$server_unit" <<EOF
[Unit]
Description=Agent harness llama.cpp server ($instance)

[Service]
Type=simple
Environment="HARNESS_LOG_DIR=$(systemd_escape "$log_dir")"
ExecStart=/bin/bash "$(systemd_escape "$bin_dir/run-server.sh")" "$(systemd_escape "$instance")" "$LLAMA_IMAGE" "$(systemd_escape "$model_path")" "$model_name" "$server_port" "$context" "$(systemd_escape "$pause_flag")"$extra_line
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
EOF
            units=("$server_unit" "$daemon_unit")
        fi
        systemctl --user daemon-reload
        systemctl --user enable --now "${units[@]}"
    else
        agents_dir="$HOME/Library/LaunchAgents"
        mkdir -p "$agents_dir"
        label="com.agent-harness.$safe_instance.daemon"
        plist="$agents_dir/$label.plist"
        cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$(xml_escape "$label")</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$(xml_escape "$bin_dir/run-daemon.sh")</string>
    <string>$(xml_escape "$app_dir")</string><string>$(xml_escape "$config_dir")</string>
    <string>$(xml_escape "$python")</string><string>$(xml_escape "$log_dir")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$app_dir")</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(xml_escape "$log_dir/launchd.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$log_dir/launchd.log")</string>
</dict></plist>
EOF
        launchctl bootout "gui/$UID" "$plist" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$UID" "$plist"
    fi
fi

step "Checking the install"
if [[ $dry_run -eq 1 ]]; then
    info "[dry run] skipped python -m harness.doctor"
else
    if [[ $no_start -eq 0 ]]; then
        daemon_ready=0
        for attempt in {1..30}; do
            if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
                daemon_ready=1
                break
            fi
            sleep 2
        done
        if [[ $daemon_ready -eq 1 ]]; then
            info "daemon ready on port $port"
        else
            info "daemon did not become ready within 60 seconds; doctor will report details"
        fi
    fi
    doctor_args=(-m harness.doctor --config-dir "$config_dir")
    [[ $no_start -eq 0 ]] && doctor_args+=(--instance "$instance")
    [[ -n $existing_server || $needs_local -eq 0 ]] && doctor_args+=(--existing-server)
    (cd "$app_dir" && "$python" "${doctor_args[@]}")
fi

printf '\nDone.\n  Web app: http://127.0.0.1:%s\n  Config:  %s\n  Logs:    %s\n' "$port" "$config_dir" "$log_dir"
if [[ $profile == service ]]; then
    printf '  Next:    ops/backends/login.sh claude|codex|cursor\n'
fi
if [[ $no_start -eq 1 ]]; then
    printf '  Start:   HARNESS_CONFIG_DIR=%q %q -m harness  (from %s)\n' "$config_dir" "$python" "$app_dir"
fi
