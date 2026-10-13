#!/usr/bin/env bash
# liveness-canary.sh — declared-vs-runtime execution reconciliation
# (docs/liveness-canary-design.md). Every 15 min: verify each job DECLARED in
# substrate/plists/*.json for this machine's role is actually ALIVE at runtime — label loaded
# in the launchd domain, last exit status healthy, fresh execution evidence (its .out mtime
# within the descriptor's liveness_max_gap_seconds) — and flag loaded ai.myndaix.* labels
# nobody declares (rogues). drift-canary covers CONFIG-level convergence; this covers the
# operational-omission class it can't see: installed AND loaded but never actually firing.
#
# READ-ONLY against launchd (print/list only — never bootstrap/bootout/kickstart). Alerts via
# the drift-canary streak+latch pattern into $OPERATOR_INBOX. Exits 0 always (the canary
# itself must not accumulate launchd failure state).
# liveness-fire: every run ends in an unconditional log line, so this job's own .out mtime is
# execution evidence for drift-canary's reverse watch (mutual coverage, no third component).
set -euo pipefail
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=substrate/lib.sh
source "$SUBSTRATE_DIR/lib.sh"
substrate_load_config

INTERVAL=900        # MUST equal StartInterval in plists/ai.myndaix.liveness.json (test-asserted)
THRESHOLD=2         # consecutive divergent runs before alerting (~2 intervals)
STATE_DIR="$MYNDAIX_HOME/state"
# Exit-0-always applies to STATE failures too (KilaBz P2): a canary that can't write its
# state must log-and-exit-0, never let set -e hand launchd a failure record.
mkdir -p "$STATE_DIR" || { log "liveness: ALARM cannot create $STATE_DIR — skipping this tick"; exit 0; }
STREAK_FILE="$STATE_DIR/liveness-streak"
ALERTED_FILE="$STATE_DIR/liveness-alerted"
LAST_RUN_FILE="$STATE_DIR/liveness-last-run"
LA_DIR="$HOME/Library/LaunchAgents"
# Test seams ONLY (mirrors nothing in the live plist): LCTL injects a stub launchctl so every
# divergence path runs without touching live launchd; PLISTS_DIR points the descriptor glob at
# a fixture dir. Both default to the real values; read-only either way.
LCTL="${LIVENESS_LAUNCHCTL:-launchctl}"
PLISTS_DIR="${LIVENESS_PLISTS_DIR:-$SUBSTRATE_DIR/plists}"

# mtime EPOCH seconds. macOS `stat -f %m` (BSD) is the factory; Linux CI is GNU `stat -c %Y`.
# CRITICAL: the two forms must be &&-GUARDED, never `A || B` inside one substitution — on GNU,
# `stat -f` means --file-system and prints a MULTILINE filesystem block to stdout while exiting
# nonzero, so a `||` chain captures that garbage + the real mtime, and `$((10#$garbage))` then
# aborts the canary under set -e (Linux CI red). Guarded: we emit ONLY a form's output when it
# succeeded; a missing file falls through to 0. Always returns 0-status so callers don't trip set -e.
mtime() {
  local m
  m="$(stat -f %m "$1" 2>/dev/null)" && { printf '%s' "$m"; return 0; }   # BSD (macOS)
  m="$(stat -c %Y "$1" 2>/dev/null)" && { printf '%s' "$m"; return 0; }   # GNU (Linux)
  printf '0'
}

now="$(date +%s)"

# ---- self-grace (sleep/wake guard) ------------------------------------------------------
# If WE haven't run in > 2x our own interval, the machine was asleep/frozen — every job's
# evidence is equally stale. Touch + skip one tick so each job catches up, instead of a
# wake-up alert storm. .last_run is touched on EVERY normal run (Oracle re-review note 1).
last_run="$(mtime "$LAST_RUN_FILE")"; last_run=$((10#$last_run))
touch "$LAST_RUN_FILE" || log "liveness: WARN cannot touch .last_run (next tick may sleep-grace)"
if [[ "$last_run" -ne 0 ]] && (( now - last_run > 2 * INTERVAL )); then
  log "liveness: own last run $((now - last_run))s ago (>2x interval) — sleep/wake grace, skipping one tick"
  exit 0
fi

div=""; div_n=0; div_keys=$'\n'
grace=""; grace_n=0   # reconcile-grace skips: alive-UNVERIFIED this tick, reported by label
# diverge KEY MESSAGE — KEY is the stable per-divergence identity (the label, or a synthetic
# key for non-label problems) used by the grow-only latch to decide what's NEW; MESSAGE is the
# human line in the alert body. Multiple sub-problems of one job share the label KEY (grouped).
diverge() {
  local key="$1" msg="$2"
  div="${div}- ${msg}"$'\n'; div_n=$((div_n + 1))
  case "$div_keys" in *$'\n'"$key"$'\n'*) : ;; *) div_keys="${div_keys}${key}"$'\n' ;; esac
  log "liveness: DIVERGENT ${msg}"
}

# ---- declared set: ONE python3 pass over all descriptors (paths via argv) ---------------
# liveness_targets.py emits label/max_gap/sentinel/out_path per watched job and a per-file
# ERR line on a corrupt descriptor (fail-closed, never sinks the batch). Its own nonzero
# exit means the CONFIG is unreadable — the whole declared set is unverifiable.
targets_ok=1; err_n=0
if ! targets="$(python3 "$SUBSTRATE_DIR/liveness_targets.py" "$CONFIG_FILE" "$PLISTS_DIR"/*.json)"; then
  targets=""
  targets_ok=0
  diverge "config" "liveness_targets.py failed (invalid config?) — declared set unverifiable, remedy: run reconcile --dry-run"
fi

declared=$'\n'   # newline-framed membership string (bash-3.2 safe; no arrays)
while IFS=$'\t' read -r label max_gap sentinel out_path; do
  [[ -n "$label" ]] || continue
  if [[ "$label" == "ERR" ]]; then
    # fields here are: ERR <file> <reason> — an unwatchable descriptor IS the omission class.
    # Count it: a per-file ERR means the declared set is INCOMPLETE (this job's label may be
    # absent), so the rogue sweep must be suppressed too (see its gate below) — else the ERR'd
    # job's still-loaded label gets flagged ROGUE with a bootout remedy (deep-audit P2).
    err_n=$((err_n + 1))
    diverge "descriptor:$max_gap" "descriptor $max_gap: $sentinel"
    continue
  fi
  declared="${declared}${label}"$'\n'
  # Belt behind the python-side validation: never interpolate an unvalidated label into a
  # launchctl target or an alert body (design §Security Surface).
  if ! [[ "$label" =~ ^ai\.myndaix\.[A-Za-z0-9._-]+$ ]]; then
    diverge "declared-invalid" "declared-set line carries an invalid label — refusing to query launchd with it"
    continue
  fi
  [[ "$max_gap" =~ ^[0-9]+$ ]] || { diverge "$label" "$label: non-numeric max gap in declared set"; continue; }
  max_gap=$((10#$max_gap))
  # Sentinel-gated + unarmed => the job is LEGITIMATELY unloaded — skip, not divergence.
  if [[ "$sentinel" != "-" && ! -e "$MYNDAIX_HOME/$sentinel" ]]; then continue; fi
  # Reconcile-grace, UNCONDITIONAL (not gated on a missing .out): a plist fresher than the
  # job's max gap was just (re)installed — the job hasn't had a full cycle yet. The skip is
  # COLLECTED, never silent: a grace-skipped job got NO verdict this tick, and the all-alive
  # pass line below must not vouch for it (the stress-matrix preflight gate rejects on this).
  plist_m="$(mtime "$LA_DIR/$label.plist")"; plist_m=$((10#$plist_m))
  if [[ "$plist_m" -ne 0 ]] && (( now - plist_m <= max_gap )); then
    grace="${grace}${label} "; grace_n=$((grace_n + 1)); continue
  fi
  # Loaded? Targeted print on the validated label only; a nonzero exit (incl. permission/SIP
  # quirks) = "not loaded" divergence, never a crash.
  if ! pr="$("$LCTL" print "$LA_DOMAIN/$label" 2>/dev/null)"; then
    diverge "$label" "$label: NOT LOADED — remedy: launchctl bootstrap $LA_DOMAIN $LA_DIR/$label.plist (or let reconcile converge)"
    continue
  fi
  # Freshness: the job's stdout log mtime is the execution evidence (every descriptor's
  # program writes >=1 stdout line per fire — the liveness-fire invariant, test-asserted).
  out_m="$(mtime "$out_path")"; out_m=$((10#$out_m))
  fresh=0; [[ "$out_m" -ne 0 ]] && (( now - out_m <= max_gap )) && fresh=1
  if [[ "$out_m" -eq 0 ]]; then
    diverge "$label" "$label: NEVER RAN — no $out_path past the install grace — remedy: investigate why launchd isn't firing it"
  elif [[ "$fresh" -eq 0 ]]; then
    diverge "$label" "$label: STALE — last execution evidence $((now - out_m))s ago (max ${max_gap}s) — remedy: investigate $out_path"
  fi
  # Last exit status — evaluated ONLY when the job is firing FRESH, and only after TWO CONSECUTIVE
  # failed FIRES. launchd LATCHES `last exit code` until the next run, so ONE failed fire is visible
  # across many 900s canary ticks; counting ticks would let a single transient failure satisfy the
  # THRESHOLD=2 persistence gate (deep-audit P2 + KilaBz re-review). We instead remember, per label,
  # the .out mtime of the last failed fire and a consecutive-fail count, and only diverge once two
  # DISTINCT fires (distinct .out mtimes) have failed — persistence across RUNS, not ticks. A fresh
  # exit 0 resets the memory. STALE jobs are already reported above; a parse miss just skips this.
  if [[ "$fresh" -eq 1 ]]; then
    ecf="$STATE_DIR/liveness-ec-$label"       # per validated label (safe as a filename)
    ec="$(printf '%s\n' "$pr" | sed -n 's/^[[:space:]]*last exit code = \(-\{0,1\}[0-9][0-9]*\).*/\1/p' | head -1 || true)"
    if [[ "$ec" =~ ^-?[0-9]+$ ]] && [[ "$ec" != "0" ]]; then
      ec_line="$(cat "$ecf" 2>/dev/null || echo '0 0')"
      ec_m="${ec_line%% *}"; ec_cnt="${ec_line##* }"
      [[ "$ec_m" =~ ^[0-9]+$ ]] || ec_m=0; [[ "$ec_cnt" =~ ^[0-9]+$ ]] || ec_cnt=0
      if [[ "$out_m" -ne "$((10#$ec_m))" ]]; then          # a NEW fire (distinct .out mtime) failed
        ec_cnt=$(( 10#$ec_cnt + 1 )); [[ "$ec_cnt" -gt 9 ]] && ec_cnt=9
        { printf '%s %s\n' "$out_m" "$ec_cnt" > "$ecf.tmp" && mv -f "$ecf.tmp" "$ecf"; } 2>/dev/null || true
      fi
      if [[ "$((10#$ec_cnt))" -ge 2 ]]; then               # persisted across >=2 distinct fires
        diverge "$label" "$label: exited nonzero ($ec) on $((10#$ec_cnt)) consecutive fires (firing but failing) — remedy: investigate $out_path"
      fi
    else
      rm -f "$ecf" 2>/dev/null || true                     # fresh exit 0 -> recovered, reset the memory
    fi
  fi
done <<< "$targets"

# ---- static hand-managed daemons (outside reconcile's managed set): liveness = pid present
# shellcheck disable=SC2043  # single-item list is intentional — the roster grows in place
for label in ai.myndaix.runtime; do
  declared="${declared}${label}"$'\n'
  if ! pr="$("$LCTL" print "$LA_DOMAIN/$label" 2>/dev/null)"; then
    diverge "$label" "$label: daemon NOT LOADED — remedy: launchctl bootstrap $LA_DOMAIN $LA_DIR/$label.plist"
  elif ! printf '%s\n' "$pr" | grep -qE '^[[:space:]]*pid = [0-9]+'; then
    diverge "$label" "$label: daemon loaded but NO RUNNING PID — remedy: launchctl kickstart -k $LA_DOMAIN/$label"
  fi
done

# ---- reverse sweep: loaded ai.myndaix.* minus declared minus static = rogue -------------
# Enumeration uses `launchctl list` (tab-delimited, stable); `print` output is never parsed
# for enumeration (brittle across macOS releases). SKIPPED when the declared set is incomplete —
# a whole-batch config failure (targets_ok=0) OR any per-file ERR (err_n>0): an incomplete
# declared set would flag a legitimate-but-unparsed job ROGUE with a bootout remedy, and an
# operator following it would cause a self-inflicted outage (Oracle P3 + deep-audit P2). The
# incompleteness is already its own divergence above.
if [[ "$targets_ok" -eq 1 && "$err_n" -eq 0 ]]; then
  rogue_src="$("$LCTL" list 2>/dev/null | awk -F'\t' '$3 ~ /^ai\.myndaix\./ {print $3}' || true)"
  while IFS= read -r label; do
    [[ -n "$label" ]] || continue
    [[ "$declared" == *$'\n'"$label"$'\n'* ]] && continue
    # VALIDATE (don't sanitize-by-deletion): a label that passes the strict regex gets the
    # copy-paste bootout remedy. One that FAILS must NEVER get a bootout — tr-style stripping
    # would ALIAS a mangled name (e.g. 'ai.myndaix.controller ' with a trailing space) onto a
    # healthy DECLARED job, and an operator following the remedy boots out the wrong one
    # (deep-audit P2). Report it by raw bytes, investigate-only.
    if [[ "$label" =~ ^ai\.myndaix\.[A-Za-z0-9._-]+$ ]]; then
      diverge "$label" "$label: ROGUE — loaded in $LA_DOMAIN but not declared in substrate/plists/ — remedy: investigate, then launchctl bootout $LA_DOMAIN/$label"
    else
      hex="$(printf '%s' "$label" | od -An -tx1 | tr -d ' \n' || true)"
      diverge "malformed:$hex" "MALFORMED loaded label (bytes 0x$hex) — not declared and not a valid ai.myndaix.* name; investigate manually, do NOT blind-bootout a mangled name"
    fi
  done <<< "$rogue_src"
fi

# ---- streak + GROW-ONLY latch + alert ---------------------------------------------------
# A bare global latch (drift-canary's model — correct there, it watches ONE signal) would blind
# this N-signal canary: once any divergence latched, a NEW job going bad would produce ZERO
# alerts until the first fully heals — the exact silent-omission class this exists to kill
# (deep-audit P1). Instead the latch stores the SET of already-alerted divergence keys; we alert
# only when a NEW key appears (grow-only). Partial healing does not re-alert; a fully-clean run
# clears the set. THRESHOLD debounces the FIRST divergence (2 consecutive ticks); a new key
# arriving mid-incident alerts on sight, which is correct — we're already in an alerting state.
# Grace visibility (tracked follow-up, review 20260905164609): emit the skipped labels on EVERY
# run they exist, so no consumer has to infer them from silence.
[[ -n "$grace" ]] && log "liveness: GRACE-SKIPPED (recent install, no verdict this tick): ${grace% }"

if [[ "$div_n" -eq 0 ]]; then
  rm -f "$STREAK_FILE" "$ALERTED_FILE" || log "liveness: WARN could not clear streak/latch"
  # "all declared jobs alive" is the strong pass signal external gates grep for — it must now
  # appear ONLY when every declared job actually got a verdict. Grace-skips get the weaker line.
  if [[ -n "$grace" ]]; then
    log "liveness: checked jobs alive — ${grace_n} grace-skipped, UNVERIFIED this tick"
  else
    log "liveness: all declared jobs alive"
  fi
  exit 0
fi

streak="$(cat "$STREAK_FILE" 2>/dev/null || echo 0)"
[[ "$streak" =~ ^[0-9]+$ ]] || streak=0
streak=$(( 10#$streak + 1 ))
# Explicit fail-closed write (a `printf > tmp && mv` &&-chain is exempt from set -e on the
# non-final link — the #89 class). ALARM-log + exit 0, NOT die (KilaBz P2): exit-0-always
# outranks the streak — a nonzero exit would hand launchd a failure record for the canary
# itself, and next tick both retries the write and reports this run's exit code anyway.
if ! { printf '%s\n' "$streak" > "$STREAK_FILE.tmp" && mv -f "$STREAK_FILE.tmp" "$STREAK_FILE"; }; then
  log "liveness: ALARM could not write streak file — divergences logged above, retrying next tick"
  exit 0
fi
log "liveness: $div_n divergence(s) (streak=$streak)"

# Load the already-alerted key set (newline-framed); a missing file = nothing alerted yet.
alerted=$'\n'
if [[ -e "$ALERTED_FILE" ]]; then
  while IFS= read -r k; do [[ -n "$k" ]] && alerted="${alerted}${k}"$'\n'; done < "$ALERTED_FILE"
fi
# Any divergence key NOT previously alerted is NEW. Also build the union to persist on alert.
new_keys=""; union="$alerted"
while IFS= read -r k; do
  [[ -n "$k" ]] || continue
  if [[ "$union" != *$'\n'"$k"$'\n'* ]]; then new_keys="${new_keys}${k} "; union="${union}${k}"$'\n'; fi
done <<< "$div_keys"

if [[ "$streak" -ge "$THRESHOLD" && -n "$new_keys" ]]; then
  msg="liveness-canary: declared-vs-runtime divergence (${streak} checks; new: ${new_keys}). Jobs declared in substrate/plists/ are not executing as declared:

$div"
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/liveness-alert-$(date '+%Y%m%d%H%M%S').md"
    # Latch ONLY after the alert write succeeds — a failed write (disk full) must not
    # suppress all future alerts (drift-canary's exact rule).
    if { printf '%s\n' "$msg" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; }; then
      # Persist the union (grow-only) so these keys don't re-alert until a fully-clean run
      # clears the set. A failed latch write must not kill the run (set -e): worst case is a
      # duplicate alert next tick — fail-noisy, never fail-dead (KilaBz P2).
      { printf '%s' "$union" > "$ALERTED_FILE.tmp" && mv -f "$ALERTED_FILE.tmp" "$ALERTED_FILE"; } \
        || log "liveness: WARN latch write failed — a duplicate alert may drop next tick"
      log "liveness: alert dropped -> $alert"
    else
      rm -f "$alert.tmp"
      log "liveness: FAILED to write alert to $alert — will retry next interval (not latched)"
    fi
  else
    # Do NOT latch: the alert was NOT delivered. Log-don't-latch; the next interval retries
    # delivery once the inbox returns, then latches on success (a lost alert must not be
    # suppressed forever).
    log "liveness: OPERATOR_INBOX unavailable (${OPERATOR_INBOX:-<unset>}) — alert not delivered:"$'\n'"$msg"
  fi
fi
exit 0
