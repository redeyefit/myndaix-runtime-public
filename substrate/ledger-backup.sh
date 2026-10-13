#!/bin/bash
# ledger-backup — nightly pg_dump of the runtime ledger (the factory's durable memory and
# the future self-learning ground truth). Substrate-declared (ai.myndaix.ledger-backup) so
# reconcile installs it and the liveness-canary watches that it actually fires.
# Durability chain: dump here -> Syncthing (send-only folder) -> MacBook ~/MiniMirror ->
# Time Machine. Restore: pg_restore -d runtime <dump>. Every dump is verified with a FULL
# archive read (pg_restore -f /dev/null) before rotation — reads the entire payload, not
# just the TOC.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DB="${LEDGER_DB:-runtime}"
PG_DUMP="${LEDGER_PG_DUMP:-pg_dump}"
PG_RESTORE="${LEDGER_PG_RESTORE:-pg_restore}"
KEEP_DAYS="${LEDGER_KEEP_DAYS:-14}"
# Fail-safe: a zero/negative/garbage KEEP_DAYS must never rotate out the fresh dump.
[[ "$KEEP_DAYS" =~ ^[1-9][0-9]{0,3}$ ]] || KEEP_DAYS=14
BASE_DIR="${MYNDAIX_HOME:-$HOME/.myndaix}"
OUT_DIR="$BASE_DIR/backups/ledger"
LOG_FILE="$BASE_DIR/backups/ledger-backup.log"
LOCK_DIR="$BASE_DIR/backups/.ledger-backup.lock"
LOCK_STALE_SECONDS=3600

# liveness-fire: every run — success, failure, or signal — emits >=1 stdout line via the
# EXIT trap, so this job's .out mtime is real execution evidence for the liveness-canary.
# Trap is installed BEFORE any fallible command; INT/TERM convert to exits so the EXIT
# trap always reports (a handled signal must not let the script keep running).
tmp=""; have_lock=0
on_exit() {
  rc=$?
  printf '[%s] liveness-fire: ledger-backup tick rc=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$rc" || true
  rm -f "$tmp" 2>/dev/null || true
  [ "$have_lock" = 1 ] && rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$OUT_DIR"

log() { printf '[%s] [ledger-backup] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

# Whole-job lock (atomic mkdir). A fresh lock means another run is live: bow out loudly
# (rc=1 -> visible in .out; persistent lock contention surfaces via the canary). A stale
# lock (crashed run) older than LOCK_STALE_SECONDS is broken and this run proceeds.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  # mtime via python3: portable across BSD/GNU (stat -f/-c is the canary's own CI-bug class).
  # FAIL CLOSED: an unreadable mtime must treat the lock as LIVE, never as stale (an
  # "|| echo 0" fallback would let a broken python3 defeat the lock entirely).
  PY_BIN="${LEDGER_PY:-python3}"
  lock_mtime="$("$PY_BIN" -c 'import os,sys;print(int(os.path.getmtime(sys.argv[1])))' "$LOCK_DIR" 2>/dev/null || true)"
  if ! [[ "$lock_mtime" =~ ^[0-9]{1,12}$ ]]; then
    log "cannot read lock mtime — treating lock as LIVE, exiting"
    exit 1
  fi
  lock_age=$(( $(date +%s) - 10#$lock_mtime ))
  if [ "$lock_age" -lt "$LOCK_STALE_SECONDS" ]; then
    log "another run holds the lock (age ${lock_age}s) — exiting"
    exit 1
  fi
  log "breaking stale lock (age ${lock_age}s)"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR"
fi
have_lock=1

stamp="$(date '+%Y%m%d-%H%M%S')"
dest="$OUT_DIR/ledger-$stamp.dump"

log "BEGIN dump db=$DB dest=$dest"
tmp="$(mktemp "$OUT_DIR/.ledger-partial.XXXXXX")"
dump_rc=0
"$PG_DUMP" -Fc --no-password -f "$tmp" "$DB" 2>> "$LOG_FILE" || dump_rc=$?
if [ "$dump_rc" -ne 0 ]; then
  log "FAIL pg_dump rc=$dump_rc — no dump written"
  exit 1
fi

# Full-read verification: streams the whole archive payload to /dev/null. An archive that
# can't be fully read can't be restored.
verify_rc=0
"$PG_RESTORE" -f /dev/null "$tmp" 2>> "$LOG_FILE" || verify_rc=$?
if [ "$verify_rc" -ne 0 ]; then
  log "FAIL dump verification (full read) rc=$verify_rc — discarding partial"
  exit 1
fi

mv "$tmp" "$dest"; tmp=""
size="$(du -h "$dest" | cut -f1 | tr -d ' ')"
log "OK dump $dest ($size)"

# Rotate: keep KEEP_DAYS most-recent dumps (lexicographic == chronological; a same-second
# re-run overwrites atomically via mv — benign, both are valid fresh dumps). The Syncthing
# mirror on the MacBook keeps 30d versioning of anything rotated out here.
count=0
while IFS= read -r f; do
  count=$((count + 1))
  if [ "$count" -gt "$KEEP_DAYS" ]; then
    rm -f "$f"
    log "rotated out $(basename "$f")"
  fi
done < <(ls -1 "$OUT_DIR"/ledger-*.dump 2>/dev/null | sort -r)

log "END ok (retained $(( count < KEEP_DAYS ? count : KEEP_DAYS )) dumps)"
