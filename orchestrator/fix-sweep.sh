#!/bin/bash
# fix-sweep.sh — periodic backstop for play-fix.sh's sandboxed exec dirs.
#
# play-fix.sh runs untrusted patched code in $EXEC = "${TMPDIR:-/tmp}/myndaix-fix.XXXXXX" and removes
# it on exit. Two cases can leave one behind: an un-trappable SIGKILL, or adversarial chmod 000 / chflags
# uchg nesting DEEPER than cleanup()'s bounded peel. This job reaps those — and ONLY those: it matches the
# exact "myndaix-fix.*" prefix at depth 1 under known temp roots, AND only dirs older than AGE_MIN so it
# can never race a live run (a run lasts minutes; the verify timeout is 300s). It defeats the same
# chmod/chflags lockdown play-fix's cleanup does, with the same bounded peel.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# liveness-fire: one unconditional stdout line per fire — liveness-canary reads this job's
# .out mtime as execution evidence (log() below writes to $LOG, NOT stdout, so the launchd
# .out would otherwise never advance and every tick would read as "stale").
printf '[%s] [fix-sweep] tick fire\n' "$(date '+%Y-%m-%d %H:%M:%S')"

AGE_MIN="${MYNDAIX_FIX_SWEEP_AGE_MIN:-120}"            # minutes; >> any single run
LOG="$HOME/.myndaix/orchestrator/fix-sweep.log"
mkdir -p "$HOME/.myndaix/orchestrator" 2>/dev/null || true

log(){ printf '[%s] [fix-sweep] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true; }

# remove_hard <dir> : peel adversarial chmod-000 / chflags-uchg nesting (chmod opens traversal, then
# chflags clears immutables, one more level each pass), bounded; 0 if gone, 1 if it still resists.
remove_hard(){
  local d="$1" i=0
  while [ -e "$d" ] && [ "$i" -lt 40 ]; do
    i=$((i + 1))
    chmod -R u+rwX "$d" >/dev/null 2>&1 || true
    chflags -R nouchg "$d" >/dev/null 2>&1 || true
    rm -rf "$d" >/dev/null 2>&1 || true
  done
  [ -e "$d" ] && return 1 || return 0
}

# Candidate temp roots play-fix.sh may have used, canonicalized + de-duplicated (TMPDIR, the per-user
# darwin temp, and the /tmp fallback — /tmp and /private/tmp are the same inode).
declare -a roots=()
add_root(){ local r="$1" c; [ -n "$r" ] && [ -d "$r" ] || return 0; c="$(cd "$r" && pwd -P)" || return 0
  local x; for x in "${roots[@]:-}"; do [ "$x" = "$c" ] && return 0; done; roots+=("$c"); }
add_root "${TMPDIR:-}"
add_root "$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || true)"
add_root "/tmp"
add_root "/private/tmp"

swept=0; failed=0
for root in "${roots[@]:-}"; do
  while IFS= read -r -d '' d; do
    if remove_hard "$d"; then log "swept $d"; swept=$((swept + 1))
    else log "WARN could not remove $d (left for next sweep)"; failed=$((failed + 1)); fi
  done < <(find "$root" -maxdepth 1 -type d -name 'myndaix-fix.*' -mmin +"$AGE_MIN" -print0 2>/dev/null)
done

log "done: swept=$swept failed=$failed roots=${#roots[@]} age_min=$AGE_MIN"
exit 0
