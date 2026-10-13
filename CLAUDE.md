# myndaix-runtime — the team runtime behind `mxr`

A deterministic, Postgres-backed spine for multi-agent work: one **Command API** is the sole
ledger writer; agents lease, run, and reply through DB row locks (`FOR UPDATE SKIP LOCKED`),
never files. Two pieces run against one Postgres: the long-lived **`serve` worker pool** and the
thin **`mxr` CLI**. The orchestrator (`orchestrator/`) is the autonomous review/fix loop on top.

**Doc map (read before touching the area):**
- `DESIGN.md` — the spec + decision log (contracts C0–C5, Command-API verbs). v0.4, build-ready.
- `DEPLOY.md` — deploy surfaces + migration rules. **Read before ANY deploy.**
- `docs/OPERATING.md` — day-to-day ops reference (serve, mxr, launchd).
- `SETUP.md` — fresh install (deps, Postgres, agent CLIs, one/two machines).
- `substrate/SETUP.md` — two-machine GitOps substrate (lab=MacBook, factory=Mini, `reconcile.sh`).

## Invariants (violating these is a design bug, not a style choice)

1. **The Command API is the SOLE ledger writer.** Transports, workers, controllers, interfaces
   all go through its verbs. Nobody writes raw tables.
2. **Contracts deep + rigid; roster/models/transport are data.** Every change is ADDITIVE (a
   registry row in `src/runtime/registry.py`), never STRUCTURAL (a patch around the spine).
   Test for any change: *can you add an agent / swap a model / change transport without editing
   the spine?* If no — a contract has a gap; close the gap, don't patch around it.
3. **Authority — not reach — drives behavior.** `workspace-actor` jobs are NEVER auto-retried
   (a half-applied mutation isn't idempotent); responders are. Don't "fix" a stuck mutating job
   by adding a retry.
4. **Transport is a dumb pipe.** It never invokes an agent, never blocks on agent work, and
   transport semantics never leak into job/agent fields. (Both were root causes of the outage
   this system replaced.)
5. **Worktree isolation, never auto-merge.** File-mutating jobs run in ephemeral git worktrees;
   the result is a reviewable diff. Merge is a deliberate, serialized human step.
6. **Never auto-exec agent output as shell.** Controller output becomes Command-API verbs only.

## Layout

```
src/runtime/          the spine: contracts.py, registry.py (roster-as-data), command_api.py,
                      runner.py, worker.py, pool.py, workspace.py, cli.py (mxr), api.py, serve
src/runtime/ledger/   schema.sql (fresh DB) + postgres_store.py + sqlite_store.py + migrations/
orchestrator/         play-review.sh / play-fix.sh / controller-tick.sh + launchd plists — the
                      autonomous review loop. Deploys via TRUSTED INSTALLED COPIES, not the tree.
substrate/            two-machine GitOps: config.env parsing (parsed, never sourced),
                      reconcile.sh, canaries, plists
tests/                39 suites / 738 test functions, wired into CI (curator gate self-skips there)
docs/                 per-feature design docs + OPERATING.md
```

## Deploy (the half-deploy trap — bit us 2026-07-02 and 2026-06-24)

Two targets; know which one your change touches:
- **`serve`** (anything under `src/`): pull, then `launchctl kickstart -k gui/$(id -u)/ai.myndaix.runtime`.
  Migrations auto-apply on boot, fail-closed.
- **Orchestrator scripts** (`orchestrator/play-review.sh`, `play-fix.sh`): the worker runs the
  trusted installed copy at `~/.myndaix/orchestrator/`, NOT the repo tree. A script change ships
  ONLY via the `cp` (both scripts, always). Full three-surface checklist + the one-line Mini
  deploy: `DEPLOY.md`.
- **The Mini is a PULL-ONLY MIRROR with NO working tree** — everything runs from the deploy clone
  (reconcile-owned); the loop's git work runs in the machine-owned `~/.myndaix/work/<repo>`
  (`repos.json`). Never commit or branch there, never recreate a checkout under `~/code`.

## Migrations

Files in `src/runtime/ledger/migrations/` — `NNNN_description.sql`, monotonic, and **idempotent**
(`IF NOT EXISTS` everywhere; serve re-runs all of them every boot). Also update `schema.sql`
(fresh-DB path) and extend `tests/test_postgres_ledger.py`.

## Tests

```bash
PYTHONPATH=src python3 tests/test_worker.py                      # zero-dep
LEDGER_TEST_DSN=postgresql://localhost/runtime_test \
  PYTHONPATH=src python3 tests/test_postgres_ledger.py           # Postgres concurrency proofs
```

⚠️ Postgres suites **DROP and recreate the schema** — `LEDGER_TEST_DSN` must point at a
throwaway DB (`runtime_test`), NEVER the ops `runtime` database.

## Workflow

- Design-first: new capability → design doc in `docs/` → cross-family review → build. Every
  shipped slice has a regression test that fails without its fix — keep it that way.
- **Implementation attack pass (MANDATORY for lock/race/trust-boundary code):** between
  design approval and first line of code, run an adversarial pass over the PLANNED
  MECHANICS (agents attacking the lock protocol, failure ordering, crash windows, mixed
  deploy states). Evidence 2026-09-03: per-repo locks got this pass → 2 blockers caught
  pre-build, ~2 residual findings; the phone wrapper skipped it → 14 findings in r1.
  Design review catches architecture; only the attack pass catches your mechanics.
- Global rules apply: commit-before-review, `/feature` phases in order, bash rules
  (`tools/bash-check.sh` + semgrep rules exist — run them on script changes).
- Outcome labels are ground truth for future acting rungs: label from the FINDING TEXT,
  never the tag (`mxr outcome <key> real|fp|wontfix` — fp = reviewer wrong, wontfix =
  right-but-declining). Mislabeled ground truth poisons the dials.
