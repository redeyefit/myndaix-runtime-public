# PLAN.md — Phase 2 + per-repo concurrency (implementation plan, for approval)

**From:** `docs/phase2-concurrency-design.md` v0.6 (Phase 0 complete, design-approved by 3 families).
**Status:** PLAN — awaiting approval. No code until approved (`/feature`: Plan → approval → Build → Review → Fix).

## Sequencing principle
Smallest isolated PRs; spine changes are highest-risk so each is independently shippable **and revertible**. Low-risk hardening first; the cap is gated by a stress harness; pool→8 only after the cap is proven; the fix stage is last. Every PR: build → in-session Claude review + you fire codex/oracle → test → **commit-before-review** → merge. Spine PRs additionally run the stress/regression suite.

## PR-0a — `repo_id`/`base_ref` plumbing (prereq P1) · spine, off `main`
- **Files:** `cli.py` (mxr `--repo`/`--base-ref` → `submit_job`), `api.py` (`POST /jobs` accepts them), `play-review.sh` (stamp the reviewed repo + exact `$tip` SHA). Columns already exist.
- **Semantics:** omitted `repo_id` → NULL → cap-EXEMPT (unbucketed, never a shared bucket).
- **Test/gate:** `repo_id`/`base_ref` round-trip unit test; NULL when omitted; full suite green. *(No behavior change yet — cap not built.)*

## PR-0b — codex env-scrub + network policy (prereq P2) · spine, off `main`
- **Files:** `runner.py` `invoke_cli` (explicit `env=` whitelisting `PATH`/`HOME` + the agent's declared `secret_ref`s only — drop the rest); best-effort seatbelt + egress restriction for workspace-actor codex; `registry.py` (codex sandbox flag where available).
- **Test/gate:** assert a codex subprocess env contains no `HF_KEY`/`PERPLEXITY_API_KEY` unless declared; a normal job still runs. **Must land before any real codex fix run.**

## PR-1a/1b/1c — hardening (low-risk, parallel-safe)
- **1a Fixed hook path** *(orchestrator branch)* — `play-review.sh` re-execs from a fixed path outside the repo (not the worktree copy). Gate: `orchestrator/test.sh` green + re-exec assertion.
- **1b Bounded reclaim (O3)** *(spine, off main)* — `postgres_store.py` `reclaim_expired` + `fail_attempt`: `MAX_ATTEMPTS` constant; `count(*) all attempts ≥ MAX_ATTEMPTS` (checked before requeue/increment) → job→`dead`+`dead_letter`. Both paths. Gate: always-failing responder dead-letters after exactly N (off-by-one: count includes the just-closed attempt); normal job unaffected.
- **1c Worktree GC (O4)** *(spine, off main)* — `workspace.py` stable shared root (config/env, not per-process `mkdtemp`), dirs named by `attempt_id`; janitor sweep removes worktrees whose attempt isn't `open` (`git worktree remove --force` + rmtree + `prune`), never younger than the lease. Gate: orphan swept, live kept, too-young kept.

## PR-2 — per-repo concurrency cap (the risky spine edit · its OWN PR, off `main`)
- **Migration:** `repo_concurrency(repo_id PK, active int default 0)`; **backfill** from current open attempts; **indexes day-1** (`attempt(status,job_id)` for the hard count, `repo_concurrency` PK, confirm queued index).
- **`postgres_store.py` (per design §4):** `lease_job` = PICK (left-join soft filter) → seed+lock `rc` → **HARD `COUNT(*)` open attempts under the lock** → claim + `rc.active=count+1`, else bounded re-pick; NULL skips. **Decrement** RETURNING-gated on the attempt open→close (in `complete_attempt`/`fail_attempt`/`reclaim_expired`). **`cancel()` → one atomic CTE** (close attempt + flip job + conditional decrement). **`reclaim_expired`** selects/aggregates/sorts `repo_id`, decrements requeue+dead. **Reconciler** (janitor, slow) UPSERT-heals `active`, monotonic-safe. **Update the lock-order comment → attempt→job→repo_concurrency.**
- **Constants:** `MAX_PER_REPO` (feature-flag: a huge value = cap disabled, instant rollback without revert); `LEASE_SECONDS`+`HEARTBEAT_SECONDS` together (~600); `pool.py` asserts `HEARTBEAT≥LEASE` and firing ≤ lease/3; `serve.py` firing cadence.
- **THE GATE — stress harness (design §4 a–j):** N>cap workers → cap never exceeded per repo · cold-repo isolation (live snapshots) · no spin when capped · **reconcile-OFF drift→0 asserted per-close** · close-race exactly-one-decrement · cancel→zero-decrement (queued/terminal/dup) · missing-row no-over-admit · **zero `40P01`** · new-repo leases · `EXPLAIN ANALYZE` index use. **Merge only when green.** Does NOT bump pool size.

## PR-3 — pool → 8 · spine, off `main`
- `serve.py --size 8` + `kickstart`. Separate PR, **only after PR-2's harness passes**. Re-run the stress harness at size 8.

## PR-4 — autonomous fix stage v1 · orchestrator branch (depends on PR-0b)
- **Chain:** review → fix (one codex attempt, isolated worktree, `base_ref`=reviewed SHA) → **verify (separate responder job from a CLEAN `base_ref` checkout)** → diff-back (`artifact_ref`); **human-gated merge** via a Command-API verb, **never auto-merge**. Trigger v1 = human; auto-on-NEEDS-FIX blocked.
- **Files:** `play-review.sh`/new `play-fix.sh` (the chain), `registry.py` (codex-fixer entry if distinct), the verify-job spec.
- **Test/gate:** known-buggy fixture repo → fix yields a passing diff (verify from clean base); a fix that breaks `PASS_TO_PASS` is rejected; test-tampering surfaces in the diff; env-scrub (P2), file-list-as-DATA-never-argv, resolve-assert-inside-worktree all enforced.

## Cross-cutting
- **Branching:** PR-0a/0b/1b/1c/2/3 = spine → each off `main`. PR-1a + PR-4 = orchestrator branch (extend `play-review.sh`).
- **Rollback:** every spine PR independently revertible; the cap is **feature-flagged** (`MAX_PER_REPO=∞`) for instant disable without revert.
- **Review:** in-session Claude per PR + you fire codex/oracle; spine PRs get the stress/regression suite; commit-before-review always.

## Definition of done (Phase 2)
3 repos review/build concurrently, capped per repo (stress-proven, drift-proof) · pool=8 with no false-reclaim/double-exec · reclaim crash-loops dead-letter · worktrees GC'd · hook re-execs a fixed path · codex fix is human-triggered, env-scrubbed, diff-back, never auto-merge, verified from a clean base.

## Suggested order to build
**0a → 0b → (1a‖1b‖1c) → 2 [stress gate] → 3 → 4.** Start at PR-0a; it unblocks everything and is the lowest-risk way to begin.
