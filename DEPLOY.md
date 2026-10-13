# Deploying myndaix-runtime

There are **three deploy targets**, and a change can touch any or all:
1. **`serve`** — the worker pool + API (Python under `src/`, run as a launchd service). Covered
   just below.
2. **the orchestrator** — the autonomous review loop (`play-review.sh` + the `controller`). It has
   its OWN deploy surfaces; see [Orchestrator deploy](#orchestrator-deploy-the-review-loop). A change
   to `orchestrator/play-review.sh` does NOT ship by pulling code + restarting serve — the worker
   runs a TRUSTED INSTALLED COPY, not the repo tree.
3. **the substrate** — the GitOps watchers (`substrate/drift-canary.sh`, `liveness-canary.sh`,
   `reconcile.sh`) and the inline **mxr freshness guard**. These have their OWN paths too; see
   [Substrate deploy](#substrate-deploy-the-gitops-watchers--the-mxr-guard). A `drift-canary.sh`
   change does NOT ship by restarting serve — the watchers run from the DEPLOY CLONE, and the guard
   is a per-machine hand-splice from `SETUP.md`.

**Pasting the multi-line blocks below:** every block that can `exit 1` is wrapped in `( … )`, so
that exit — and the `set -e` some of them turn on — ends the block, never your terminal. They
carry `#` comments, which interactive zsh rejects by default (`interactivecomments` is OFF):
every comment line errors (`command not found: #`) and the `set -e` blocks stop at the first one.
Both machines' `~/.zshrc` set `setopt interactivecomments` (2026-09-24); on any other shell, run
it first. Non-interactive runs (`ssh mini '…'`, an agent's shell tool) need neither.

## TL;DR (serve)

`serve` now **auto-applies pending migrations on startup**, so the old footgun is gone:
you can deploy new code and just (re)start `serve` — it migrates the schema before it
leases any jobs.

```bash
# pull the new code, then:
MYNDAIX_DSN=postgresql://localhost/runtime PYTHONPATH=src python3 -m runtime.serve
# [serve] schema migrations ensured (idempotent): 0001_add_job_context.sql
# [serve] MyndAIX runtime up: 4-worker pool draining ...
```

On a host where `serve` runs under launchd (the Mini), restart it with:

```bash
launchctl kickstart -k gui/$(id -u)/ai.myndaix.runtime
```

## Orchestrator deploy (the review loop)

The autonomous review loop deploys across **THREE surfaces**. A deploy that updates only some of
them is a **half-deploy** — it looks done but runs a mix of old and new code. This bit us
2026-07-02: `play-review.sh` was updated but the repo tree was left on a stale branch, so the
`controller` half of the same PR silently didn't ship. Update ALL THREE:

1. **Repo working tree — must be on `main` at `origin/main`.** On the **lab** (MacBook), `serve`
   and `controller` import Python (`src/runtime/controller.py`, `registry.py`, `runner.py`) via
   `PYTHONPATH` FROM this tree, so it must be current. The `controller` spawns fresh each launchd
   tick, so it picks up tree changes on the next tick automatically; `serve` is long-lived and
   needs the restart above. Verify with `git branch --show-current` (want `main`) + `git log -1`.
   **The factory (Mini) has NO working tree** (removed 2026-09-22): `serve`, `controller`,
   automerge and the substrate jobs all run from the pull-only deploy clone, which only reconcile
   advances; the loop's git work (review diffs, fix worktrees, automerge pins) runs in the
   machine-owned work repo `~/.myndaix/work/myndaix-runtime` — the `$ORCH/repos.json` path. Never
   edit, commit in, or check out a branch in either; never recreate a checkout under `~/code` there.

2. **`$ORCH/play-review.sh` (and `play-fix.sh`) — the TRUSTED INSTALLED COPY.** The pre-push hook
   and the controller re-exec the worker from `$ORCH` (`PLAY_SELF=$HOME/.myndaix/orchestrator/
   play-review.sh`), NOT the repo copy — a defense so a push that edits the worktree script can't
   run as the worker. So a `play-review.sh` change ships ONLY when you copy it in:

   ```bash
   cp orchestrator/play-review.sh ~/.myndaix/orchestrator/play-review.sh
   ```

   (When autofix is armed, `orchestrator/autofix-arm.sh arm` does this cp for BOTH scripts + re-runs
   its gates — prefer it on an autofix host. Run it only from a clean, up-to-date `main` checkout.)
   On the Mini the tick plists set `PLAY_SELF` to the DEPLOY CLONE's `play-review.sh` (and
   `play-fix.sh` resolves next to it), so the controller/automerge never run the `$ORCH` copies;
   they matter there only for manual `--worker` runs. `deploy-sync.sh` keeps them in sync — see
   the Mini deploy below.

3. **`serve` restart** — `launchctl kickstart -k gui/$(id -u)/ai.myndaix.runtime`, to reload
   `registry.py`/`runner.py` into the long-lived pool (e.g. an agent profile-timeout or adapter
   change). Skipped only if the deploy touched nothing serve imports.

**⚠ MINI SERVE IS GITOPS — the one-liner below does NOT ship serve code there (live-verified
2026-09-11):** on the Mini, `runtime-serve.sh` execs from the DEPLOY CLONE
(`~/.myndaix/deploy/myndaix-runtime/.venv`), which only `ai.myndaix.reconcile` advances (fetch →
health-gate on `to_regclass(migration_head.txt)` → kickstart). A hand `kickstart` restarts serve
on whatever the clone already has. To force a Mini serve deploy NOW:
`launchctl kickstart gui/$(id -u)/ai.myndaix.reconcile` (proved: advanced the clone + applied a
new migration in one tick).

**The full Mini deploy** (serve + controller + substrate ride reconcile; the installed copies
follow from the SAME commit):

```bash
(
launchctl kickstart gui/$(id -u)/ai.myndaix.reconcile
# The wait is CODE, not a comment: this block gets pasted whole, and an unwaited read takes the
# PRE-merge RUNNING_SHA — deploy-sync then cleanly reinstalls the OLD copies and exits 0 (review 11391).
M=$(git -C ~/.myndaix/deploy/myndaix-runtime ls-remote origin refs/heads/main | cut -f1)
[[ "$M" =~ ^[0-9a-f]{40}$ ]] || { echo "ls-remote failed (network/auth) — cannot resolve origin/main" >&2; exit 1; }
for _ in $(seq 60); do [[ "$(cat ~/.myndaix/state/RUNNING_SHA 2>/dev/null)" == "$M" ]] && break; sleep 5; done
S=$(cat ~/.myndaix/state/RUNNING_SHA); [[ "$S" =~ ^[0-9a-f]{40}$ && "$S" == "$M" ]] || { echo "reconcile has not converged to $M (RUNNING_SHA=$S) — check reconcile, do NOT apply" >&2; exit 1; }
~/.myndaix/deploy/myndaix-runtime/orchestrator/deploy-sync.sh --apply "$S"
)
```

This installs `$ORCH/play-review.sh` and `$ORCH/play-fix.sh` from the commit reconcile
HEALTH-GATED (`RUNNING_SHA` is written last, only on a good converge) — both together, never a
partial copy (the half-deploy this doc exists to prevent). Pin the sha, never
`HEAD`: a later reconcile tick can move the clone past the gate between your wait and the apply
(review 99718 P2). If it did, deploy-sync REFUSES the skew (clone HEAD ≠ the pinned sha) — rerun
from the wait; never override with `DEPLOY_SYNC_ALLOW_SKEW`. The refusal covers the window BEFORE
validation only: a tick that lands mid-apply is not refused, and takes no lock — every installed
file is read from the pinned sha, so the install is still one consistent commit; the clone just
moved on, same as a merge landing a second later (`--preflight` flags it; the next apply covers it).
Not guarded: a `git gc` repacking the clone during the read (low probability; if the apply
errors, rerun it from the wait).

**Autofix apply rung (2026-09-11, docs/autofix-apply-rung-design.md):** verified fixes
(`SUITE_GREEN` / `REGRESSION_CHECK_ONLY`) commit + push `fix/auto/*` branches and open PRs when
`$ORCH/AUTOFIX_APPLY_ENABLED` exists. The flag is PER-MACHINE and DELIBERATE (currently armed:
MacBook only; the Mini's launchd callers hard-disable autofix). Arm `touch` / disarm `rm` —
after any play-script deploy, re-check the flag state matches intent.

**Verify the deploy landed** (read-only): `git log -1` (the merge sha — on the Mini, `git -C
~/.myndaix/deploy/myndaix-runtime log -1`), a `grep` for the new code in the repo
`src/runtime/controller.py` (the deploy clone's on the Mini) AND in BOTH installed workers
(`$ORCH/play-review.sh` and `$ORCH/play-fix.sh`), and a fresh serve pid (`launchctl print
gui/$(id -u)/ai.myndaix.runtime | grep pid`). A claimed deploy that skipped the `cp`/`deploy-sync`
runs the OLD worker(s); one that skipped the pull (lab) or reconcile's convergence (Mini) runs the
OLD controller.

## Substrate deploy (the GitOps watchers + the mxr guard)

The substrate scripts and the inline **mxr freshness guard** ship on paths the serve one-liner does
NOT cover. This was reverse-engineered from scratch twice (2026-09-21, PRs #162/#163); documented
here so it isn't a third time. Substrate is **FACTORY-only** — the MacBook lab runs none of these
launchd jobs and its `mxr` wrapper is deliberately NOT gated (its dev tree is dirty by design).

### Watchers (`drift-canary` / `liveness-canary`) — via the DEPLOY CLONE

On the Mini the substrate launchd jobs execute the script from the **deploy clone** (the Mini
has no other checkout):

```
/Users/factory/.myndaix/deploy/myndaix-runtime/substrate/drift-canary.sh   # <- what ai.myndaix.drift-canary runs
```

`ai.myndaix.reconcile` is the ONLY thing that advances that clone (fetch → health-gate on
`to_regclass(migration_head.txt)` → the clone tracks origin/main). So a substrate script change
ships like this, after the PR merges to `main`:

```bash
(
set -euo pipefail
# 1. capture the intended full merge SHA from origin (read-only — reconcile alone moves the clone):
TARGET=$(git -C ~/.myndaix/deploy/myndaix-runtime ls-remote origin refs/heads/main | cut -f1) \
  || { echo "ls-remote failed (network/auth) — cannot resolve origin/main" >&2; exit 1; }
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || { echo "origin/main did not resolve to a sha" >&2; exit 1; }
launchctl kickstart gui/$(id -u)/ai.myndaix.reconcile
# 2. wait for successful convergence, not just the clone reset:
deadline=$((SECONDS + 300))
until [[ "$(cat ~/.myndaix/state/RUNNING_SHA 2>/dev/null || true)" == "$TARGET" ]]; do
  (( SECONDS < deadline )) || { echo "Timed out waiting for convergence to $TARGET; inspect reconcile.out" >&2; exit 1; }
  echo "waiting…"; sleep 5
done
# 3. run one canary tick in the foreground and judge its stdout + exit.
#    The quiesce→snapshot→kickstart→tail-offset dance is CUT: StartInterval:900 could fire
#    into the snapshot→kickstart gap, producing an ambiguous offset that judged an autonomous
#    tick as the deliberate one (five rounds on this mechanism — third door: rip it out).
#    The script logs ≥1 verdict line per run unconditionally and exits nonzero on drift/failure
#    — foreground stdout + exit are unambiguous. Honest loss: does not prove launchd can START
#    the job; covered by the print assertion below + the liveness watcher.
#    PRIVATE state dir (DRIFT_CANARY_STATE_DIR): the launchd job stays loaded and can fire mid-run,
#    and the script's streak/latch read-modify-write is safe only for ONE instance per state dir
#    (its SINGLE-INSTANCE INVARIANT). A private-state run also delivers no alerts (the inbox is
#    live shared state; undelivered alerts are logged instead). Still shared with a live tick: the
#    deploy clone's git fetch/status — contention there fails CLOSED (a DRIFT line → rerun).
#    ANY DRIFT line fails this check, sub-threshold watches included — by design: streak grace
#    periods exist to keep the UNATTENDED alarm quiet, and an attended deploy check stays strict.
#    Bounded by perl alarm+exec (macOS has no timeout(1)): rc 142 = killed at 120s. Output goes
#    to a FILE, never $(): alarm kills only the canary's bash, and an orphaned child still holding
#    a capture pipe would block $() until IT finished — silently voiding the bound (a stub
#    `sleep 30` held a 3s-budget $() for 30s). An orphaned fetch may finish on its own; it
#    touches only the deploy clone's remote-tracking ref.
#    Verdicts are matched with [[ ]] on the captured output, not grep: in an `if`, a grep ERROR
#    (rc 2) reads the same as "no match" and would skip the deny branch (fail-open).
launchctl print gui/$(id -u)/ai.myndaix.drift-canary >/dev/null 2>&1 \
  || { echo "drift-canary service not loaded — run reconcile/bootstrap first" >&2; exit 1; }
canary_state="$(mktemp -d)"
canary_rc=0
DRIFT_CANARY_STATE_DIR="$canary_state" perl -e 'alarm shift; exec @ARGV or exit 127' 120 \
  bash ~/.myndaix/deploy/myndaix-runtime/substrate/drift-canary.sh > "$canary_state/verify.out" 2>&1 \
  || canary_rc=$?
canary_out="$(cat "$canary_state/verify.out")"
# an orphan from a timed-out run can still be writing here; a failed cleanup must not mask the verdict
rm -rf "$canary_state" || echo "note: could not remove $canary_state (orphaned canary child?)" >&2
printf '%s\n' "$canary_out"
[[ "$canary_rc" -ne 142 ]] \
  || { echo "canary did not finish within 120s (killed) — rerun; if it repeats, run reconcile.sh --dry-run by hand" >&2; exit 1; }
bad='DRIFT|ALARM|watch failed'
if [[ "$canary_rc" -ne 0 || "$canary_out" =~ $bad ]]; then
  echo "canary reported drift/failure (rc=$canary_rc) — investigate the output above before trusting this deploy" >&2
  exit 1
fi
[[ "$canary_out" == *"canary: no drift"* ]] \
  || { echo "canary printed no healthy verdict ('canary: no drift') — treat this deploy as UNVERIFIED" >&2; exit 1; }
)
```

Once reconcile has converged, install the copied surface from that health-gated commit with the
validated `S=… --apply "$S"` block in [Orchestrator deploy](#orchestrator-deploy-the-review-loop) —
never an inline `$(cat RUNNING_SHA)`: an empty value reads as no ref and deploy-sync falls back to
`origin/main`, ungated.

### The inline mxr freshness guard — hand-spliced per machine from `SETUP.md`

`SETUP.md` is the CANONICAL source of the guard block. Each machine's live `~/.local/bin/mxr`
carries a hand-copied copy — a guard sourced from the tree would rot with the very tree it guards.
The machine-specific lines (`PYTHONPATH`/venv-python) live OUTSIDE the guard, so the guard block
itself is machine-independent. The block is delimited by these two markers:

```
# --- runtime-tree freshness guard (FACTORY only) ...      <- first line of the block
...
# -----------------------------------------------------     <- last line of the block
```

To deploy a guard change to the FACTORY (Mini), splice — keep the live wrapper's head (shebang +
exports, before the guard) and tail (the `exec ... python -m runtime.cli` line), replace ONLY the
block between the markers:

```bash
(
# 1. extract the new guard from SETUP.md on the MacBook (mktemp — a predictable /tmp name on a
#    shared box is a symlink-clobber target):
set -euo pipefail
# the trap lives in the ( ) subshell, so it cleans up on abort too and never replaces a trap
# the pasting shell already set
GUARD=$(mktemp); trap 'rm -f -- "$GUARD"' EXIT
awk '/^# --- runtime-tree freshness guard/{f=1} f{print} f&&/^# -----/{exit}' \
  SETUP.md > "$GUARD"
[ -s "$GUARD" ] && grep -q '^# --- runtime-tree freshness guard' "$GUARD" \
  && grep -q '^# -----' "$GUARD" \
  || { echo "Guard markers missing in SETUP.md" >&2; exit 1; }

# 2. stream to the Mini over ssh stdin (no shared /tmp handoff at all) + splice there:
#    assembly + parse-check + atomic-swap in a mktemp stage on the same filesystem as the target.
ssh mini '
  set -euo pipefail
  stage=$(mktemp -d ~/.local/bin/mxr.XXXXXXXX)
  trap "rm -rf -- \"$stage\"" EXIT
  cp ~/.local/bin/mxr "$stage/mxr.snap"
  cat > "$stage/guard"
  [ -s "$stage/guard" ] && grep -q "^# --- runtime-tree freshness guard" "$stage/guard" \
    && grep -q "^# -----" "$stage/guard" \
    || { echo "Guard markers missing" >&2; exit 1; }
  N1=$(awk "/^# --- runtime-tree freshness guard/{print NR; exit}" "$stage/mxr.snap")
  [[ "$N1" =~ ^[1-9][0-9]*$ ]] || { echo "start marker not found" >&2; exit 1; }
  N2=$(awk -v n="$N1" "NR>n && /^# -----/{print NR; exit}" "$stage/mxr.snap")
  [[ "$N2" =~ ^[1-9][0-9]*$ ]] || { echo "end marker not found" >&2; exit 1; }
  head -n $((N1-1)) "$stage/mxr.snap" > "$stage/mxr.new"
  cat "$stage/guard" >> "$stage/mxr.new"
  tail -n +$((N2+1)) "$stage/mxr.snap" >> "$stage/mxr.new"
  bash -n "$stage/mxr.new"
  cp "$stage/mxr.snap" ~/.local/bin/mxr.bak.$(date +%Y%m%d%H%M%S)   # backup outside /tmp AND outside the auto-removed stage
  chmod +x "$stage/mxr.new"
  mv -f "$stage/mxr.new" ~/.local/bin/mxr
' < "$GUARD"
)
```

**Verify it PASSES on clean main WITHOUT dispatching a job** (extract the guard from the now-live
wrapper, add a PASSTHROUGH tail, point it at the real tree — `PASSTHROUGH` = guard fell through to
dispatch; `REFUSING` = it would block):

```bash
ssh mini '
set -euo pipefail
g=$(mktemp); run=$(mktemp)
trap "rm -f -- \"$g\" \"$run\"" EXIT
awk "/# --- runtime-tree freshness guard/{f=1} f{print} f&&/^# -----/{exit}" ~/.local/bin/mxr > "$g"
[ -s "$g" ] || { echo "GUARD NOT FOUND — markers missing in live mxr"; exit 1; }
{ printf "#!/bin/bash\nexport PYTHONPATH=/Users/factory/.myndaix/deploy/myndaix-runtime/src\n"; cat "$g"; printf "echo PASSTHROUGH\n"; } > "$run"
MYNDAIX_HOME=$HOME/.myndaix bash "$run" kilabz'    # factory + clean main -> PASSTHROUGH
```

The guard's LOGIC is covered by `substrate/test.sh` (extracted from `SETUP.md`, fixture repos: clean
passes, off-main/ahead/behind/dirty/unreadable-role refused), so a merge is safe — this splice just
ships the reviewed text to the live per-machine wrapper.

## Phone surface (`mxr-phone`) — RETIRED 2026-09-27

Retired unused (Jefe): the Mini's forced-command key (`iphone-2026-09`) was removed from
`~/.ssh/authorized_keys`, `orchestrator/phone/` was deleted, and `deploy-sync.sh` no longer
guards `~/.myndaix/bin/mxr-phone`. To revive it, start from git history (the commit that
deleted `orchestrator/phone/`) and `docs/phone-tailnet-surface-design.md`.

## Why this exists

On 2026-06-24 a deploy took dispatch down: `serve` was restarted onto code that read
`job.context` **before** the migration adding that column had been applied, so every
job dispatch errored against the stale schema.

The root cause was a manual ordering rule — *migrate first, then restart* — that is easy
to get backwards. `serve.migrate()` removes the decision: migrations run automatically,
in order, before the worker pool starts. A broken migration is **fail-closed** — `serve`
raises and never comes up, rather than serving a half-migrated DB.

## Fresh database (one time)

```bash
createdb runtime
psql runtime < src/runtime/ledger/schema.sql     # full DDL for a new DB
# first `serve` boot then runs migrations/ idempotently (all no-ops on a fresh DB)
```

## Adding a migration

1. Drop a file in `src/runtime/ledger/migrations/` named `NNNN_description.sql`
   (zero-padded, monotonic — they run in sorted filename order).
2. It **MUST be idempotent** — `serve` re-runs every migration on every boot. Use
   `ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT
   EXISTS`, etc. A non-idempotent migration will crash the second boot.
3. Also update `schema.sql` so a fresh DB gets the column directly (schema.sql is plain
   `CREATE`, not re-run; migrations are the path for existing DBs).
4. Add/extend a test in `tests/test_postgres_ledger.py`
   (see `test_zz_migrate_heals_stale_schema`).

## Manual fallback

If you ever need to apply a migration by hand (e.g. migrating a DB without deploying
new code):

```bash
psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0001_add_job_context.sql
```

Because migrations are idempotent, running one by hand and then letting `serve`
re-run it on boot is harmless.
