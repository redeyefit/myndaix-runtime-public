#!/bin/bash
# tailnet-watch — the LAB watches the FACTORY.
# Installed copy runs from ~/.myndaix/bin (NOT the git tree — a branch switch must never
# kill the monitor). Hand-managed LaunchAgent on the MacBook, macOS-only by design.
# Probes the Mini over the tailnet each tick; 3 consecutive OBSERVED failures => macOS
# notification (re-armed every 4h while dark), recovery notice on heal. Ticks are skipped
# (not counted) when this Mac itself is off the tailnet — the watcher only asserts what it
# actually observed. Closes the 22-day-dark-Mini class (2026-07-18..08-09).
# Install/update/verify: substrate/lab/README.md.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Site config, no default: TW_HOST (the factory node tailnet IP) comes from the LaunchAgent
# plist EnvironmentVariables — see substrate/lab/README.md. Checked BELOW, after log/notify
# exist, so a script/plist skew alerts loudly instead of dying silently into the .err file.
TW_HOST="${TW_HOST:-}"
TW_PORT="${TW_PORT:-22}"
TS_BIN="${TW_TS_BIN:-/Applications/Tailscale.app/Contents/MacOS/Tailscale}"
NC_BIN="${TW_NC_BIN:-/usr/bin/nc}"            # pinned: a brew netcat lacks -G and would shadow via PATH
OSA_BIN="${TW_OSA_BIN:-osascript}"
FAIL_THRESHOLD="${TW_FAIL_THRESHOLD:-3}"      # consecutive observed ticks (~30 min awake)
REALERT_SECONDS="${TW_REALERT_SECONDS:-14400}" # 4h: de-anchors retries from daily Focus windows
STALE_SECONDS="${TW_STALE_SECONDS:-1800}"     # gap > 3 ticks => streak restarts (sleep honesty)
STATE_DIR="${MYNDAIX_HOME:-$HOME/.myndaix}/state"
STATE_FILE="$STATE_DIR/tailnet-watch.state"
LOG_FILE="$STATE_DIR/tailnet-watch.log"

mkdir -p "$STATE_DIR"
tmp=""
trap 'rm -f "$tmp"' EXIT INT TERM

log() { printf '[%s] [tailnet-watch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

notify() {
  # $1 = message, passed as argv — never interpolated into AppleScript source. Returns
  # osascript's rc; stderr goes to the log (no silent suppression on the alert path).
  # rc=0 still cannot prove a BANNER rendered (Focus can suppress silently) — the README's
  # mandatory install drill + Script-Editor-to-Alerts step covers that half.
  "$OSA_BIN" -e 'on run argv' \
    -e 'display notification (item 1 of argv) with title "MyndAIX tailnet-watch" sound name "Basso"' \
    -e 'end run' -- "$1" >/dev/null 2>> "$LOG_FILE"
}

# Missing site config = the watcher is NOT watching. Alert every tick until fixed — a
# script-only update against an old plist (no TW_HOST), or a plist copied with the
# placeholder unedited, must never turn into false "Mini unreachable" alerts or go dark.
if [ -z "$TW_HOST" ] || [ "$TW_HOST" = "REPLACE-ME-factory-tailnet-ip" ]; then
  log "TW_HOST unset or placeholder (script/plist skew) — set it in the plist EnvironmentVariables"
  notify "tailnet-watch misconfigured: TW_HOST not set — the factory is UNWATCHED"
  exit 1
fi

observer_online() {
  # Can THIS Mac even judge the Mini? Gate on local tailnet health so planes, captive
  # portals, wifi-off and wake races skip the tick instead of counting a false failure.
  # If the CLI is missing, don't gate (nc may still reach the Mini over LAN).
  [ -x "$TS_BIN" ] || return 0
  local s
  s="$("$TS_BIN" status --peers=false --json 2>/dev/null || true)"
  printf '%s' "$s" | grep -q '"BackendState": "Running"' || return 1
  printf '%s' "$s" | grep -q '"Online": true' || return 1
  return 0
}

probe() {
  if "$NC_BIN" -z -G 5 "$TW_HOST" "$TW_PORT" >/dev/null 2>&1; then return 0; fi
  if [ -x "$TS_BIN" ] && "$TS_BIN" ping -c 1 --timeout=5s "$TW_HOST" >/dev/null 2>&1; then return 0; fi
  return 1
}

# ---- read state (own file, key=value; missing file = first run) ----
# Digit caps prevent arithmetic overflow from corrupt values; future-timestamp clamps
# prevent a corrupt alerted_at from suppressing alerts indefinitely.
fails=0; alerted_at=0; first_fail_at=0; last_tick_at=0
if [ -f "$STATE_FILE" ]; then
  v="$(grep '^fails=' "$STATE_FILE" | cut -d= -f2 || true)"
  [[ "$v" =~ ^[0-9]{1,6}$ ]] && fails=$((10#$v))
  v="$(grep '^alerted_at=' "$STATE_FILE" | cut -d= -f2 || true)"
  [[ "$v" =~ ^[0-9]{1,12}$ ]] && alerted_at=$((10#$v))
  v="$(grep '^first_fail_at=' "$STATE_FILE" | cut -d= -f2 || true)"
  [[ "$v" =~ ^[0-9]{1,12}$ ]] && first_fail_at=$((10#$v))
  v="$(grep '^last_tick_at=' "$STATE_FILE" | cut -d= -f2 || true)"
  [[ "$v" =~ ^[0-9]{1,12}$ ]] && last_tick_at=$((10#$v))
fi

now="$(date +%s)"
[ "$fails" -gt 1000 ] && fails=1000
[ "$alerted_at" -gt "$now" ] && alerted_at=0
[ "$first_fail_at" -gt "$now" ] && first_fail_at=0
[ "$last_tick_at" -gt "$now" ] && last_tick_at=0

# A fail streak with a big observation gap (sleep, offline-observer skips) is stale —
# "consecutive" means consecutively OBSERVED. Restart the window rather than lie.
if [ "$fails" -gt 0 ] && [ "$last_tick_at" -gt 0 ] && [ $((now - last_tick_at)) -gt "$STALE_SECONDS" ]; then
  log "streak stale (last observation $(( (now - last_tick_at) / 60 ))m ago) — restarting window"
  fails=0; first_fail_at=0
fi

write_state() {
  tmp="$(mktemp "$STATE_DIR/.tailnet-watch.XXXXXX")"
  printf 'fails=%s\nalerted_at=%s\nfirst_fail_at=%s\nlast_tick_at=%s\n' "$1" "$2" "$3" "$4" > "$tmp"
  mv "$tmp" "$STATE_FILE"; tmp=""
}

if ! observer_online; then
  # No state write: skipped ticks neither extend nor kill a streak (staleness handles gaps).
  log "observer off the tailnet — tick skipped (cannot judge Mini)"
  exit 0
fi

if probe; then
  if [ "$alerted_at" -gt 0 ]; then
    if [ "$first_fail_at" -gt 0 ] && [ "$now" -ge "$first_fail_at" ] && [ $(( (now - first_fail_at) / 60 )) -le 43200 ]; then
      log "RECOVERED after ~$(( (now - first_fail_at) / 60 ))m down"
      notify "Mini is reachable again (was down ~$(( (now - first_fail_at) / 60 )) min)" || log "recovery notify FAILED (rc=$?)"
    else
      log "RECOVERED (duration unknown — state was reset or clamped)"
      notify "Mini is reachable again (downtime unknown)" || log "recovery notify FAILED (rc=$?)"
    fi
  fi
  [ "$fails" -gt 0 ] && log "reachable again after $fails failed tick(s) (below threshold)"
  write_state 0 0 0 "$now"
else
  fails=$((fails + 1))
  [ "$first_fail_at" -eq 0 ] && first_fail_at="$now"
  log "probe FAILED ($fails consecutive observed; threshold $FAIL_THRESHOLD)"
  if [ "$fails" -ge "$FAIL_THRESHOLD" ] && [ $((now - alerted_at)) -ge "$REALERT_SECONDS" ]; then
    since="$(date -r "$first_fail_at" '+%H:%M %m/%d')"
    if notify "Mini unreachable from this Mac since $since — factory + backup mirror may be dark"; then
      log "ALERT delivered: Mini unreachable since $since"
      alerted_at="$now"
    else
      log "ALERT notify FAILED (rc=$?) — will retry next tick"
    fi
    # Belt for the CRITICAL alert only: a dialog WINDOW — Focus modes and notification-style
    # settings cannot suppress it, so the alert path has no drifting Settings dependency.
    # Backgrounded (the tick must not block on a human); auto-dismisses after 10 min.
    nohup "$OSA_BIN" -e 'on run argv' \
      -e 'display dialog (item 1 of argv) with title "MyndAIX tailnet-watch" buttons {"OK"} default button 1 with icon caution giving up after 600' \
      -e 'end run' -- "Mini unreachable from this Mac since $since — factory + backup mirror may be dark" >/dev/null 2>> "$LOG_FILE" &
  fi
  write_state "$fails" "$alerted_at" "$first_fail_at" "$now"
fi
