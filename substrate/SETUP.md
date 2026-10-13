# Two-Machine Substrate — Operator Guide (PR-1)

The GitOps pull-reconcile substrate. `origin/main` is the desired state; one idempotent
`reconcile.sh` converges a machine to it; drift is loud. Full rationale + review history:
`docs/two-machine-system-design.md` (v0.3, twice cross-family reviewed).

> **PR-1 scope.** This ships the substrate mechanism + its trust boundary. **Deferred:**
> serve-pool plist ownership + venv relocation (PR-1b — reconcile restarts serve via
> `kickstart` on the existing hand-managed label for now); config-driven de-personalization
> of the inbox / author-allowlist / DSN defaults (PR-2). Merging PR-1 does **not** change the
> currently-running loop until you deliberately perform the topology migration below.

## The three areas (all under `$MYNDAIX_HOME`, e.g. `~/.myndaix`)

| Area | Path | Rule |
|---|---|---|
| **DEPLOY CLONE** | `$MYNDAIX_HOME/deploy/myndaix-runtime` | Pull-only. `reset --hard`-safe. launchd resolves code here. NEVER hand-edited / used for git work. |
| **WORK AREA** | `$MYNDAIX_HOME/work` (+ play-fix `/tmp` scratch) | Ephemeral clones/worktrees for autonomous verify. Network-denied sandbox — never the live ledger. |
| **STATE** | `$MYNDAIX_HOME` (`config.env`, `state/`, `orchestrator/`, `bin/`, Postgres) | ALL mutable state. Nothing mutable under a checkout. |

## `config.env` (per-machine, `chmod 600`, git-ignored)

Copy `substrate/config.env.example` → `$MYNDAIX_HOME/config.env` and fill in. It is **parsed,
never sourced** (`config_parse.py` — strict `KEY=value`, fail-closed on a bad/missing
`MACHINE_ROLE`, no shell execution). Keys: `MACHINE_ROLE` (`lab`|`factory`), `MYNDAIX_HOME`,
`MYNDAIX_DSN` (factory pins `127.0.0.1`), `MYNDAIX_WORK_DSN` (optional scratch), `OPERATOR_INBOX`,
`AUTHOR_ALLOWLIST`, `AGENT_CLI_PATH`, `POLL_INTERVAL_S`, `DEPLOY_CLONE`.

## Roles

- **`lab`** (MacBook): interactive `serve` + pre-push review hook, autonomy OFF. Runs only
  `reconcile.sh --dry-run` (a pre-push drift sanity check) — never a full converge.
- **`factory`** (Mini): `serve` + all autonomy ticks + the 15-min reconcile poll + drift-canary
  + the 15-min **liveness-canary** (`ai.myndaix.liveness`). The deploy clone is pull-only;
  converge is factory-only.

  **Two canaries, distinct jobs.** `drift-canary` watches *config-level* convergence (descriptor →
  installed → loaded) and drops `drift-alert-*.md` / `liveness-watch-alert-*.md` into
  `$OPERATOR_INBOX`. `liveness-canary` watches *runtime execution* — every declared job for this
  role is loaded, its last exit healthy, and its `.out` fresh within the descriptor's
  `liveness_max_gap_seconds` — plus flags loaded-but-undeclared `ai.myndaix.*` labels; it drops
  `liveness-alert-*.md` and keeps `state/liveness-*` (streak/latch/last-run) files. The two watch
  each other's `.out` freshness (mutual coverage). **Every watched descriptor MUST carry a
  `liveness_max_gap_seconds` field** (`substrate/test.sh` asserts it); the reconcile poll's value
  assumes the default `POLL_INTERVAL_S=900` — if you raise the poll interval, raise that gap to
  ≥ 2× it (else the armed poll reads perpetually STALE).

## Commands

```
reconcile.sh                 # converge to origin/main (factory; via Stage-0 bootstrap-fetch)
reconcile.sh --dry-run       # non-destructive drift report (any machine); exit 1 on drift
reconcile.sh --update-bootstrap   # (re)install the static $MYNDAIX_HOME/bin/bootstrap-fetch
manifest.py build|check <config.env>   # the artifact receipt / drift core
```

`bootstrap-fetch` is the **static Stage-0 fetcher** installed to `$MYNDAIX_HOME/bin/` (NOT
auto-overwritten by reconcile — a broken reconcile in origin/main can't brick the fetch). It
quiesces the mutating ticks, `reset --hard origin/main`s the deploy clone, then re-execs the
fresh `reconcile.sh` exactly once (`$RECONCILE_BOOTSTRAPPED` guard).

## FACTORY topology migration (one-time, deliberate — do LAB dry-run first)

1. `git clone <origin> $MYNDAIX_HOME/deploy/myndaix-runtime` (the pull-only deploy clone).
2. Create `$MYNDAIX_HOME/deploy/myndaix-runtime/.venv` and `pip install -e .` (in-tree for PR-1).
3. Write `$MYNDAIX_HOME/config.env` (`MACHINE_ROLE=factory`, pin DSN to `127.0.0.1`, set
   `AUTHOR_ALLOWLIST` + `OPERATOR_INBOX`, `DEPLOY_CLONE=$MYNDAIX_HOME/deploy/myndaix-runtime`).
4. Point `$MYNDAIX_HOME/orchestrator/repos.json`'s runtime entry at a **work clone** (NOT the
   deploy clone) so review/fix git work never mutates the pull-only clone.
5. From the deploy clone: `substrate/reconcile.sh --update-bootstrap`, then `substrate/reconcile.sh`.
   reconcile renders + installs the tick / drift-canary plists (injecting
   `PLAY_SELF=<deploy-clone>/orchestrator/play-review.sh` — Option A, so the `$ORCH` script
   copies are no longer referenced), restarts serve, waits for the migration head, starts the
   ticks, and writes the `state/RUNNING_SHA` + `state/manifest.json` receipt.

### Arming the unattended auto-deploy poll (`RECONCILE_ARMED` — §2.8)

The 15-min `ai.myndaix.reconcile` poll (which auto-converges FACTORY to `origin/main` unattended)
is **sentinel-gated**: reconcile installs it ONLY if `$MYNDAIX_HOME/RECONCILE_ARMED` exists. Until
then FACTORY is **manual** — run `reconcile.sh` on demand to deploy a reviewed change. Arm it with:

```
touch $MYNDAIX_HOME/RECONCILE_ARMED    # then run reconcile.sh once to install + start the poll
```

Only arm once you trust unattended deploy — reconcile has **auto-revert** (§2.8): a post-restart
health-gate failure `reset --hard`s the deploy clone back to the last-good `RUNNING_SHA`, re-runs the
restart sequence, and drops a `reconcile-revert-*.md` alert in `OPERATOR_INBOX` — so a bad merge
self-heals to known-good code instead of stranding the always-on brain. This relies on the
**additive-migration lint** (`migration_lint.py`, run by reconcile on the `prev_good..HEAD` delta):
a non-additive migration (drop/rename/retype/tighten) is REFUSED for unattended deploy — a
contraction is a deliberate, human-gated two-release change.

Rollback: the old enumerated launchd labels are unchanged names — `launchctl bootout` + re-bootstrap
the prior plists, or `git reset` the deploy clone to a known SHA and re-run reconcile.

> **Note (reconcile-poll self-plist):** reconcile runs *under* the `ai.myndaix.reconcile` label, so
> it never bootout/bootstraps its OWN plist (self-suicide). A change to `POLL_INTERVAL_S` or the
> reconcile poll's env re-renders + installs the plist to disk but does NOT take effect until one
> manual `launchctl bootout gui/$(id -u)/ai.myndaix.reconcile && launchctl bootstrap gui/$(id -u) \
> ~/Library/LaunchAgents/ai.myndaix.reconcile.plist` (or the next reboot).

## Test

`substrate/test.sh` — fixture-only smoke + security harness (config fail-closed, plist
XML-injection safety, dry-run non-destructiveness, M4 denylist, migration-head pin, sandbox
isolation, shellcheck). The launchd-bootstrap / serve-restart / live psql migration probe are
**live-verified at deploy (LAB first)** — they need real launchd + a running serve + Postgres.
