#!/bin/bash
# Installs or updates the Mac CLI and runner. Run as the logged-in user; no sudo.
# Fresh install from Settings:
#   curl -fsSL https://<server>/mac-client/install.sh | bash -s -- --server https://<server> --code hrp-...
# An unpacked package (or the legacy SSH deploy) works without downloads and preserves an existing pairing.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SERVER=""
CODE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --code) CODE="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

TEMP_SRC=""
cleanup() { [ -z "$TEMP_SRC" ] || rm -rf "$TEMP_SRC"; }
trap cleanup EXIT
if [ ! -f "$SRC/app/harness_runner.py" ]; then
  [ -n "$SERVER" ] || { echo "--server is required when installing without an unpacked package" >&2; exit 2; }
  TEMP_SRC="$(mktemp -d "${TMPDIR:-/tmp}/agent-harness-mac.XXXXXX")"
  curl -fsSL "${SERVER%/}/mac-client/package.tar.gz" | tar -xzf - -C "$TEMP_SRC"
  SRC="$TEMP_SRC"
fi

BASE="$HOME/.agent-harness"
VENV="$BASE/venv"
LABEL="dev.agent-harness.runner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$BASE/runner" "$BASE/client" "$BASE/workspaces" "$BASE/logs" "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
chmod 700 "$BASE" "$BASE/runner" "$BASE/client"

[ -x "$VENV/bin/python" ] || /usr/bin/python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check 'httpx>=0.27'

rm -rf "$BASE/runner/app.new"
mkdir -p "$BASE/runner/app.new/harness"
cp "$SRC/app/harness_runner.py" "$SRC/app/sandbox.sb" "$BASE/runner/app.new/"
cp "$SRC/app/harness/"*.py "$BASE/runner/app.new/harness/"
rm -rf "$BASE/runner/app"
mv "$BASE/runner/app.new" "$BASE/runner/app"
install -m 600 "$SRC/client/harness_cli.py" "$BASE/client/harness_cli.py"
install -m 644 "$SRC/client/harness_client.py" "$BASE/client/harness_client.py"
install -m 644 "$SRC/client/harness_compat.py" "$BASE/client/harness_compat.py"
install -m 644 "$SRC/client/harness_update.py" "$BASE/client/harness_update.py"
SITE_PACKAGES="$("$VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n' "$BASE/client" > "$SITE_PACKAGES/agent_harness_client.pth"

if [ -f "$SRC/config.json" ]; then
  install -m 600 "$SRC/config.json" "$BASE/runner/config.json"
fi
if [ -z "$SERVER" ] && [ -n "$CODE" ]; then
  echo "--server is required with --code" >&2
  exit 2
fi
if [ -n "$CODE" ]; then
  "$VENV/bin/python" "$BASE/client/harness_cli.py" --config "$BASE/client/config.json" \
    pair "$SERVER" "$CODE" --runner-config "$BASE/runner/config.json"
fi
[ -f "$BASE/runner/config.json" ] || { echo "missing $BASE/runner/config.json" >&2; exit 1; }
[ -f "$BASE/client/config.json" ] || cat > "$BASE/client/config.json" <<EOF
{"server": "$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["server"])' "$BASE/runner/config.json")", "token": ""}
EOF
chmod 600 "$BASE/client/config.json"

cat > "$HOME/.local/bin/harness" <<'EOF'
#!/bin/sh
exec "$HOME/.agent-harness/venv/bin/python" "$HOME/.agent-harness/client/harness_cli.py" "$@"
EOF
chmod 755 "$HOME/.local/bin/harness"

# Keep the log from growing without bound: start over once it passes 5 MB.
LOG="$BASE/logs/runner.log"
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 5000000 ]; then mv "$LOG" "$LOG.1"; fi

sed -e "s#__HOME__#$HOME#g" -e "s#__PYTHON__#$VENV/bin/python#g" "$SRC/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5; do  # bootout is asynchronous
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"

sleep 3
launchctl print "$DOMAIN/$LABEL" | grep -E '^\s+(state|pid|last exit code) =' || true
tail -n 5 "$LOG" 2>/dev/null || true

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Add $HOME/.local/bin to PATH to use the harness command." ;;
esac
