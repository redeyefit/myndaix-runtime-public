#!/bin/bash
# test.sh — tailnet-watch. Hermetic: probes + notifications shimmed via env-bin overrides,
# state in a scratch MYNDAIX_HOME. Run: bash substrate/lab/test.sh
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# The watcher AND this suite are macOS-lab-only by design (BSD date -r, nc -G, osascript).
# CI is Linux (GNU coreutils) — the canary's own history: a suite that only runs on one
# platform must say so loudly instead of failing confusingly on the other.
if [ "$(uname)" != "Darwin" ]; then
  echo "tailnet-watch suite is macOS-only by design — skipped on $(uname)"
  exit 0
fi

SCRIPT="$(cd "$(dirname "$0")" && pwd)/tailnet-watch.sh"
SCRATCH="$(mktemp -d /tmp/tw-test.XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT INT TERM
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

# ---- shims ----
mkdir -p "$SCRATCH/bin" "$SCRATCH/home"
cat > "$SCRATCH/bin/nc" << 'EOF'
#!/bin/bash
exit "${TW_TEST_NC_EXIT:-0}"
EOF
cat > "$SCRATCH/bin/osascript" << 'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "${TW_TEST_NOTIFY_LOG:?}"
exit "${TW_TEST_OSA_EXIT:-0}"
EOF
cat > "$SCRATCH/bin/ts" << 'EOF'
#!/bin/bash
if [ "$1" = "status" ]; then
  case "${TW_TEST_TS_MODE:-healthy}" in
    healthy) printf '{"BackendState": "Running", "Self": {"Online": true}}';;
    offline) printf '{"BackendState": "Stopped", "Self": {"Online": false}}';;
  esac
  exit 0
fi
exit "${TW_TEST_TS_PING_EXIT:-1}"   # ping leg
EOF
chmod +x "$SCRATCH/bin/nc" "$SCRATCH/bin/osascript" "$SCRATCH/bin/ts"

run_tick() {  # $1 = nc exit code for this tick; extra env via caller
  MYNDAIX_HOME="$SCRATCH/home" \
  TW_HOST="203.0.113.1" \
  TW_TS_BIN="$SCRATCH/bin/ts" \
  TW_NC_BIN="$SCRATCH/bin/nc" \
  TW_OSA_BIN="$SCRATCH/bin/osascript" \
  TW_TEST_NC_EXIT="$1" \
  TW_TEST_NOTIFY_LOG="$SCRATCH/notifications.log" \
  bash "$SCRIPT"
}
STATE="$SCRATCH/home/state/tailnet-watch.state"
NOTES="$SCRATCH/notifications.log"; : > "$NOTES"

echo "1. happy path — reachable, no alert, clean state"
run_tick 0 || bad "tick errored"
grep -q '^fails=0$' "$STATE" && ok "state reset" || bad "state not reset"
grep -q '^last_tick_at=[1-9]' "$STATE" && ok "last_tick_at recorded" || bad "last_tick_at missing"
[ -s "$NOTES" ] && bad "unexpected notification" || ok "no notification"

echo "2. below threshold — 2 failures, no alert"
run_tick 1; run_tick 1
grep -q '^fails=2$' "$STATE" && ok "fails=2 persisted" || bad "fails wrong: $(cat "$STATE")"
[ -s "$NOTES" ] && bad "premature alert" || ok "no premature alert"

echo "3. threshold hit — 3rd failure alerts exactly once, latches only on delivery"
run_tick 1; sleep 1   # settle: the dialog belt is backgrounded
grep -c 'display notification.*unreachable' "$NOTES" | grep -qx 1 && ok "one alert" || bad "alert count: $(grep -c 'display notification.*unreachable' "$NOTES")"
grep -q 'display dialog.*unreachable' "$NOTES" && ok "dialog belt fired on the critical alert" || bad "no dialog belt"
grep -q '^alerted_at=[1-9]' "$STATE" && ok "alerted_at latched after delivered notify" || bad "alerted_at not latched"

echo "4. dedup — 4th/5th failures do NOT re-alert within window"
run_tick 1; run_tick 1; sleep 1
grep -c 'display notification.*unreachable' "$NOTES" | grep -qx 1 && ok "still one alert" || bad "re-alerted too soon"

echo "5. re-alert after window — aged alerted_at fires again"
python3 - "$STATE" << 'PYEOF'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r'alerted_at=\d+', 'alerted_at=1', s)
open(p, 'w').write(s)
PYEOF
run_tick 1; sleep 1
grep -c 'display notification.*unreachable' "$NOTES" | grep -qx 2 && ok "re-alert after window" || bad "no re-alert after window"

echo "6. recovery — heal notifies once and resets"
run_tick 0; sleep 1
grep -q 'reachable again' "$NOTES" && ok "recovery notice" || bad "no recovery notice"
grep -q 'display dialog.*reachable again' "$NOTES" && bad "recovery raised a dialog (should be notification-only)" || ok "no dialog on recovery"
grep -q '^fails=0$' "$STATE" && ok "state reset on heal" || bad "state not reset"
run_tick 0
grep -c 'reachable again' "$NOTES" | grep -qx 1 && ok "no duplicate recovery" || bad "duplicate recovery"

echo "7. corrupt state — garbage values fall back safe (octal trap incl.)"
printf 'fails=08\nalerted_at=banana\nfirst_fail_at=\n' > "$STATE"
run_tick 0 || bad "corrupt state crashed the tick"
grep -q '^fails=0$' "$STATE" && ok "corrupt state recovered" || bad "corrupt state persisted"

echo "7b. numeric-corrupt state — future alerted_at cannot suppress alerts"
printf 'fails=99\nalerted_at=99999999999\nfirst_fail_at=99999999999\n' > "$STATE"
run_tick 1 || bad "numeric-corrupt state crashed the tick"
sleep 1
grep -c 'display notification.*unreachable' "$NOTES" | grep -qx 3 && ok "future alerted_at clamped, alert fired" || bad "future alerted_at suppressed alert"
: > "$NOTES"; run_tick 0 > /dev/null; : > "$NOTES"

echo "7c. tailscale fallback — nc fails but ts-ping succeeds = reachable"
TW_TEST_TS_PING_EXIT=0 run_tick 1 || bad "ts-fallback tick errored"
grep -q '^fails=0$' "$STATE" && ok "ts-ping fallback counts as reachable" || bad "fallback ignored"
[ -s "$NOTES" ] && bad "fallback tick notified" || ok "no notification on fallback success"

echo "9. observer offline — tick skipped, streak untouched, no notification"
printf 'fails=2\nalerted_at=0\nfirst_fail_at=100\nlast_tick_at=%s\n' "$(date +%s)" > "$STATE"
TW_TEST_TS_MODE=offline run_tick 1 || bad "observer-offline tick errored"
grep -q '^fails=2$' "$STATE" && ok "streak untouched on skip" || bad "skip mutated state: $(grep fails= "$STATE")"
[ -s "$NOTES" ] && bad "skip tick notified" || ok "no notification on skip"

echo "10. stale streak — big observation gap restarts the window"
old=$(( $(date +%s) - 7200 ))
printf 'fails=2\nalerted_at=0\nfirst_fail_at=%s\nlast_tick_at=%s\n' "$old" "$old" > "$STATE"
run_tick 1
grep -q '^fails=1$' "$STATE" && ok "stale streak restarted (2 stale + 1 = 1, not 3)" || bad "stale streak persisted: $(grep fails= "$STATE")"
[ -s "$NOTES" ] && bad "stale streak alerted" || ok "no alert from stale accumulation"

echo "11. failed notify does not latch — alert retries next tick"
: > "$NOTES"
printf 'fails=2\nalerted_at=0\nfirst_fail_at=100\nlast_tick_at=%s\n' "$(date +%s)" > "$STATE"
TW_TEST_OSA_EXIT=1 run_tick 1 || bad "failed-notify tick errored"
grep -q '^alerted_at=0$' "$STATE" && ok "alerted_at NOT latched on failed notify" || bad "latched despite notify failure"
run_tick 1
grep -q '^alerted_at=[1-9]' "$STATE" && ok "alert retried and latched next tick" || bad "no retry after failed notify"

echo "12. recovery with unknown duration — no absurd minutes"
printf 'fails=3\nalerted_at=100\nfirst_fail_at=0\nlast_tick_at=%s\n' "$(date +%s)" > "$STATE"
: > "$NOTES"
run_tick 0
grep -q 'downtime unknown' "$NOTES" && ok "unknown-duration guard" || bad "absurd duration: $(cat "$NOTES")"

echo "13a. misconfig guard — missing TW_HOST alerts loudly and exits 1"
: > "$NOTES"
rc=0
TW_HOST="" \
  MYNDAIX_HOME="$SCRATCH/home" \
  TW_TS_BIN="$SCRATCH/bin/ts" TW_NC_BIN="$SCRATCH/bin/nc" TW_OSA_BIN="$SCRATCH/bin/osascript" \
  TW_TEST_NOTIFY_LOG="$NOTES" bash "$SCRIPT" || rc=$?   # TW_HOST="": hermetic under a caller shell that exports one (guard treats empty as unset)
[ "$rc" -eq 1 ] && ok "missing TW_HOST exits 1" || bad "missing TW_HOST rc=$rc"
grep -q 'misconfigured' "$NOTES" && ok "missing TW_HOST raises the misconfigured alert" || bad "no misconfigured alert"

echo "13b. misconfig guard — placeholder TW_HOST is treated as unset, not probed"
: > "$NOTES"
rc=0
MYNDAIX_HOME="$SCRATCH/home" TW_HOST="REPLACE-ME-factory-tailnet-ip" \
  TW_TS_BIN="$SCRATCH/bin/ts" TW_NC_BIN="$SCRATCH/bin/nc" TW_OSA_BIN="$SCRATCH/bin/osascript" \
  TW_TEST_NOTIFY_LOG="$NOTES" bash "$SCRIPT" || rc=$?
[ "$rc" -eq 1 ] && ok "placeholder exits 1" || bad "placeholder rc=$rc"
grep -q 'misconfigured' "$NOTES" && ok "placeholder raises the misconfigured alert" || bad "placeholder probed instead of alerting"
grep -q 'unreachable' "$NOTES" && bad "placeholder produced a false unreachable alert" || ok "no false unreachable alert"
: > "$NOTES"

echo "13. structural — pinned nc, no GNU stat trap, base-10 guards"
grep -q 'NC_BIN="${TW_NC_BIN:-/usr/bin/nc}"' "$SCRIPT" && ok "nc pinned to /usr/bin (brew-shadow proof)" || bad "nc not pinned"
grep -q 'stat -c' "$SCRIPT" && bad "GNU stat crept in" || ok "no stat portability trap"
grep -q '10#\$v' "$SCRIPT" && ok "base-10 normalization present" || bad "10# guard missing"

echo; echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
