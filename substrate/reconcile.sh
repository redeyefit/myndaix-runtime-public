#!/usr/bin/env bash
# reconcile.sh — the GitOps pull-reconcile converger (design §2.2).
#
#   reconcile.sh                 converge this machine to origin/main (factory; via Stage 0)
#   reconcile.sh --dry-run       NON-destructive drift report (any machine); exit 1 on drift
#   reconcile.sh --update-bootstrap   (re)install the static $MYNDAIX_HOME/bin/bootstrap-fetch
#
# The converge path is reached only AFTER bootstrap-fetch has fetched+reset the deploy
# clone and re-exec'd us with RECONCILE_BOOTSTRAPPED=1 (so we run the fresh code against
# the fresh schema). --dry-run NEVER triggers Stage 0 (no reset).
set -euo pipefail
SUBSTRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=substrate/lib.sh
source "$SUBSTRATE_DIR/lib.sh"

MODE=converge
case "${1:-}" in
  --dry-run)          MODE=dry-run ;;
  --update-bootstrap) MODE=update-bootstrap ;;
  "")                 MODE=converge ;;
  *)                  die "unknown arg: ${1:-} (use --dry-run | --update-bootstrap)" ;;
esac

# ---- --update-bootstrap: install the STATIC fetcher (explicit, human-approved) ----------
if [[ "$MODE" == update-bootstrap ]]; then
  substrate_resolve_home
  mkdir -p "$MYNDAIX_HOME/bin"
  tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
  cp "$SUBSTRATE_DIR/bootstrap-fetch.sh" "$tmp"
  atomic_install "$tmp" "$MYNDAIX_HOME/bin/bootstrap-fetch" 0755
  trap - EXIT
  log "installed static bootstrap-fetch -> $MYNDAIX_HOME/bin/bootstrap-fetch"
  exit 0
fi

# ---- STAGE 0: static bootstrap dispatch (converge only; dry-run never resets) ------------
if [[ "$MODE" == converge && -z "${RECONCILE_BOOTSTRAPPED:-}" ]]; then
  substrate_resolve_home
  BF="$MYNDAIX_HOME/bin/bootstrap-fetch"
  [[ -x "$BF" ]] || die "static bootstrap-fetch missing ($BF) — run: reconcile.sh --update-bootstrap"
  log "dispatching Stage-0 bootstrap-fetch"
  exec /bin/bash "$BF"
fi

# Autonomy-halt guard (cross-family review MAJOR): in a BOOTSTRAPPED converge, bootstrap-fetch has
# already quiesced the mutating ticks and cleared its own restore trap. Arm OUR restore trap NOW —
# BEFORE config-load / deploy-clone-assert — so a failure in that early window can't leave autonomy
# silently down. The restore uses only fixed labels + LA_DIR (no config), so it's valid this early.
# Cleared after step 6 restarts the ticks. Must match bootstrap-fetch QUIESCE_LABELS (test.sh asserts).
MUTATING_TICKS=(ai.myndaix.controller ai.myndaix.automerge ai.myndaix.fix-sweep ai.myndaix.proposer)
# The arm-sentinel that gates the unattended reconcile poll. MUST equal the poll descriptor's
# requires_sentinel (substrate/plists/ai.myndaix.reconcile.json) — asserted by test.sh. Re-read ON DISK
# at the disarm site so a mid-converge disarm isn't missed by a stale ROLE_LABELS snapshot (r3 HIGH #4).
RECONCILE_SENTINEL="RECONCILE_ARMED"
LA_DIR="$HOME/Library/LaunchAgents"
reconcile_restore_ticks() {
  local l
  for l in "${MUTATING_TICKS[@]}"; do
    if [[ -f "$LA_DIR/$l.plist" ]]; then launchctl bootstrap "$LA_DOMAIN" "$LA_DIR/$l.plist" 2>/dev/null || true; fi
  done
}
[[ "$MODE" == converge && -n "${RECONCILE_BOOTSTRAPPED:-}" ]] && trap 'reconcile_restore_ticks' EXIT

substrate_load_config
STATE_DIR="$MYNDAIX_HOME/state"
mkdir -p "$STATE_DIR"

fetch_origin() {
  git -C "$DEPLOY_CLONE" fetch --no-tags --prune origin '+refs/heads/main:refs/remotes/origin/main'
}

# ---- --dry-run: the real drift detector (design §2.6) -----------------------------------
if [[ "$MODE" == dry-run ]]; then
  # A fetch failure means we CANNOT know the remote state — that is drift/UNKNOWN, never "clean"
  # (cross-family review MAJOR: "ambiguity => drift"). Comparing against a stale local ref would
  # false-green a machine that has fallen behind an unreachable origin.
  fetch_origin 2>/dev/null || { log "DRIFT: fetch failed — cannot determine remote state (UNKNOWN)"; exit 1; }
  set +e
  report="$(python3 "$SUBSTRATE_DIR/manifest.py" check "$CONFIG_FILE" 2>&1)"; rc=$?
  set -e
  printf '%s\n' "$report"
  if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then
    log "DRIFT: git status failed — unknown tree state"; exit 1
  fi
  if [[ -n "$porcelain" ]]; then
    log "DRIFT: working tree not clean"; printf '%s\n' "$porcelain"; rc=1
  fi
  if [[ "$rc" -ne 0 ]]; then log "DRIFT detected"; exit 1; fi
  log "no drift"; exit 0
fi

# =========================================================================================
# CONVERGE (factory only; reached bootstrapped, so the tree is already reset to origin/main)
# =========================================================================================
[[ "$MACHINE_ROLE" == "factory" ]] || die "converge is factory-only (role=$MACHINE_ROLE); LAB uses --dry-run"
substrate_assert_deploy_clone   # restore trap already armed above (bootstrapped converge)

# Capture the LAST-GOOD SHA before any mutation — RUNNING_SHA is written last, so it only ever names
# a fully-converged SHA. This is what auto-revert (§2.8) reverts to on a health-gate failure.
PREV_GOOD="$(cat "$STATE_DIR/RUNNING_SHA" 2>/dev/null || echo none)"
HEAD_SHA="$(git -C "$DEPLOY_CLONE" rev-parse HEAD)"
prev_recoverable() { [[ "$PREV_GOOD" != none ]] && git -C "$DEPLOY_CLONE" cat-file -e "${PREV_GOOD}^{commit}" 2>/dev/null; }

# MIGRATION ADDITIVITY LINT on the prev_good..HEAD DELTA (auto-revert precondition, §2.8): a
# non-additive migration in this deploy would make a code-revert-against-the-applied-schema unsafe,
# so refuse to auto-deploy it (a contraction is a deliberate human-gated two-release change). Only
# the delta is linted — historical migrations may contain legitimate one-time contractions.
if prev_recoverable; then
  # --diff-filter=AMR: catch ADDED, MODIFIED, and RENAMED migrations (adversarial review M1) — serve
  # re-runs the CURRENT on-disk content of every migration on boot, so an in-place EDIT to a shipped
  # migration also executes and must be linted. Capture into a var + check exit (a process-sub failure
  # would fail-OPEN and silently skip the lint).
  if ! delta="$(git -C "$DEPLOY_CLONE" diff --name-only --diff-filter=AMR "${PREV_GOOD}..HEAD" -- 'src/runtime/ledger/migrations/*.sql')"; then
    die "could not compute migration delta ${PREV_GOOD:0:8}..HEAD (git diff failed) — refusing to converge"
  fi
  new_migs=()
  while IFS= read -r m; do [[ -n "$m" ]] && new_migs+=("$m"); done <<< "$delta"
  if [[ ${#new_migs[@]} -gt 0 ]]; then
    # RECONCILE_ALLOW_ROUTINE (operator-gated) is a NARROW escape: it drops only the CREATE/DROP-routine
    # rules (for a blessed additive trigger/util function), keeping every other contraction check active
    # — unlike RECONCILE_ALLOW_CONTRACTION, which skips the whole lint (r5 FP-7).
    lint_flags=()
    [[ "${RECONCILE_ALLOW_ROUTINE:-0}" == "1" ]] && lint_flags+=(--allow-routine)
    if [[ "${RECONCILE_ALLOW_CONTRACTION:-0}" == "1" ]]; then
      log "WARN: RECONCILE_ALLOW_CONTRACTION=1 — skipping the additive-migration lint (blessed contraction)"
    else
      # PR-1d: give the lint the relations that ALREADY EXIST. The new migrations have NOT applied yet (the
      # lint is a PRE-converge precondition), so the live pg_catalog reflects prev_good's schema. An op on a
      # relation BORN this deploy (a new table's UNIQUE INDEX, a new view) is then additive without an escape,
      # while a DROP/tighten on a pre-existing relation is still rejected. FAIL-CLOSED: a psql failure just
      # skips --existing and the lint falls back to its conservative same-migration rule (bounded connect via
      # lib.sh PGCONNECT_TIMEOUT + statement_timeout; the pre-existing set never WEAKENS the gate).
      existing_rel="$STATE_DIR/existing_relations.txt"
      if PGOPTIONS='-c statement_timeout=10s' psql "$MYNDAIX_DSN" -v ON_ERROR_STOP=1 -tAqc \
           "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','i','I','S','v','m','c','f','p') AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')" \
           > "$existing_rel.tmp" 2>/dev/null; then
        mv -f "$existing_rel.tmp" "$existing_rel"
        lint_flags+=(--existing "$existing_rel")
      else
        rm -f "$existing_rel.tmp"
        log "WARN: could not read pg_catalog for pre-existing relations — additivity lint runs conservative (an idempotent create+index migration may need RECONCILE_ALLOW_CONTRACTION)"
      fi
      if ! ( cd "$DEPLOY_CLONE" && python3 "$SUBSTRATE_DIR/migration_lint.py" ${lint_flags[@]+"${lint_flags[@]}"} "${new_migs[@]}" ); then
        die "NON-ADDITIVE migration in ${PREV_GOOD:0:8}..HEAD — refusing unattended auto-deploy (§2.8; RECONCILE_ALLOW_CONTRACTION=1 to override a blessed one, or RECONCILE_ALLOW_ROUTINE=1 for a blessed function)"
      fi
    fi
  fi
fi

# --- install_artifacts: tree guard + dep-sync + render/install role plists + orphan prune. -------
# Re-runnable (called again after an auto-revert reset). Sets the global ROLE_LABELS.
install_artifacts() {
  local porcelain
  # 1. tree guard — FAIL CLOSED on a dirty/untracked tree (a git ERROR also fails closed, not `|| true`).
  if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then die "git status failed — unknown tree state"; fi
  [[ -z "$porcelain" ]] || die "deploy clone NOT clean — refusing to converge:"$'\n'"$porcelain"
  log "converging to $(git -C "$DEPLOY_CLONE" rev-parse --short HEAD)"

  # 2. dep-sync — install when pyproject changed OR the venv is fresh/invalid (repair a deleted venv).
  local VENV cur_dep dep_rec need_install=0
  VENV="$DEPLOY_CLONE/.venv"
  if [[ ! -x "$VENV/bin/pip" ]]; then log "creating venv $VENV"; python3 -m venv "$VENV" || die "venv creation failed"; need_install=1; fi
  cur_dep="$(shasum -a 256 "$DEPLOY_CLONE/pyproject.toml" | cut -d' ' -f1)"
  dep_rec="$STATE_DIR/venv_source.sha"
  [[ ! -f "$dep_rec" || "$(cat "$dep_rec" 2>/dev/null || true)" != "$cur_dep" ]] && need_install=1
  if [[ "$need_install" == 1 ]]; then
    log "installing deps into venv"
    "$VENV/bin/pip" install -q -e "$DEPLOY_CLONE" || die "pip install failed"
    { printf '%s\n' "$cur_dep" > "$dep_rec.tmp" && mv -f "$dep_rec.tmp" "$dep_rec"; } || die "could not write venv_source.sha"
  fi

  # 3. render + install role plists atomically. A SENTINEL-GATED job (the reconcile poll) installs
  #    ONLY when its sentinel exists — so unattended auto-deploy is an explicit opt-in (§2.8).
  mkdir -p "$LA_DIR"
  ROLE_LABELS=()
  local desc rcheck label sentinel tmp
  for desc in "$SUBSTRATE_DIR"/plists/*.json; do
    set +e; python3 "$SUBSTRATE_DIR/render_plist.py" role-check "$desc" "$MACHINE_ROLE"; rcheck=$?; set -e
    case "$rcheck" in
      0) : ;;
      1) continue ;;                                                # not this role
      *) die "descriptor error (role-check rc=$rcheck): $desc" ;;   # broken descriptor — fail closed
    esac
    label="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["label"])' "$desc")" || die "cannot read label from $desc"
    sentinel="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("requires_sentinel",""))' "$desc")" || die "cannot read $desc"
    if [[ -n "$sentinel" && ! -e "$MYNDAIX_HOME/$sentinel" ]]; then
      log "skip $label — sentinel '$sentinel' not set (unarmed)"; continue
    fi
    ROLE_LABELS+=("$label")
    tmp="$(mktemp)"
    if ! python3 "$SUBSTRATE_DIR/render_plist.py" render "$desc" "$CONFIG_FILE" > "$tmp" 2>>"$STATE_DIR/reconcile.err"; then
      rm -f "$tmp"; die "render failed for $label (see $STATE_DIR/reconcile.err)"
    fi
    plutil -lint "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "rendered plist for $label failed plutil -lint"; }
    # Content-aware install: only rewrite when the rendered plist DIFFERS from what's installed.
    # An unconditional atomic_install cp+mv mints a fresh inode+mtime every converge, which would
    # make the installed-plist mtime a lie — the liveness-canary keys its reconcile-grace on that
    # mtime ("just (re)installed, hasn't run yet"), so a rewrite on every unrelated deploy would
    # perpetually re-arm the grace and leave long-gap jobs (inbox-assistant, 25h) effectively
    # unwatched (deep-audit P2). cmp is byte-exact and render is deterministic, so "identical" ==
    # "nothing to converge" — skipping is correct AND cuts needless launchd-plist churn.
    # The skip requires a REGULAR file (not a symlink) at the intended mode 0644 too: a
    # byte-identical but wrong-mode / symlinked / non-regular installed plist is NOT converged and
    # must be repaired, not skipped (KilaBz re-review — convergence completeness on the load-bearing
    # LaunchAgents path). Anything off-spec falls through to a real atomic_install.
    inst="$LA_DIR/$label.plist"
    # Separate assignments joined by || (NOT `A || B` inside one substitution): GNU `stat -f`
    # means --file-system and leaks a multiline block to stdout while exiting nonzero; a chained
    # substitution would capture that garbage. Here a failed `stat -f` (Linux) just re-assigns
    # imode from `stat -c %a`. macOS BSD `stat -f %Lp` wins on the first try.
    imode="$(stat -f %Lp "$inst" 2>/dev/null)" || imode="$(stat -c %a "$inst" 2>/dev/null)" || imode=""
    if [[ -f "$inst" && ! -L "$inst" && "$imode" == "644" ]] && cmp -s "$tmp" "$inst"; then
      rm -f "$tmp"
      log "plist unchanged: $label"
    else
      atomic_install "$tmp" "$inst" 0644
      log "installed plist: $label"
    fi
  done

  # 3.5 ORPHAN PRUNE — a previously-managed label no longer in ROLE_LABELS (descriptor removed / role
  #     changed / sentinel disarmed) is booted out + its plist removed. SCOPED to state/managed_labels
  #     (what reconcile itself installed) — NEVER a wildcard over ai.myndaix.* (risk-#1 catastrophe).
  local MANAGED_REC prev still l
  MANAGED_REC="$STATE_DIR/managed_labels"
  if [[ -f "$MANAGED_REC" ]]; then
    while IFS= read -r prev; do
      [[ -n "$prev" ]] || continue
      [[ "$prev" == "ai.myndaix.runtime" ]] && continue
      still=0; for l in ${ROLE_LABELS[@]+"${ROLE_LABELS[@]}"}; do [[ "$l" == "$prev" ]] && { still=1; break; }; done
      if [[ "$still" == 0 ]]; then
        # The reconcile poll is handled at the END of the script (synchronous bootout — it may be the
        # job we're running under, so it can't self-suicide mid-converge, and manifest --health-only
        # already excludes it as transitionally-disarmed). Everything else: bootout + remove now.
        [[ "$prev" == "ai.myndaix.reconcile" ]] && continue
        log "orphan prune: $prev no longer managed — bootout + remove plist"
        la_bootout "$prev"
        rm -f "$LA_DIR/$prev.plist"
      fi
    done < "$MANAGED_REC"
  fi
}

# --- health_gate: restart serve + wait healthy+migrated + start ticks + verify. Returns 0/1 (NO die)
# so the caller can auto-revert. Reads migration_head.txt from the CURRENT tree each call (so after a
# revert it checks the reverted SHA's head object). psql CONNECT is bounded by PGCONNECT_TIMEOUT (lib.sh)
# so a flaky loopback fails-fast and the deadline logic actually fires (found live on the Mini cutover).
serve_pid() { launchctl print "$LA_DOMAIN/ai.myndaix.runtime" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid =/{gsub(/[^0-9]/,"",$2); print $2; exit}'; }
health_gate() {
  local HEAD_OBJ deadline old_pid prev_pid pid head_ok label vrc verify porcelain
  HEAD_OBJ="$(cat "$SUBSTRATE_DIR/migration_head.txt" 2>/dev/null || true)"
  # health_gate must NEVER die (its contract is return 0/1 so the caller can auto-revert). A bad
  # migration_head.txt on the NEW SHA must fall through to the revert — if the reverted SHA has the
  # same problem, the two-fault die fires (cross-family r2 CRITICAL #1).
  [[ "$HEAD_OBJ" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || { log "migration_head.txt not a plain identifier: '$HEAD_OBJ'"; return 1; }
  # 4. restart serve (sole migration owner; hand-managed plist — kickstart the existing label).
  #    Capture the OLD pid FIRST: kickstart -k does NOT block on the old process exiting (serve does a
  #    graceful multi-worker drain), so without requiring the pid to CHANGE the WAIT could false-green
  #    on the still-draining OLD serve — a broken new SHA reads healthy and gets cemented (review M3).
  la_loaded ai.myndaix.runtime || { log "serve (ai.myndaix.runtime) not loaded"; return 1; }
  old_pid="$(serve_pid)"
  log "kickstarting serve (old pid ${old_pid:-none}) to apply migrations"; la_kickstart ai.myndaix.runtime || { log "serve kickstart failed"; return 1; }
  # 5. WAIT for serve to be RESTARTED (pid != old) AND STABLE (pid unchanged across a poll) AND migrated.
  deadline=$(( $(date +%s) + 150 )); prev_pid=""
  while :; do
    pid="$(serve_pid)"
    head_ok="$(PGOPTIONS='-c statement_timeout=10s' psql "$MYNDAIX_DSN" -v ON_ERROR_STOP=1 -tAc "SELECT to_regclass('public.$HEAD_OBJ') IS NOT NULL" 2>/dev/null | tr -d '[:space:]')"
    [[ -n "$pid" && "$pid" != "$old_pid" && "$pid" == "$prev_pid" && "$head_ok" == "t" ]] && break
    prev_pid="$pid"
    [[ $(date +%s) -ge $deadline ]] && { log "health WAIT timeout: serve not restarted+stable / head '$HEAD_OBJ' absent (150s)"; return 1; }
    sleep 3
  done
  log "serve healthy (restarted pid $pid stable, was ${old_pid:-none}); migration head '$HEAD_OBJ' present"
  # 6. start the ticks (never bootout the reconcile-poll self label)
  for label in ${ROLE_LABELS[@]+"${ROLE_LABELS[@]}"}; do
    [[ "$label" == "ai.myndaix.runtime" ]] && continue
    if [[ "$label" == "ai.myndaix.reconcile" ]]; then
      # Reload so a CHANGED plist/env (e.g. the RECONCILE_POLL marker added by an upgrade) takes effect on
      # an ALREADY-loaded poll (cross-family r4 HIGH-2). Only a MANUAL converge (RECONCILE_POLL unset) can
      # safely bootout+re-bootstrap the poll — a poll-TRIGGERED run is the poll's own launchd process and
      # would SIGKILL itself mid-reload, so it merely ensures the job is loaded (its env is current by
      # definition). Arming is a manual converge, so the fresh env lands on arm. Both paths retry a
      # transient launchd EBUSY (r5 HIGH-5) like every other tick, and the manual path fail-CLOSES via
      # la_ensure_gone rather than silently skipping the bootstrap on a slow unload (r5 #8).
      if [[ "${RECONCILE_POLL:-0}" != "1" ]]; then
        la_bootout "$label"
        la_ensure_gone "$label" 10 5 || { log "poll won't quiesce for reload"; return 1; }
        la_bootstrap "$LA_DIR/$label.plist" || { sleep 2; la_bootstrap "$LA_DIR/$label.plist"; } || { log "could not (re)bootstrap poll"; return 1; }
      else
        la_loaded "$label" || la_bootstrap "$LA_DIR/$label.plist" || { sleep 2; la_bootstrap "$LA_DIR/$label.plist"; } || { log "could not bootstrap poll"; return 1; }
      fi
      continue
    fi
    la_bootout "$label"; la_wait_gone "$label" 10 || true
    la_bootstrap "$LA_DIR/$label.plist" || { sleep 2; la_bootstrap "$LA_DIR/$label.plist"; } || { log "could not bootstrap $label"; return 1; }
    log "started tick: $label"
  done
  # 7. verify manifest + tree clean
  # --health-only: post-converge health (plists/labels/orphans/venv/tree), NOT deploy-vs-origin SHA
  # currency — an AUTO-REVERT intentionally leaves deploy=last-good while origin=bad, and that must
  # not read as a failed converge (cross-family review CRITICAL #1).
  set +e; verify="$(python3 "$SUBSTRATE_DIR/manifest.py" check --health-only "$CONFIG_FILE" 2>&1)"; vrc=$?; set -e
  if ! porcelain="$(git -C "$DEPLOY_CLONE" status --porcelain)"; then log "verify: git status failed"; return 1; fi
  if [[ "$vrc" -ne 0 || -n "$porcelain" ]]; then
    printf '%s\n' "$verify"; [[ -n "$porcelain" ]] && printf 'tree dirty:\n%s\n' "$porcelain"
    log "post-converge verify FAILED — manifest/tree drift"; return 1
  fi
  return 0
}

# ---- converge with AUTO-REVERT (§2.8) ---------------------------------------------------------
install_artifacts
reverted=0
if health_gate; then
  converged_sha="$HEAD_SHA"
else
  log "HEALTH-GATE FAILED at ${HEAD_SHA:0:8}"
  if prev_recoverable; then
    log "AUTO-REVERT to last-good ${PREV_GOOD:0:8} (§2.8) — FACTORY must not stay on broken code"
    # QUIESCE the mutating ticks BEFORE the reset — health_gate may have already started them (step 6
    # runs before its verify), and a `reset --hard` rewrites their in-clone scripts in place. Prove
    # them gone first — FAIL CLOSED (SIGKILL-escalate then die), exactly like bootstrap-fetch's pre-reset
    # quiesce. A `log WARN` that let the reset proceed against a live tick reopened the exact race the
    # quiesce exists to close (cross-family r3 HIGH #3): a tick surviving SIGKILL is a human-needed
    # anomaly, and dying here leaves FACTORY on the (bad) SHA with the restore trap re-arming the ticks —
    # the same posture as an unrecoverable health gate, minus the tree-corruption risk.
    for _t in "${MUTATING_TICKS[@]}"; do la_bootout "$_t"; done
    for _t in "${MUTATING_TICKS[@]}"; do
      la_ensure_gone "$_t" 30 10 || die "auto-revert: $_t survived SIGKILL — refusing reset (would rewrite a live tick mid-run); human needed"
    done
    # Abandoned-worker guard (mirror bootstrap-fetch H2): the controller detaches a play-* worker that
    # can outlive its tick. If one is running FROM the deploy clone, `reset --hard` would rewrite its
    # scripts mid-execution — refuse.
    if pgrep -f "$DEPLOY_CLONE/orchestrator/play-" >/dev/null 2>&1; then
      die "auto-revert: a worker is still running from the deploy clone — refusing reset; human needed"
    fi
    git -C "$DEPLOY_CLONE" reset --hard "$PREV_GOOD" && git -C "$DEPLOY_CLONE" clean -ffd || die "auto-revert reset failed — human needed"
    install_artifacts
    if health_gate; then
      converged_sha="$PREV_GOOD"; reverted=1
    else
      die "AUTO-REVERT to ${PREV_GOOD:0:8} ALSO failed — FACTORY needs a human (two-fault, serve down)"
    fi
  else
    die "health-gate failed and no recoverable last-good SHA to revert to (first converge?) — human needed"
  fi
fi
trap - EXIT   # converged (possibly reverted) — clear the autonomy-halt restore trap

# QUARANTINE (review M2): if we auto-reverted, remember the BAD SHA so the NEXT poll doesn't reset to
# it and thrash forever (origin/main is still the bad SHA until a human pushes a fix). bootstrap-fetch
# HOLDS while origin == QUARANTINED_SHA. A clean (non-reverted) converge clears any stale quarantine.
if [[ "$reverted" == 1 ]]; then
  # FAIL-CLOSED: without QUARANTINED_SHA, bootstrap-fetch has no hold and the next poll redeploys the
  # bad SHA -> thrash. A write failure must alarm, not be swallowed (cross-family r2 HIGH #4).
  if ! { printf '%s\n' "$HEAD_SHA" > "$STATE_DIR/QUARANTINED_SHA.tmp" && mv -f "$STATE_DIR/QUARANTINED_SHA.tmp" "$STATE_DIR/QUARANTINED_SHA"; }; then
    rm -f "$STATE_DIR/QUARANTINED_SHA.tmp"
    die "failed to write QUARANTINED_SHA after auto-revert — quarantine NOT set; a human must hold FACTORY off ${HEAD_SHA:0:8}"
  fi
else
  rm -f "$STATE_DIR/QUARANTINED_SHA"
fi

# 8. COMMIT POINT — manifest receipt FIRST, then managed_labels, then RUNNING_SHA LAST (the last-good
#    marker). Explicit fail-closed if-blocks (a `printf > tmp && mv` &&-chain is exempt from set -e on
#    the non-final link — the #89 class). The `${arr[@]+...}` idiom is bash-3.2 + set -u safe on an
#    empty array (review MED — /bin/bash is 3.2 on macOS).
if ! { printf '%s\n' ${ROLE_LABELS[@]+"${ROLE_LABELS[@]}"} > "$STATE_DIR/managed_labels.tmp" && mv -f "$STATE_DIR/managed_labels.tmp" "$STATE_DIR/managed_labels"; }; then
  die "failed to write managed_labels (converge incomplete, retry next poll)"
fi
if ! { python3 "$SUBSTRATE_DIR/manifest.py" build "$CONFIG_FILE" > "$STATE_DIR/manifest.json.tmp" && mv -f "$STATE_DIR/manifest.json.tmp" "$STATE_DIR/manifest.json"; }; then
  die "manifest build failed — NOT writing RUNNING_SHA (converge incomplete, retry next poll)"
fi
if ! { printf '%s\n' "$converged_sha" > "$STATE_DIR/RUNNING_SHA.tmp" && mv -f "$STATE_DIR/RUNNING_SHA.tmp" "$STATE_DIR/RUNNING_SHA"; }; then
  die "failed to write RUNNING_SHA (converge incomplete, retry next poll)"
fi
log "CONVERGED at ${converged_sha:0:8} — receipt written"

# Loud ALARM on an auto-revert (dedup: filename keyed on the BAD SHA so repeated polls can't flood).
if [[ "$reverted" == 1 ]]; then
  log "ALARM: AUTO-REVERTED ${HEAD_SHA:0:8} -> ${converged_sha:0:8} (bad SHA failed the health gate) — investigate"
  if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
    alert="$OPERATOR_INBOX/reconcile-revert-${HEAD_SHA:0:12}.md"
    [[ -e "$alert" ]] || { printf 'reconcile AUTO-REVERTED FACTORY from %s to %s: the new SHA failed the post-restart health gate.\nFACTORY is back on known-good code. Investigate the bad SHA before re-deploying.\n' "$HEAD_SHA" "$converged_sha" > "$alert.tmp" && mv -f "$alert.tmp" "$alert"; } || rm -f "$alert.tmp"
  fi
fi

# Non-blocking canary: a green mxr proves end-to-end drain, but a quota-drained pool is a FALSE
# health signal, so this NEVER fails the converge — warn only.
if command -v mxr >/dev/null 2>&1; then
  if MXR_TIMEOUT_S=60 mxr recon "reply READY" >/dev/null 2>&1; then
    log "canary: pool drained end-to-end"
  else
    log "WARN: canary did not confirm (pool busy / quota) — schema+health already verified"
  fi
fi

# DISARM enforcement (review M4 + cross-family CRITICAL #2) — the ABSOLUTE LAST action. `rm
# RECONCILE_ARMED` deletes the poll's plist file, but the LOADED StartInterval job keeps firing and
# auto-deploying until it's booted out. If the poll is loaded but NOT managed this run (disarmed),
# bootout it SYNCHRONOUSLY here: everything (receipt, alerts, canary) is already done, so it is safe
# even if we ARE the poll process — the bootout's SIGTERM just ends an already-finished script. A
# detached `( sleep; bootout ) &` did NOT work: it shares the launchd process group and gets killed
# when the parent exits before the sleep elapses.
# Decide from the sentinel ON DISK RIGHT NOW, not the install_artifacts ROLE_LABELS snapshot: if
# RECONCILE_ARMED was removed AFTER install_artifacts ran, the snapshot still lists the poll as managed
# and the bootout would be wrongly skipped, leaving a "disarmed" poll firing forever (cross-family r3
# HIGH #4). Absent sentinel + loaded poll => bootout NOW.
if [[ ! -e "$MYNDAIX_HOME/$RECONCILE_SENTINEL" ]] && la_loaded ai.myndaix.reconcile; then
  log "reconcile poll loaded but DISARMED (sentinel '$RECONCILE_SENTINEL' absent) — bootout + remove plist (final action)"
  rm -f "$LA_DIR/ai.myndaix.reconcile.plist"
  launchctl bootout "$LA_DOMAIN/ai.myndaix.reconcile" 2>/dev/null || true
  # CONFIRM it unloaded — a silently-failed bootout leaves a disarmed poll auto-deploying (r3 HIGH #4).
  # If bootout SIGTERM'd us (we ARE the poll) we never reach here — that's a clean unload. If we survive
  # AND it's still loaded, retry once, then fail LOUD (operator alarm + die) — never `|| true` silent.
  if la_loaded ai.myndaix.reconcile; then
    sleep 2; launchctl bootout "$LA_DOMAIN/ai.myndaix.reconcile" 2>/dev/null || true
    if la_loaded ai.myndaix.reconcile; then
      if [[ -n "${OPERATOR_INBOX:-}" && -d "$OPERATOR_INBOX" ]]; then
        da="$OPERATOR_INBOX/reconcile-disarm-failed.md"
        [[ -e "$da" ]] || { printf 'reconcile could NOT bootout the DISARMED poll (ai.myndaix.reconcile): it is still loaded and will keep firing every interval. A human must run: launchctl bootout %s/ai.myndaix.reconcile\n' "$LA_DOMAIN" > "$da.tmp" && mv -f "$da.tmp" "$da"; } || rm -f "$da.tmp"
      fi
      die "DISARM FAILED: reconcile poll still loaded after bootout — a human must bootout it"
    fi
  fi
fi
