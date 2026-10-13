#!/usr/bin/env bash
# lib.sh — shared helpers for the two-machine substrate. Sourced by reconcile.sh and
# drift-canary.sh (both run from the FRESH deploy clone, so this file is post-reset).
#
# NOT sourced by bootstrap-fetch (which must stay self-contained: it is the static
# fetcher that survives a bricked reconcile in origin/main — design §2.2 Stage 0).
#
# Callers own `set -euo pipefail`; we set it too for defence when sourced early.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# Don't write .pyc into the PULL-ONLY deploy clone — keep it pristine (belt-and-suspenders beyond
# .gitignore so the tree-guard never sees a stray __pycache__).
export PYTHONDONTWRITEBYTECODE=1
# Bound network git over SSH so a hung fetch can't stall reconcile / the drift-canary (no `timeout` on macOS).
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o BatchMode=yes}"
# Bound psql CONNECT so a flaky loopback (the Mini's intermittent 127.0.0.1 wedge) can't hang the
# health-WAIT forever: PGOPTIONS statement_timeout does NOT bound connection establishment, so without
# this the WAIT blocks on connect instead of failing-closed at its deadline (found live on the Mini cutover).
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-8}"

# Resolve the substrate dir + deploy clone from THIS file's location (source of truth
# for where we are actually running — cross-checked against config's DEPLOY_CLONE).
# pwd -P (physical): must match bootstrap-fetch's pwd -P + git --show-toplevel, else a symlinked
# DEPLOY_CLONE (/tmp, /var) passes bootstrap but fails reconcile's assert (cross-family review MAJOR).
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SELF_CLONE="$(cd "$SUBSTRATE_DIR/.." && pwd -P)"
LA_DOMAIN="gui/$(id -u)"

log() { printf '[%s] [substrate] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ALARM: $*" >&2; exit 1; }

# ---- config -----------------------------------------------------------------
# MYNDAIX_HOME is bootstrapped from env (the plist injects it) or the default; the
# config file lives under it. Every OTHER value comes from the validated config.
substrate_resolve_home() {
  MYNDAIX_HOME="${MYNDAIX_HOME:-$HOME/.myndaix}"
  CONFIG_FILE="$MYNDAIX_HOME/config.env"
  [[ -f "$CONFIG_FILE" ]] || die "no config.env at $CONFIG_FILE (run reconcile --update-bootstrap / SETUP)"
}

# cfg_get KEY — validated single value (config_parse fails closed on bad config).
cfg_get() {
  python3 "$SUBSTRATE_DIR/config_parse.py" "$CONFIG_FILE" --get "$1"
}

# substrate_load_config — validate the whole config once, then export resolved values.
# A validation failure here aborts under set -e (fail-closed: no restart on bad config).
substrate_load_config() {
  substrate_resolve_home
  # One full-file validation up front; --get calls below re-validate cheaply.
  python3 "$SUBSTRATE_DIR/config_parse.py" "$CONFIG_FILE" >/dev/null \
    || die "config validation failed ($CONFIG_FILE)"
  MACHINE_ROLE="$(cfg_get MACHINE_ROLE)"
  DEPLOY_CLONE="$(cfg_get DEPLOY_CLONE)"
  MYNDAIX_DSN="$(cfg_get MYNDAIX_DSN)"
  OPERATOR_INBOX="$(cfg_get OPERATOR_INBOX)"
  POLL_INTERVAL_S="$(cfg_get POLL_INTERVAL_S)"
  export MYNDAIX_HOME MACHINE_ROLE DEPLOY_CLONE MYNDAIX_DSN OPERATOR_INBOX POLL_INTERVAL_S
}

# substrate_assert_deploy_clone — before ANY mutating op, prove we run from exactly the
# clone config names as DEPLOY_CLONE. Blocks a reset --hard on the wrong tree.
substrate_assert_deploy_clone() {
  local cfg_clone; cfg_clone="$(cd "$DEPLOY_CLONE" 2>/dev/null && pwd -P || true)"
  [[ -n "$cfg_clone" ]] || die "DEPLOY_CLONE does not exist: $DEPLOY_CLONE"
  [[ "$SELF_CLONE" == "$cfg_clone" ]] \
    || die "running from $SELF_CLONE but config DEPLOY_CLONE=$cfg_clone — refusing to mutate"
}

# ---- atomic install ---------------------------------------------------------
# atomic_install SRC DST MODE — install SRC's contents at DST via a SAME-DIRECTORY rename so the
# swap is truly atomic. A bare `mv /tmp/x $dst` degrades to copy+unlink across filesystems (mktemp
# lands in /private/tmp, which may be a different volume than $MYNDAIX_HOME / ~/Library) — a crash
# mid-copy then leaves a partial file (cross-family review MAJOR). Staging in dirname($dst)
# guarantees rename(2), not copy. A running process keeps the old inode; new invocations get new.
atomic_install() {
  local src="$1" dst="$2" mode="$3" tmp
  tmp="$(mktemp "$(dirname "$dst")/.reconcile.XXXXXX")"
  # Clean the dest-side temp on ANY failure path (else .reconcile.* garbage accumulates in $LA_DIR
  # under repeated cp/chmod/mv failures — cross-family review MINOR) and propagate the error.
  if ! cp "$src" "$tmp" || ! chmod "$mode" "$tmp" || ! mv -f "$tmp" "$dst"; then
    rm -f "$tmp"; return 1
  fi
  rm -f "$src"
}

# ---- launchctl --------------------------------------------------------------
la_loaded()    { launchctl print "$LA_DOMAIN/$1" >/dev/null 2>&1; }
la_bootout()   { launchctl bootout "$LA_DOMAIN/$1" 2>/dev/null || true; }   # idempotent
la_bootstrap() { launchctl bootstrap "$LA_DOMAIN" "$1"; }                   # $1 = plist path
la_kickstart() { launchctl kickstart -k "$LA_DOMAIN/$1"; }

# la_wait_gone LABEL TIMEOUT — poll until the label's process is actually gone after a
# bootout (bootout can return before the process dies; the Option-A reset must not race
# a still-reading tick — design §2.3). Best-effort: returns 0 when unloaded.
la_wait_gone() {
  local label="$1" timeout="${2:-30}" deadline
  deadline=$(( $(date +%s) + timeout ))
  while la_loaded "$label"; do
    [[ $(date +%s) -ge $deadline ]] && { log "WARN: $label still loaded after ${timeout}s"; return 1; }
    sleep 1
  done
  return 0
}

# la_ensure_gone LABEL [T1] [T2] — bootout ALREADY issued by the caller; wait T1s, then SIGKILL-
# escalate, then wait T2s. Returns 0 iff the process is proven gone, 1 if it SURVIVED SIGKILL. This
# is the escalation bootstrap-fetch inlines for its pre-reset quiesce (§2.3); the auto-revert path
# uses it so a mutating tick that won't die can't have its scripts rewritten by `reset --hard`
# mid-run. NO die (the caller decides): reconcile's health_gate must never die, but its revert path
# turns a 1 into die (a tick surviving SIGKILL is a genuine human-needed anomaly).
la_ensure_gone() {
  local label="$1" t1="${2:-30}" t2="${3:-10}"
  la_wait_gone "$label" "$t1" && return 0
  log "WARN: $label survived ${t1}s bootout — SIGKILL escalate"
  launchctl kill -9 "$LA_DOMAIN/$label" 2>/dev/null || true
  la_wait_gone "$label" "$t2"
}
