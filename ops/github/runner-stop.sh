#!/usr/bin/env bash
# ExecStop for a GitHub Actions runner service: lets the runner finish cancelling a job before the stop goes further (#814).
#
# The unit runs runsvc.sh under KillMode=mixed. A signal to runsvc.sh reaches bin/RunnerService.js, which always kills
# the listener with SIGKILL 30 seconds later, and KillMode=mixed then kills whatever is left. So a job's cancellation
# cleanup would get 30 seconds whatever TimeoutStopSec says.
#
# systemd runs this first, while nothing has been signalled. It sends SIGINT straight to Runner.Listener, the signal
# RunnerService.js would send, without that deadline, and waits. The listener cancels the job, waits for the worker and
# exits 0, which ends RunnerService.js and runsvc.sh too. If the grace period passes first, this returns and systemd
# goes on to signal runsvc.sh, so the 30-second kill and KillMode=mixed still end the stop.
#
# Keep TimeoutStopSec above RUNNER_STOP_GRACE_SECONDS (default 300). Recheck this when the pinned runner release changes.
set -uo pipefail

GRACE_SECONDS="${RUNNER_STOP_GRACE_SECONDS:-300}"
MAIN="${MAINPID:-}"

# Only below the unit's own main process, so another runner's listener under the same user is never signalled.
find_listener() {
  local children
  children="$(pgrep -d, -P "$MAIN")" || return 1
  pgrep -x -P "$children" Runner.Listener
}

main() {
  local listener waited=0
  # systemd also runs ExecStop after a main process that already exited, when MAINPID is empty.
  [[ "$MAIN" =~ ^[0-9]+$ ]] || return 0
  [[ "$GRACE_SECONDS" =~ ^[0-9]+$ ]] || GRACE_SECONDS=300
  listener="$(find_listener)" || return 0
  echo "runner-stop: sending SIGINT to Runner.Listener and waiting up to $GRACE_SECONDS seconds"
  # shellcheck disable=SC2086 # pgrep prints one pid per line, and each is a number.
  kill -INT $listener 2>/dev/null || return 0
  # shellcheck disable=SC2086
  while kill -0 $listener 2>/dev/null; do
    if [[ "$waited" -ge "$GRACE_SECONDS" ]]; then
      echo "runner-stop: Runner.Listener is still running after $GRACE_SECONDS seconds; the service stop takes over"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "runner-stop: Runner.Listener exited after about $waited seconds"
  return 0
}

main
