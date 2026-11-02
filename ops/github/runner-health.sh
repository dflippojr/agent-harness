#!/usr/bin/env bash
# Checks the tower's deploy runner and raises an ntfy alert when it stops working (#814).
#
# A dead deploy runner means no deploys, and nothing else reports it. The service being active isn't enough: in the
# incident behind this script the runner looked registered and was dead. So this asks both systemd and GitHub.
#
#   healthy           the service is active and GitHub reports the runner online
#   service-inactive  the service is anything but active
#   runner-offline    GitHub reports the runner offline, or doesn't list it
#   cannot-tell       systemd or GitHub can't answer: a check that can't run must not read as a healthy runner
#
# One outage raises one alert and its end raises one notice. The state file keeps three things, saved after every check:
# what that check observed, what ntfy last accepted a notice for, and whether the current occurrence's notice is still
# pending. A change of state starts a new occurrence, and only ntfy accepting clears its pending notice. So a refused
# post is sent again on the next check, whatever was accepted for an earlier outage of the same kind.
#
# runner-health.timer runs it. Settings come from the environment (runner-health.service's EnvironmentFile):
#
#   RUNNER_HEALTH_NTFY_SERVER      ntfy server URL (the daemon's notify.server)
#   RUNNER_HEALTH_NTFY_TOPIC       ntfy topic (notify.topic)
#   RUNNER_HEALTH_NTFY_TOKEN_FILE  optional file holding the ntfy write token (notify.token_file)
#   RUNNER_HEALTH_GH_TOKEN_FILE    file holding a GitHub token with the repository's Administration: read permission
#   RUNNER_HEALTH_REPO, RUNNER_HEALTH_RUNNER, RUNNER_HEALTH_SERVICE, RUNNER_HEALTH_STATE_FILE override the defaults below
#
# Tokens stay out of the output and every command line. gh gets its token in its own environment, curl gets ntfy's
# through --config on standard input, and nothing gh prints reaches the output or an alert.
#
# Exit codes: 0 the check ran (whatever it found) and any alert was delivered, 1 ntfy didn't accept an alert,
# 2 the settings are invalid. A non-zero exit fails the unit, which `systemctl --user --failed` shows.
set -uo pipefail

REPO="${RUNNER_HEALTH_REPO:-dflippojr/agent-harness}"
RUNNER="${RUNNER_HEALTH_RUNNER:-dflippotower-linux-agent-harness}"
SERVICE="${RUNNER_HEALTH_SERVICE:-github-runner-agent-harness.service}"
NTFY_SERVER="${RUNNER_HEALTH_NTFY_SERVER:-}"
NTFY_TOPIC="${RUNNER_HEALTH_NTFY_TOPIC:-}"
NTFY_TOKEN_FILE="${RUNNER_HEALTH_NTFY_TOKEN_FILE:-}"
GH_TOKEN_FILE="${RUNNER_HEALTH_GH_TOKEN_FILE:-}"
STATE_FILE="${RUNNER_HEALTH_STATE_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-harness/runner-health.state}"

# What the state file holds: the last check's state, the last state ntfy accepted a notice for, and whether the notice
# for the current occurrence is still to be accepted.
observed=healthy
delivered=healthy
pending=no
# What the current check found, and the alert for it.
state=""
title=""
message=""
tags=""
priority=4

invalid() {
  echo "runner-health: invalid or missing $1" >&2
  exit 2
}

# These go into a jq filter, a JSON body and command lines, so each one is held to characters that mean nothing there.
validate_settings() {
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || invalid RUNNER_HEALTH_REPO
  [[ "$RUNNER" =~ ^[A-Za-z0-9_.-]+$ ]] || invalid RUNNER_HEALTH_RUNNER
  [[ "$SERVICE" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service$ ]] || invalid RUNNER_HEALTH_SERVICE
  [[ "$NTFY_TOPIC" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || invalid RUNNER_HEALTH_NTFY_TOPIC
  # No credentials in the URL: it goes on curl's command line.
  [[ "$NTFY_SERVER" =~ ^https?://[A-Za-z0-9._~:/-]+$ ]] || invalid RUNNER_HEALTH_NTFY_SERVER
  return 0
}

# Prints a token file's first line, or fails. The check on its characters keeps a damaged file from adding options to
# curl's configuration, and it never prints what it refused.
read_token() {
  local file="$1" token=""
  [[ -n "$file" ]] && [[ -r "$file" ]] && [[ -f "$file" ]] || return 1
  IFS= read -r token < "$file" || [[ -n "$token" ]] || return 1
  token="${token//[[:space:]]/}"
  [[ "$token" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  printf '%s' "$token"
}

found() {
  state="$1"
  title="$2"
  message="$3"
  tags="$4"
}

cannot_tell() {
  found cannot-tell "Deploy runner: can't tell if it is healthy" "$1" question
}

# Sets the state when the service isn't active, and leaves it empty when the runner should be asked about.
check_service() {
  local active
  active="$(timeout 20 systemctl --user is-active "$SERVICE" 2>/dev/null)"
  case "$active" in
    active) ;;
    inactive|failed|activating|deactivating)
      found service-inactive "Deploy runner: service not running" \
        "$SERVICE is $active on the tower, so deploys are not running." warning ;;
    *) cannot_tell "systemctl --user can't report $SERVICE." ;;
  esac
  return 0
}

check_github() {
  local token errors status rc
  if ! token="$(read_token "$GH_TOKEN_FILE")"; then
    cannot_tell "RUNNER_HEALTH_GH_TOKEN_FILE is missing, unreadable or malformed, so GitHub can't be asked about $RUNNER."
    return 0
  fi
  errors="$(mktemp)" || { cannot_tell "No temporary file for asking GitHub about $RUNNER."; return 0; }
  status="$(GH_TOKEN="$token" timeout 60 gh api --paginate "repos/$REPO/actions/runners?per_page=100" \
    --jq ".runners[] | select(.name == \"$RUNNER\") | .status" 2>"$errors")"
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    # gh's own words stay in this file. They choose between two fixed messages and go no further.
    if grep -Eq 'HTTP 40[13]' "$errors"; then
      cannot_tell "GitHub refused the token (401 or 403), so it can't report $RUNNER. Check that the token is current."
    else
      cannot_tell "GitHub is unreachable or failing, so it can't report $RUNNER."
    fi
    rm -f -- "$errors"
    return 0
  fi
  rm -f -- "$errors"
  case "$status" in
    online) found healthy "Deploy runner recovered" "$SERVICE is active and GitHub reports $RUNNER online." white_check_mark ;;
    offline) found runner-offline "Deploy runner offline" "GitHub reports $RUNNER offline, so deploys are not running." warning ;;
    "") found runner-offline "Deploy runner offline" "$RUNNER is not registered with $REPO, so deploys are not running." warning ;;
    *) cannot_tell "GitHub gave an unexpected answer about $RUNNER." ;;
  esac
  return 0
}

notify() {
  local token="" curl_config="" answer
  if [[ -n "$NTFY_TOKEN_FILE" ]]; then
    token="$(read_token "$NTFY_TOKEN_FILE")" || return 1
    curl_config="header = \"Authorization: Bearer $token\""
  fi
  # Every value here is a fixed string or passed validate_settings. -q must come first: it stops curl reading the user's
  # .curlrc, where a trace setting would log the token and a location setting would follow a redirect. Redirects stay off,
  # so the token goes nowhere else, and only a 2xx counts: --fail lets a 3xx through, and an unfollowed one published nothing.
  answer="$(printf '%s\n' "$curl_config" | timeout 60 curl -q --config - --silent --fail --output /dev/null \
    --write-out '%{http_code}' --max-time 20 --retry 2 --proto '=https,http' \
    --header 'Content-Type: application/json' --data-raw \
    "{\"topic\":\"$NTFY_TOPIC\",\"title\":\"$title\",\"message\":\"$message\",\"priority\":$priority,\"tags\":[\"$tags\"]}" \
    "$NTFY_SERVER" 2>/dev/null)" || return 1
  [[ "$answer" =~ ^2[0-9][0-9]$ ]]
}

# Reads the state file. The file is replaced whole, so one missing a field isn't ours to trust: all three then read as
# their defaults, under which any outage is a new one and alerts.
load_state() {
  local key value fields=0
  [[ -r "$STATE_FILE" ]] || return 0
  while IFS='=' read -r key value; do
    case "$key=$value" in
      observed=healthy|observed=service-inactive|observed=runner-offline|observed=cannot-tell) observed="$value" ;;
      delivered=healthy|delivered=service-inactive|delivered=runner-offline|delivered=cannot-tell) delivered="$value" ;;
      pending=yes|pending=no) pending="$value" ;;
      *) continue ;;
    esac
    fields=$((fields + 1))
  done < "$STATE_FILE"
  if [[ "$fields" -ne 3 ]]; then
    observed=healthy
    delivered=healthy
    pending=no
  fi
  return 0
}

save_state() {
  local dir partial
  dir="$(dirname -- "$STATE_FILE")"
  mkdir -p -m 0700 -- "$dir" || return 1
  partial="$(mktemp "$dir/.runner-health.XXXXXX")" || return 1
  printf 'observed=%s\ndelivered=%s\npending=%s\n' "$state" "$delivered" "$pending" > "$partial" && mv -f -- "$partial" "$STATE_FILE" \
    || { rm -f -- "$partial"; return 1; }
}

# A change of state starts a new occurrence. An outage always owes an alert. A healthy runner owes a notice only if ntfy
# accepted an alert whose end hasn't been announced: an outage nobody heard about has nothing to announce.
start_occurrence() {
  [[ "$state" != "$observed" ]] || return 0
  pending=yes
  if [[ "$state" = healthy ]] && [[ "$delivered" = healthy ]]; then
    pending=no
  fi
  return 0
}

main() {
  local rc=0
  validate_settings
  check_service
  [[ -n "$state" ]] || check_github
  load_state

  echo "runner-health: $state (was $observed; last notice: $delivered)"
  [[ "$state" != healthy ]] || priority=3
  start_occurrence
  if [[ "$pending" = yes ]]; then
    if notify; then
      delivered="$state"
      pending=no
    else
      echo "runner-health: ntfy did not accept the alert; the next check sends it again" >&2
      rc=1
    fi
  fi
  if ! save_state; then
    echo "runner-health: could not save $STATE_FILE; the next check may alert again" >&2
    rc=1
  fi
  return "$rc"
}

main
