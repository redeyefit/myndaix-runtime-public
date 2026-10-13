#!/bin/bash
# disk-cleanup.sh — recurring reclaim of REGENERABLE dev cruft so the host never hits a mid-build
# "No space left on device" wall (it did, 2026-06-28: a 460Gi disk at 99% killed a build).
#
# SAFETY MODEL (this runs UNATTENDED + deletes files, so it is deliberately conservative):
#   * ALLOWLIST ONLY — it touches a fixed set of known-regenerable paths under ~/Library, never a
#     variable/glob that could expand wrong. Each path is re-validated (prefix + exists) before any rm.
#   * AGE-GUARDED — Xcode/test dirs are reaped per-child only when older than N days, so it can NEVER
#     race a live build/test (which touches them now). DeviceSupport keys on OLD iOS versions.
#   * TIERED — routine pass always runs the cheap native cleaners + age-based regenerables; the heavy
#     re-download caches (playwright/Chrome) are cleared ONLY under disk pressure (free < threshold).
#   * NEVER TOUCHES — Xcode Archives (App Store/dSYMs), UserData, ~/Downloads, ~/code, or anything not
#     explicitly allowlisted. (It only ever deletes what rebuilds itself.)
#   * DISK_CLEANUP_DRY_RUN=1 -> log every intended delete, delete NOTHING (used to test).
#
# Install (launchd, weekly): cp orchestrator/ai.myndaix.disk-cleanup.plist.example
#   ~/Library/LaunchAgents/ai.myndaix.disk-cleanup.plist && launchctl load it.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# fail-closed on a bad HOME (codex HIGH): the whole allowlist is "$HOME/Library/..."; an empty or "/"
# HOME would silently move it to "/Library". Refuse to run rather than operate on the wrong tree.
case "${HOME:-}" in ""|"/") echo "[disk-cleanup] refusing: invalid HOME=${HOME:-<unset>}" >&2; exit 1;; esac
[ -d "$HOME" ] || { echo "[disk-cleanup] refusing: HOME is not a directory: $HOME" >&2; exit 1; }

DRY="${DISK_CLEANUP_DRY_RUN:-0}"
DD_AGE="${DISK_CLEANUP_DD_AGE_DAYS:-5}"          # DerivedData child not built in N days -> reap
XCTEST_AGE="${DISK_CLEANUP_XCTEST_AGE_DAYS:-5}"  # XCTestDevices clone idle N days -> reap
DEVSUPPORT_AGE="${DISK_CLEANUP_DEVSUPPORT_AGE_DAYS:-21}"  # iOS DeviceSupport version idle N days
SHIPIT_AGE="${DISK_CLEANUP_SHIPIT_AGE_DAYS:-3}"  # app-updater leftovers
PRESSURE_GB="${DISK_CLEANUP_PRESSURE_GB:-30}"    # below this free -> also clear heavy re-download caches
LOG="$HOME/.myndaix/orchestrator/disk-cleanup.log"
mkdir -p "$HOME/.myndaix/orchestrator" 2>/dev/null || true

log(){ printf '[%s] [disk-cleanup] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true
       printf '[disk-cleanup] %s\n' "$*"; }

free_gb(){ df -g / 2>/dev/null | awk 'NR==2{print $4}' || true; }   # macOS df -g: Avail (GiB) col 4; fail-safe

# _build_active: a belt over the age-guard (codex HIGH) — skip Xcode reaps if a build/test is running
# RIGHT NOW. Match ONLY the real build processes (exact names), NOT the persistent CoreSimulatorService
# daemon (which always runs once any sim was used and would disable the reaps forever). The age-guard
# (>N days; an active build updates its project's top-level DerivedData dir) is the primary protection.
_build_active(){ pgrep -x xcodebuild >/dev/null 2>&1 || pgrep -x XCBBuildService >/dev/null 2>&1; }

# remove_hard <path>: bounded peel of chmod-000 / chflags-uchg nesting, then rm -rf (mirrors
# fix-sweep.sh). Returns 0 if gone. NEVER called on a path that failed the allowlist guard.
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

# _guard <path> <required-prefix>: the path must be a non-empty, ..-free literal that resolves UNDER
# the required prefix (which is itself under $HOME). Fail-closed -> 1 (refuse).
_guard(){
  local p="$1" pre="$2"
  [ -n "$p" ] || return 1
  case "$p" in *".."*) return 1;; esac
  case "$pre" in "$HOME"/*) : ;; *) return 1;; esac      # prefix must be under HOME
  case "$p" in "$pre"/*|"$pre") return 0;; *) return 1;; esac
}

# _resolve_ok <dir> <prefix> <label>: the STRING guard isn't enough — a symlinked target dir would make
# the glob/find below enumerate the symlink's REAL contents and remove_hard would rm -rf them (codex
# CRITICAL: ~/Library/Caches/Google -> ~/code would delete ~/code). So refuse a symlinked dir AND
# require its CANONICAL (pwd -P) path to still sit under the allowlist prefix. Fail-closed.
_resolve_ok(){
  local d="$1" pre="$2" lbl="$3" real
  if [ -L "$d" ]; then log "REFUSE $lbl (is a symlink): $d"; return 1; fi
  real="$(cd "$d" 2>/dev/null && pwd -P)" || { log "REFUSE $lbl (unresolvable): $d"; return 1; }
  case "$real" in "$pre"/*|"$pre") return 0;; *) log "REFUSE $lbl (real path escapes $pre): $real"; return 1;; esac
}

# reap_aged <dir> <age_days> <label>: reap each immediate CHILD older than age_days; keep <dir>.
reap_aged(){
  local dir="$1" age="$2" label="$3" n=0 item
  if ! _guard "$dir" "$HOME/Library"; then log "REFUSE $label (failed guard): $dir"; return 0; fi
  [ -d "$dir" ] || { log "skip $label (absent)"; return 0; }
  _resolve_ok "$dir" "$HOME/Library" "$label" || return 0
  while IFS= read -r -d '' item; do
    [ -L "$item" ] && { log "skip $label symlink child (untouched): $item"; continue; }
    n=$((n + 1))
    if [ "$DRY" = "1" ]; then log "DRY reap $label: $item ($(du -sh "$item" 2>/dev/null | cut -f1))"
    else remove_hard "$item" || log "WARN $label: resisted $item"; fi
  done < <(find "$dir" -mindepth 1 -maxdepth 1 -mtime +"$age" -print0 2>/dev/null)
  log "$label: ${n} child(ren) >${age}d ${DRY:+(dry-run) }handled under $dir"
}

# purge_cache <dir> <label>: clear ALL contents (any age) of an app cache; keep <dir>. Caches only.
purge_cache(){
  local dir="$1" label="$2" item n=0
  if ! _guard "$dir" "$HOME/Library/Caches"; then log "REFUSE $label (not a cache): $dir"; return 0; fi
  [ -d "$dir" ] || { log "skip $label (absent)"; return 0; }
  _resolve_ok "$dir" "$HOME/Library/Caches" "$label" || return 0
  for item in "$dir"/* "$dir"/.[!.]*; do
    [ -e "$item" ] || continue
    [ -L "$item" ] && { log "skip $label symlink child (untouched): $item"; continue; }
    n=$((n + 1))
    if [ "$DRY" = "1" ]; then log "DRY purge $label: $item ($(du -sh "$item" 2>/dev/null | cut -f1))"
    else remove_hard "$item" || log "WARN $label: resisted $item"; fi
  done
  log "$label: ${n} entr(y/ies) ${DRY:+(dry-run) }purged from $dir"
}

native_cleaners(){   # tool-native, idempotent, best-effort — never fail the run
  if [ "$DRY" = "1" ]; then log "DRY would run: brew cleanup -s; pip cache purge; npm cache clean; simctl delete unavailable"; return 0; fi
  command -v brew  >/dev/null 2>&1 && { brew cleanup -s >/dev/null 2>&1 || true; log "brew cleanup done"; }
  command -v python3 >/dev/null 2>&1 && { python3 -m pip cache purge >/dev/null 2>&1 || true; log "pip cache purge done"; }
  command -v npm   >/dev/null 2>&1 && { npm cache clean --force >/dev/null 2>&1 || true; log "npm cache clean done"; }
  command -v xcrun >/dev/null 2>&1 && { xcrun simctl delete unavailable >/dev/null 2>&1 || true; log "simctl delete unavailable done"; }
}

main(){
  log "=== start (dry_run=$DRY) — free before: $(free_gb)Gi ==="
  native_cleaners
  # --- age-guarded regenerable Xcode caches — but NEVER while a build/sim is live (codex HIGH) ---
  if _build_active; then
    log "Xcode/Simulator active -> SKIP Xcode reaps this run (won't race a live build/test)"
  else
    reap_aged "$HOME/Library/Developer/Xcode/DerivedData"      "$DD_AGE"         "DerivedData"
    reap_aged "$HOME/Library/Developer/Xcode/iOS DeviceSupport" "$DEVSUPPORT_AGE" "iOS-DeviceSupport"
    reap_aged "$HOME/Library/Developer/XCTestDevices"          "$XCTEST_AGE"     "XCTestDevices"
  fi
  reap_aged "$HOME/Library/Caches/com.anthropic.claudefordesktop.ShipIt" "$SHIPIT_AGE" "ShipIt-updater"
  # --- pressure-only: heavy re-download caches (clearing forces a re-fetch) ---
  local avail; avail="$(free_gb)"
  if [ -n "$avail" ] && [ "$avail" -lt "$PRESSURE_GB" ]; then
    log "disk pressure (${avail}Gi < ${PRESSURE_GB}Gi) -> clearing heavy re-download caches"
    purge_cache "$HOME/Library/Caches/ms-playwright" "playwright"
    purge_cache "$HOME/Library/Caches/Google"        "chrome"
  else
    log "no disk pressure (${avail}Gi >= ${PRESSURE_GB}Gi) -> keeping heavy re-download caches"
  fi
  log "=== done — free after: $(free_gb)Gi ==="
}

# run only when executed directly; `source` (the test) gets the functions without running main.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
