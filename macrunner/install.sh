#!/bin/bash
# Installs or updates the agent-harness runner from an unpacked deploy directory (see ops/macbook/deploy.ps1)
# and (re)starts its launchd agent. Run on the Mac as the logged-in user; no sudo.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BASE="$HOME/.agent-harness"
LABEL="dev.agent-harness.runner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$BASE/runner" "$BASE/workspaces" "$BASE/logs" "$HOME/Library/LaunchAgents"
chmod 700 "$BASE" "$BASE/runner"

rm -rf "$BASE/runner/app.new"
mkdir -p "$BASE/runner/app.new/harness"
cp "$SRC/app/harness_runner.py" "$SRC/app/sandbox.sb" "$BASE/runner/app.new/"
cp "$SRC/app/harness/"*.py "$BASE/runner/app.new/harness/"
rm -rf "$BASE/runner/app"
mv "$BASE/runner/app.new" "$BASE/runner/app"

if [ -f "$SRC/config.json" ]; then
  install -m 600 "$SRC/config.json" "$BASE/runner/config.json"
fi
[ -f "$BASE/runner/config.json" ] || { echo "missing $BASE/runner/config.json" >&2; exit 1; }

# Keep the log from growing without bound: start over once it passes 5 MB.
LOG="$BASE/logs/runner.log"
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 5000000 ]; then mv "$LOG" "$LOG.1"; fi

sed "s#__HOME__#$HOME#g" "$SRC/$LABEL.plist" > "$PLIST"
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
