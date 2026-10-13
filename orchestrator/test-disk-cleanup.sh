#!/bin/bash
# Safety tests for disk-cleanup.sh — an UNATTENDED deleter, so the tests prove it (a) refuses
# anything outside the ~/Library allowlist, (b) refuses `..`, (c) reaps ONLY age-aged children and
# spares fresh ones, (d) NEVER deletes in dry-run. Run: bash orchestrator/test-disk-cleanup.sh
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAIL=0
ok(){ if [ "$1" = "1" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); echo "  FAIL: $2"; fi; }

# source the functions WITHOUT running main (guarded by BASH_SOURCE check)
# shellcheck disable=SC1090
source "$DIR/disk-cleanup.sh"
set +e   # the sourced script sets -e; the negative guard tests intentionally return non-zero

# --- (a)/(b) _guard: allowlist + no-traversal, fail-closed ---
_guard "$HOME/Library/Caches/x" "$HOME/Library";        ok "$([ $? = 0 ] && echo 1)" "guard allows a path under the prefix"
_guard "$HOME/Downloads/x" "$HOME/Library";             ok "$([ $? = 1 ] && echo 1)" "guard REFUSES ~/Downloads (outside prefix)"
_guard "$HOME/Library/../.ssh" "$HOME/Library";         ok "$([ $? = 1 ] && echo 1)" "guard REFUSES a '..' traversal"
_guard "/etc/passwd" "$HOME/Library";                   ok "$([ $? = 1 ] && echo 1)" "guard REFUSES an absolute non-home path"
_guard "" "$HOME/Library";                              ok "$([ $? = 1 ] && echo 1)" "guard REFUSES empty"
_guard "/tmp/x" "/tmp";                                 ok "$([ $? = 1 ] && echo 1)" "guard REFUSES a prefix outside HOME"

# --- (c) reap_aged: a NON-allowlisted dir is refused, its file SURVIVES ---
SBOX="$(mktemp -d "${TMPDIR:-/tmp}/mxq-clean-test.XXXXXX")"
trap 'chmod -R u+rwX "$SBOX" 2>/dev/null; rm -rf "$SBOX" 2>/dev/null' EXIT
mkdir -p "$SBOX/precious"; echo keep > "$SBOX/precious/data.txt"
DRY=0 reap_aged "$SBOX/precious" 0 "OUTSIDE-ALLOWLIST" >/dev/null 2>&1
ok "$([ -f "$SBOX/precious/data.txt" ] && echo 1)" "reap_aged REFUSES a dir outside ~/Library (file survived)"

# --- (c) reap_aged inside the allowlist: OLD child reaped, FRESH child spared ---
LIVE="$HOME/Library/Caches/mxq-cleanup-selftest"
mkdir -p "$LIVE/old_build" "$LIVE/fresh_build"
echo x > "$LIVE/old_build/a"; echo x > "$LIVE/fresh_build/a"
touch -t "$(date -v-3d '+%Y%m%d%H%M' 2>/dev/null || echo 202601010000)" "$LIVE/old_build"   # 3 days old
DRY=0 reap_aged "$LIVE" 1 "SELFTEST" >/dev/null 2>&1
ok "$([ ! -e "$LIVE/old_build" ] && echo 1)" "reap_aged removed the >1d-old child"
ok "$([ -e "$LIVE/fresh_build" ] && echo 1)" "reap_aged SPARED the fresh child (no live-build race)"

# --- (d) dry-run deletes NOTHING even inside the allowlist ---
mkdir -p "$LIVE/old2"; touch -t 202601010000 "$LIVE/old2"
DRY=1 reap_aged "$LIVE" 1 "DRY" >/dev/null 2>&1
ok "$([ -e "$LIVE/old2" ] && echo 1)" "dry-run reaped NOTHING (old child still present)"
DRY=1 purge_cache "$LIVE" "DRY" >/dev/null 2>&1
ok "$([ -e "$LIVE/old2" ] && echo 1)" "dry-run purge_cache deleted NOTHING"

# --- purge_cache refuses a non-Caches dir ---
mkdir -p "$SBOX/notcache"; echo k > "$SBOX/notcache/f"
DRY=0 purge_cache "$SBOX/notcache" "NOTCACHE" >/dev/null 2>&1
ok "$([ -f "$SBOX/notcache/f" ] && echo 1)" "purge_cache REFUSES a dir outside ~/Library/Caches"

# --- codex CRITICAL: a SYMLINKED allowlisted dir must be refused (else the glob/find enumerates its
#     real target and remove_hard rm -rf's it). Both reap_aged + purge_cache must spare the target. ---
mkdir -p "$SBOX/precious2"; echo keep > "$SBOX/precious2/data.txt"
SYM="$HOME/Library/Caches/mxq-symlink-selftest"
ln -s "$SBOX/precious2" "$SYM"
DRY=0 purge_cache "$SYM" "SYMLINK-CACHE" >/dev/null 2>&1
ok "$([ -f "$SBOX/precious2/data.txt" ] && echo 1)" "purge_cache REFUSES a symlinked cache dir (target survived)"
DRY=0 reap_aged "$SYM" 0 "SYMLINK-REAP" >/dev/null 2>&1
ok "$([ -f "$SBOX/precious2/data.txt" ] && echo 1)" "reap_aged REFUSES a symlinked dir (target survived)"
rm -f "$SYM"

# --- a symlinked CHILD inside an allowlisted dir is left untouched (its target survives) ---
mkdir -p "$SBOX/childtarget"; echo k > "$SBOX/childtarget/f"
ln -s "$SBOX/childtarget" "$LIVE/symchild"
touch -h -t 202601010000 "$LIVE/symchild" 2>/dev/null
DRY=0 reap_aged "$LIVE" 1 "CHILDSYM" >/dev/null 2>&1
ok "$([ -f "$SBOX/childtarget/f" ] && echo 1)" "reap_aged skips a symlinked child (its target survived)"

# --- codex HIGH: refuse to run with a bad HOME (empty/"/") so the allowlist can't become /Library ---
HOME=/ bash "$DIR/disk-cleanup.sh" >/dev/null 2>&1; ok "$([ $? -ne 0 ] && echo 1)" "refuses to run with HOME=/ (no /Library deletion)"
env -u HOME bash "$DIR/disk-cleanup.sh" >/dev/null 2>&1; ok "$([ $? -ne 0 ] && echo 1)" "refuses to run with HOME unset"

chmod -R u+rwX "$LIVE" 2>/dev/null; rm -rf "$LIVE" 2>/dev/null   # clean the self-test dir
echo "ALL PASS ($PASS checks)"; [ "$FAIL" = 0 ] || { echo "FAILED ($FAIL)"; exit 1; }
