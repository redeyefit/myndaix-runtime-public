#!/usr/bin/env bash
# bootstrap-fetch — the STATIC Stage-0 fetcher (design §2.2). Canonical source lives here
# in the repo; the RUNNING copy is installed to $MYNDAIX_HOME/bin/bootstrap-fetch by
# `reconcile.sh --update-bootstrap` and is NEVER auto-overwritten by a reconcile. That
# separation is the whole point: a broken reconcile.sh (or config_parse.py) landing in
# origin/main can't brick the fetch — this file always resets the fix in.
#
# SELF-CONTAINED on purpose: it does NOT source lib.sh or run the clone's Python (both
# could be the very files a bad commit broke). It reads only DEPLOY_CLONE from config
# with a minimal, non-executing grep, hard-validates it, resets it to origin/main, then
# re-execs the (now-fresh) reconcile exactly once via the RECONCILE_BOOTSTRAPPED guard.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# Bound the network fetch so a hung SSH connection can't stall the poll (macOS has no `timeout`).
export GIT_SSH_COMMAND="ssh -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o BatchMode=yes"

# liveness-fire: every invocation writes >=1 line to the launchd .out (every exit path goes
# through log/die, and stderr shares the .out via StandardErrorPath) — liveness-canary reads
# that mtime as execution evidence for the ARMED reconcile poll.
log() { printf '[%s] [bootstrap-fetch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ALARM: $*" >&2; exit 1; }

# Re-exec loop guard: if we're already inside a bootstrapped invocation, something is
# wrong — never fetch+reset+re-exec a second time.
[[ -n "${RECONCILE_BOOTSTRAPPED:-}" ]] && die "RECONCILE_BOOTSTRAPPED already set — refusing to re-bootstrap"

MYNDAIX_HOME="${MYNDAIX_HOME:-$HOME/.myndaix}"
CONFIG_FILE="$MYNDAIX_HOME/config.env"
[[ -f "$CONFIG_FILE" ]] || die "no config.env at $CONFIG_FILE"

# Minimal, NON-executing extraction of exactly two keys. `cut`/`tr` only — never source.
read_key() {
  local key="$1" line val
  line="$(grep -E "^${key}=" "$CONFIG_FILE" | head -1 || true)"
  val="${line#*=}"
  # strip one layer of matching quotes
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}

MACHINE_ROLE="$(read_key MACHINE_ROLE)"
DEPLOY_CLONE="$(read_key DEPLOY_CLONE)"
[[ -z "$DEPLOY_CLONE" ]] && DEPLOY_CLONE="$MYNDAIX_HOME/deploy/myndaix-runtime"

# Only the FACTORY auto-fetches+resets. LAB never runs this (its reconcile poll is not
# installed); a stray invocation on LAB must not reset a dev checkout.
[[ "$MACHINE_ROLE" == "factory" ]] || die "bootstrap-fetch only runs on a factory machine (role=$MACHINE_ROLE)"

# DISARM belt (cross-family r3 HIGH #4c): the reconcile POLL injects RECONCILE_POLL=1 (its plist's
# env_literal). If a poll-fired invocation finds the arm-sentinel gone — a "disarmed" poll that a
# failed bootout left LOADED — HOLD: do not fetch/reset/deploy. This is defence-in-depth behind
# reconcile's disarm bootout; it must NOT gate MANUAL converges (a human running reconcile.sh has no
# RECONCILE_POLL set), which are legitimate while the poll is disarmed. Keep the sentinel name in sync
# with reconcile.sh RECONCILE_SENTINEL + the poll descriptor's requires_sentinel (test.sh asserts).
if [[ "${RECONCILE_POLL:-0}" == "1" && ! -e "$MYNDAIX_HOME/RECONCILE_ARMED" ]]; then
  log "poll fired while DISARMED (RECONCILE_ARMED absent) — holding, not deploying (a stale loaded poll?)"
  exit 0
fi

# Hard-validate DEPLOY_CLONE before ANY reset — this is the path we `reset --hard`.
case "$DEPLOY_CLONE" in
  /*) : ;;                                   # absolute
  *)  die "DEPLOY_CLONE not absolute: $DEPLOY_CLONE" ;;
esac
[[ "$DEPLOY_CLONE" == *".."* ]] && die "DEPLOY_CLONE contains '..': $DEPLOY_CLONE"
[[ -d "$DEPLOY_CLONE/.git" ]] || die "DEPLOY_CLONE is not a git repo: $DEPLOY_CLONE"
# Compare PHYSICAL paths (pwd -P): git --show-toplevel resolves symlinks, and on macOS a clone
# under /tmp or /var is symlinked to /private/... — a logical `pwd` would falsely mismatch.
top="$(cd "$(git -C "$DEPLOY_CLONE" rev-parse --show-toplevel 2>/dev/null || echo /nonexistent)" 2>/dev/null && pwd -P || true)"
real_deploy="$(cd "$DEPLOY_CLONE" && pwd -P)"
[[ -n "$top" && "$top" == "$real_deploy" ]] || die "DEPLOY_CLONE is not a git worktree TOPLEVEL ($DEPLOY_CLONE -> $top)"

QUIESCE_LABELS=(ai.myndaix.controller ai.myndaix.automerge ai.myndaix.fix-sweep ai.myndaix.proposer)
DOMAIN="gui/$(id -u)"
LA_DIR="$HOME/Library/LaunchAgents"

# --only-if-changed SHORT-CIRCUIT (borrowed from ansible-pull; the one gap prior-art review found).
# Do the READ-ONLY fetch FIRST, then skip the whole expensive quiesce/reset/converge dance when the
# machine is already converged at an unchanged origin/main. This shrinks the quiesce blast radius to
# REAL deploys only. Safe because RUNNING_SHA is written LAST (the receipt) — so equality across all
# three proves the LAST converge fully succeeded at this SHA. A new SHA, or an incomplete/failed
# prior converge (RUNNING_SHA != HEAD), or a never-converged machine all fall through and proceed.
# (Same-SHA artifact drift is still detected + alerted by the drift-canary's --dry-run; the poll just
# doesn't auto-correct it — a documented tradeoff, since pull-only FACTORY shouldn't drift at a fixed SHA.)
log "fetching origin/main into $real_deploy"
# One retry absorbs a transient refs/remotes/origin/main.lock race with the canary's fetch.
git -C "$real_deploy" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main' \
  || { log "fetch failed — one retry"; sleep 2; git -C "$real_deploy" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main'; } \
  || die "git fetch failed twice (retry next poll)"
origin_sha="$(git -C "$real_deploy" rev-parse refs/remotes/origin/main)"
head_sha="$(git -C "$real_deploy" rev-parse HEAD)"
running_sha="$(cat "$MYNDAIX_HOME/state/RUNNING_SHA" 2>/dev/null || echo none)"
# QUARANTINE HOLD (review M2): a prior converge auto-reverted this exact origin SHA (it failed the
# health gate). Do NOT reset to it again — that would thrash (reset→converge→fail→revert) every poll
# until a human pushes a fix. Hold at the last-good RUNNING_SHA; reconcile already alerted (once). The
# quarantine is cleared by reconcile on the next clean converge (when origin advances to a good SHA).
quarantined="$(cat "$MYNDAIX_HOME/state/QUARANTINED_SHA" 2>/dev/null || echo none)"
if [[ "$quarantined" != none && "$origin_sha" == "$quarantined" ]]; then
  log "origin ${origin_sha:0:8} is QUARANTINED (a prior auto-revert) — holding at last-good, not deploying"
  exit 0
fi
if [[ "$origin_sha" == "$head_sha" && "$head_sha" == "$running_sha" ]]; then
  # SHA unchanged AND the last converge fully succeeded (RUNNING_SHA is written last). Skip the
  # expensive quiesce/reset ONLY if there is also NO drift — otherwise fall through to converge so
  # same-SHA drift (a hand-edited plist, an orphan, an unloaded label) is AUTO-CORRECTED, not merely
  # canary-alerted (design G3 — cross-family review BLOCKER).
  # BUT verify the clone WORKING TREE is clean FIRST, with git here in the trusted static fetcher —
  # a working-tree-only tamper of the clone's reconcile.sh/manifest.py could otherwise false-green the
  # clone's own dry-run (cross-family review r3 BLOCKER). A dirty tree => converge (reset+clean fixes it).
  # Capture status as the FIRST && operand so a git ERROR (non-zero exit, possibly empty stdout —
  # repo corruption / stale lock) short-circuits to converge, not a false "clean" (r4 BLOCKER: a
  # bare `[[ -z "$(git status)" ]]` drops git's exit code).
  if tree_status="$(git -C "$real_deploy" status --porcelain)" \
     && [[ -z "$tree_status" ]] \
     && /bin/bash "$real_deploy/substrate/reconcile.sh" --dry-run >/dev/null 2>&1; then
    log "already converged at ${origin_sha:0:8}; origin unchanged + tree clean + no drift — skip (only-if-changed)"
    exit 0
  fi
  log "origin unchanged but tree-dirty or DRIFT — converging to auto-correct"
else
  log "converge needed: origin=${origin_sha:0:8} head=${head_sha:0:8} running=${running_sha:0:8}"
fi

# QUIESCE-BRACKETS-THE-RESET (design §2.3, risk #2). Under Option A launchd runs the tick scripts
# DIRECTLY from the clone; a `reset --hard` rewrites those files in place (git working-tree writes
# are NOT atomic), so a mutating tick mid-read could execute a half-written script. Bootout the
# mutating ticks and PROVE they are gone BEFORE the reset. EXPLICIT allowlist (risk #1) — never a
# wildcard. The read-only drift-canary is deliberately NOT quiesced. NOTE: QUIESCE_LABELS is asserted
# == the mutating-tick descriptors by substrate/test.sh.
#
# If we abort AFTER quiescing but BEFORE handing off to reconcile, RESTORE the ticks so a failed
# converge never leaves autonomy silently down (the drift-canary still shouts either way).
restore_ticks() {
  local l
  for l in "${QUIESCE_LABELS[@]}"; do
    if [[ -f "$LA_DIR/$l.plist" ]]; then launchctl bootstrap "$DOMAIN" "$LA_DIR/$l.plist" 2>/dev/null || true; fi
  done
}
trap 'restore_ticks' EXIT

for label in "${QUIESCE_LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
done

# Wait — FAIL CLOSED — for each label to actually be gone (bootout can return before the process
# exits). Escalate SIGKILL after 30s; if a tick is STILL up, REFUSE the reset. Rewriting a live
# tick's scripts is the exact race the quiesce exists to prevent, so "couldn't prove it exited"
# must abort (retry next poll), never proceed. Matches lib.sh la_wait_gone + reconcile die-on-timeout.
for label in "${QUIESCE_LABELS[@]}"; do
  deadline=$(( $(date +%s) + 30 )); killed=0
  while launchctl print "$DOMAIN/$label" >/dev/null 2>&1; do
    if [[ $(date +%s) -ge $deadline ]]; then
      if [[ "$killed" == 0 ]]; then
        log "WARN: $label still up after 30s — SIGKILL escalate"
        launchctl kill -9 "$DOMAIN/$label" 2>/dev/null || true
        killed=1; deadline=$(( $(date +%s) + 10 ))
      else
        die "quiesce FAILED: $label still up after SIGKILL — refusing reset (would rewrite a live tick's scripts)"
      fi
    fi
    sleep 1
  done
done

# Live-process guards below FAIL CLOSED: pgrep exits 1 for "no match" and 0 for a match; anything
# else (2 = bad pattern, 3 = fatal) means we could not look, so the reset is deferred exactly as if
# something were running (P2 review r2/r3). The rc is logged; pgrep's stderr stays visible.
_pgrep_rc() { local rc=0; pgrep -f "$1" >/dev/null || rc=$?; echo "$rc"; }

# Abandoned-worker guard (H2): the controller detaches (abandon_process_group) a play-review/
# play-fix worker that outlives the tick. If one is running FROM the deploy clone (Option-A
# topology), reset --hard would rewrite its scripts mid-execution — refuse. (Latent until
# PLAY_SELF is injected; under the current topology the worker runs from $ORCH, outside the reset
# target — but fail-closed regardless.)
_pg_rc="$(_pgrep_rc "$real_deploy/orchestrator/play-")"
if [ "$_pg_rc" -ne 1 ]; then
  die "quiesce FAILED: a worker is running from the deploy clone (pgrep rc=$_pg_rc) — refusing reset (retry next poll)"
fi
# PR-gate guard: a reset must never land while a gate is LOADING its code (P2 review r1 #1 / r2).
# It works by composition with the Mini `mxr` wrapper, which refuses to start a gate (exit 78, the
# workflow retries) while the clone is behind origin/main or dirty:
#   - this check runs AFTER the fetch above, so from here on the clone is behind origin/main;
#   - a gate started BEFORE this check is seen by it — the pattern matches the `mxr pr-gate`
#     wrapper AND the `runtime.cli pr-gate` python it execs, in any flag order, so there is no
#     unseen gap between them -> reset deferred;
#   - a gate started AFTER this check hits the wrapper's guard (behind, or dirty mid-reset) and is
#     refused until the reset has fully landed -> it loads one consistent tree.
# pr-gate imports everything and reads its schema at startup, so a reset after that load can at
# worst fail that run's verdict parse (-> reviewer unavailable). Keep the pattern matching BOTH
# processes and keyed on the SUBCOMMAND, never on a flag (r3: `--pr 1 --repo X` is valid argparse).
_pg_rc="$(_pgrep_rc '(mxr|runtime\.cli) pr-gate( |$)')"
if [ "$_pg_rc" -ne 1 ]; then
  die "quiesce FAILED: a PR gate is running (pgrep rc=$_pg_rc) — refusing reset (retry next poll)"
fi

git -C "$real_deploy" reset --hard refs/remotes/origin/main
# Remove stray UNTRACKED files too (cross-family review BLOCKER): reset --hard only reverts tracked
# files, so an untracked substrate/plists/*.json could otherwise be rendered+installed by reconcile.
# `-ffd` (NOT -x) removes untracked files+dirs but KEEPS gitignored state (the in-tree .venv). The
# deploy clone is pull-only, so nothing legitimate is untracked here.
git -C "$real_deploy" clean -ffd
log "reset+clean to $(git -C "$real_deploy" rev-parse --short HEAD)"

RECONCILE="$real_deploy/substrate/reconcile.sh"
[[ -f "$RECONCILE" ]] || die "reconcile.sh missing after reset: $RECONCILE"

# Hand off — reconcile owns restarting the ticks (its step 6). Clear our restore trap so the
# ticks aren't double-managed across the exec (exec replaces this process; EXIT would not fire
# anyway, but be explicit).
trap - EXIT
export RECONCILE_BOOTSTRAPPED=1
export MYNDAIX_HOME
log "re-exec reconcile (bootstrapped)"
exec /bin/bash "$RECONCILE" "$@"
