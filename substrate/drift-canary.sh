#!/usr/bin/env bash
# drift-canary.sh — the loud smoke alarm (design §2.6). Runs `reconcile.sh --dry-run` on a
# cheap interval; if drift PERSISTS past a threshold, drops one alert into the operator
# inbox. It does NOT auto-fix — reconcile's own poll converges; the canary only shouts when
# convergence isn't happening (e.g. a broken reconcile, a stuck migration, a hand-edit).
# liveness-fire: every run logs >=1 stdout line unconditionally ("no drift" / "DRIFT"), so
# this job's .out mtime is execution evidence for liveness-canary's freshness check.
set -euo pipefail
# EXECUTE-ONLY: this script owns its shell options (set -euo pipefail above, and a bare set +e /
# set -e bracket around the reconcile --dry-run capture below). Sourcing it would clobber the caller's
# options, so refuse (review 28330 P3 — the alternative, save-and-restore of prior errexit state,
# buys nothing for a script that is only ever run by launchd and test.sh).
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then echo "drift-canary.sh must be executed, not sourced" >&2; return 1; fi
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=substrate/lib.sh
source "$SUBSTRATE_DIR/lib.sh"
substrate_load_config

# SINGLE-INSTANCE INVARIANT (all streak/latch read-modify-write in this script relies on it):
# drift-canary runs ONLY as the launchd job ai.myndaix.drift-canary — launchd never overlaps
# invocations of a label, so ticks are serialized. Do NOT run concurrent manual instances (a
# manual run while the tick is live can race the cat→rm→mv sequences and the fixed .tmp names;
# reviews 73734/23749/34704 flag these — wontfix BECAUSE of this invariant, matching canary_emit's
# established fixed-suffix pattern). A one-off manual run while the launchd job is unloaded is fine,
# and so is a manual run with DRIFT_CANARY_STATE_DIR set to a private dir (it then shares no
# streak/latch file with the live tick — the DEPLOY.md step-3 verify run).
# DRIFT_CANARY_STATE_DIR relocates ONLY this script's streak/latch files — the ones the
# SINGLE-INSTANCE INVARIANT above protects. DEPLOY.md step 3 points it at a private scratch dir
# so a foreground verify run can overlap a live launchd tick without racing its read-modify-write.
# Reads of OTHER jobs' state (LIVENESS_OUT) deliberately stay on $MYNDAIX_HOME/state. launchd
# never sets it. Absolute-only: a relative dir would silently land state under whatever cwd ran us.
# `-` not `:-`: an explicitly EMPTY override (a caller's failed mktemp) must hit the absolute-path
# refusal, not silently fall back to the live state dir it was meant to avoid.
STATE_DIR="${DRIFT_CANARY_STATE_DIR-$MYNDAIX_HOME/state}"
[[ "$STATE_DIR" == /* ]] || die "DRIFT_CANARY_STATE_DIR must be an absolute path (got: '$STATE_DIR')"
mkdir -p "$STATE_DIR"
# A private-state run delivers NO alerts: the inbox is live shared state too, and alert names are
# only second-unique, so a same-second live alert would share the .tmp (review 68869 P2). Blanking
# OPERATOR_INBOX routes canary_emit down its existing not-delivered path: logs the body, no latch.
if [[ -n "${DRIFT_CANARY_STATE_DIR:-}" ]]; then OPERATOR_INBOX=""; fi
STREAK_FILE="$STATE_DIR/drift-streak"
ALERTED_FILE="$STATE_DIR/drift-alerted"
THRESHOLD=2   # consecutive drifting checks before alerting (~2 intervals)

# canary_emit SFILE AFILE PREFIX LABEL BODY [THRESHOLD] — shared streak+latch+alert used by the
# config-drift watch and the (independent) liveness-execution watch.
# Each passes its OWN streak+latch files so a standing latch on one NEVER suppresses the other: a
# QUARANTINED hold keeps config drift latched for days, and the execution watcher dying under it
# must STILL alert (deep-audit P2 — the mutual watch must not share fate with config drift). Bumps
# the streak; at THRESHOLD drops ONE alert + latches on success (fail-closed writes,
# latch-after-write). THRESHOLD defaults to the config-drift $THRESHOLD.
canary_emit() {
  local sfile="$1" afile="$2" prefix="$3" label="$4" body="$5" threshold="${6:-$THRESHOLD}" streak alert
  streak="$(cat "$sfile" 2>/dev/null || echo 0)"
  [[ "$streak" =~ ^[0-9]+$ ]] || streak=0
  streak=$(( 10#$streak + 1 ))
  # &&-chain is exempt from set -e on the non-final link — the #89 class; fail-closed write.
  if ! { printf '%s\n' "$streak" > "$sfile.tmp" && mv -f "$sfile.tmp" "$sfile"; }; then
    die "could not write $label streak"
  fi
  log "canary: $label (streak=$streak)"
  [[ "$streak" -ge "$threshold" && ! -e "$afile" ]] || return 0
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/${prefix}-$(date '+%Y%m%d%H%M%S').md"
    # Latch ONLY after the alert write succeeds — else a failed write (disk full) would latch and
    # silently suppress ALL future alerts (cross-family review MAJOR).
    if { printf '%s\n' "$body" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; }; then
      : > "$afile"
      log "canary: $label alert dropped -> $alert"
    else
      rm -f "$alert.tmp"
      log "canary: FAILED to write $label alert to $alert — will retry next interval (not latched)"
    fi
  else
    # Do NOT latch: the alert was NOT delivered (a fail-open if latched — r5 gate). Re-logging each
    # interval is noisy-but-recoverable; the next interval retries delivery, then latches on success.
    log "canary: OPERATOR_INBOX unavailable (${OPERATOR_INBOX:-<unset>}) — $label alert not delivered:"$'\n'"$body"
  fi
}

# Test-only seam (mirrors liveness-canary's LCTL): drive rc without a heavy real reconcile run so
# the independent liveness-watch can be exercised behaviorally. Live drift-canary never sets it.
if [[ -n "${DRIFT_CANARY_TEST_RC:-}" ]]; then
  rc="$DRIFT_CANARY_TEST_RC"; report="(test seam: reconcile --dry-run skipped)"
else
  set +e
  report="$(/bin/bash "$SUBSTRATE_DIR/reconcile.sh" --dry-run 2>&1)"; rc=$?
  set -e
fi

# ---- liveness-execution reverse watch (INDEPENDENT streak+latch) --------------------------
# liveness-canary watches THIS job's recency like any declared job; here we watch ITS .out mtime
# back — mutual coverage, no third component, no cycle risk (each only READS the other's log
# mtime). Its OWN streak+latch (NOT folded into config drift) so a standing drift latch can't
# mute "the execution watcher is dead" and vice versa (deep-audit P2). Gated on its plist being
# installed past one full window (deploy grace). Runs regardless of the config-drift outcome.
LW_STREAK_FILE="$STATE_DIR/liveness-watch-streak"
LW_ALERTED_FILE="$STATE_DIR/liveness-watch-alerted"
LIVENESS_OUT="$MYNDAIX_HOME/state/liveness-canary.out"
LIVENESS_PLIST="$HOME/Library/LaunchAgents/ai.myndaix.liveness.plist"
LIVENESS_MAX_AGE=1800   # 2x its 900s StartInterval
# mtime EPOCH seconds, &&-guarded (NOT `A || B` inside one substitution): GNU `stat -f` means
# --file-system and leaks a multiline block to stdout while exiting nonzero, which a `||` chain
# would capture as garbage and abort the arithmetic below (Linux CI). Emit only a form that won.
_mtime() { local m; m="$(stat -f %m "$1" 2>/dev/null)" && { printf '%s' "$m"; return 0; }; m="$(stat -c %Y "$1" 2>/dev/null)" && { printf '%s' "$m"; return 0; }; printf '0'; }
lnow="$(date +%s)"
lpm="$(_mtime "$LIVENESS_PLIST")"
lom="$(_mtime "$LIVENESS_OUT")"
if [[ "$((10#$lpm))" -ne 0 ]] && (( lnow - 10#$lpm > LIVENESS_MAX_AGE )) && (( lnow - 10#$lom > LIVENESS_MAX_AGE )); then
  canary_emit "$LW_STREAK_FILE" "$LW_ALERTED_FILE" "liveness-watch-alert" "liveness-watch DRIFT" \
    "drift-canary reverse watch: liveness-canary.out is stale ($((lnow - 10#$lom))s; max ${LIVENESS_MAX_AGE}s) — the execution watcher is not running. Every declared job's execution omission is now UNWATCHED. Investigate ai.myndaix.liveness (launchctl print $LA_DOMAIN/ai.myndaix.liveness)."
else
  rm -f "$LW_STREAK_FILE" "$LW_ALERTED_FILE" || log "canary: WARN could not clear liveness-watch streak/latch"
fi

# ---- config-drift watch ------------------------------------------------------------------
if [[ "$rc" -eq 0 ]]; then
  rm -f "$STREAK_FILE" "$ALERTED_FILE"
  log "canary: no drift"
else
  canary_emit "$STREAK_FILE" "$ALERTED_FILE" "drift-alert" "config DRIFT" \
    "drift-canary: FACTORY drift persisting. reconcile is not converging. Investigate.

$report"
fi
exit 0
