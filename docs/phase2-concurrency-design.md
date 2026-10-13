# DESIGN.md — Phase 2 (autonomous fix) + parallel per-repo concurrency

**Status:** **v0.6 — §4 design-approved by all three families; Phase 0 COMPLETE.** in-session panel ×2 + codex + Oracle all confirm the counter-row pattern (lock order A→J→R no ABBA, NULL leases legacy, FOR UPDATE∘SKIP LOCKED = per-repo only). Remaining items are codified below; new code (the `cancel()` CTE) is caught again at Phase 4 implementation review + the stress harness. **Next: PLAN.**
**Author:** Steven Fernandez — designed with the AI pair-engineering team.

> **The correctness model (your COUNT-under-lock call):** the cached `repo_concurrency.active` is a **SOFT filter** (perf only — keeps workers off likely-capped repos; may be stale; self-healed by the reconciler). The **HARD cap authority** is a `COUNT(*)` of open attempts for the repo, taken **under the rc row lock** at lease time. So counter drift can **never** breach the cap — only cause transient mis-filtering, which heals. The counter is an optimization; correctness never depends on it being exact.

---

## 1. What it does & why
- **A. Parallel per-repo concurrency** — repos run concurrently, capped per repo; scale the pool. Per-repo, not per-tip.
- **B. Autonomous fix stage** — codex applies a fix in an isolated worktree, diffs back, never auto-merges; ONE bounded attempt in v1, human-triggered.

## 2. Non-negotiable principle
Additive where possible — the per-repo cap is the one true SPINE edit + one counter table: its own PR, gated by the stress harness, highest-risk change in the system.

## 3. Prerequisites
- **P1** — plumb `repo_id` (+ exact reviewed SHA as `base_ref`) through `mxr`→`submit_job`; `play-review.sh` stamps the repo. `repo_id IS NULL` ⇒ cap-EXEMPT.
- **P2** — codex fix actor: env-scrub + network-egress restriction + best-effort seatbelt; `sandbox-exec` is weak → human merge gate is the v1 backstop (Docker deferred).

## 4. Part A — per-repo concurrency (CONCRETE; soft-filter + hard-count)
**Table:** `repo_concurrency(repo_id text PRIMARY KEY, active int NOT NULL DEFAULT 0)`. Lazily seeded; backfilled by a one-time migration from current open attempts.

**Lease = PICK → LOCK+SEED → HARD-COUNT → CLAIM:**
1. **PICK** (soft filter, perf only — restores cold-repo fallback under `LIMIT 1`):
   ```sql
   SELECT j.id, j.repo_id FROM job j
   LEFT JOIN repo_concurrency rc ON rc.repo_id = j.repo_id
   WHERE j.status='queued' AND NOT EXISTS (open attempt for j)
     AND (j.repo_id IS NULL OR COALESCE(rc.active,0) < MAX_PER_REPO)
   ORDER BY priority DESC, created_at, id
   FOR UPDATE OF j SKIP LOCKED LIMIT 1
   ```
2. **If `repo_id IS NOT NULL`:** seed+lock the rc row (`INSERT … ON CONFLICT DO NOTHING`, then `SELECT active FROM repo_concurrency WHERE repo_id=$r FOR UPDATE` — B4), then the **HARD authority** under that lock:
   ```sql
   SELECT count(*) FROM attempt a JOIN job j2 ON j2.id=a.job_id
   WHERE a.status='open' AND j2.repo_id=$r          -- reality, not the cache
   ```
   If `count < MAX_PER_REPO` → **CLAIM** (`UPDATE job→leased`, `INSERT attempt`, set `rc.active = count+1` to keep the soft filter honest). If `≥` → **bounded re-PICK** (WHERE now excludes the full repo → no spin).
3. **NULL repo_id** skips step 2 and claims directly (O1).

**Lock order (B2):** canonical **attempt → job → repo_concurrency** (counter LAST). Lease locks the job (1) before the counter (2); terminal paths already hold attempt+job, touch the counter last → no ABBA. **Update the lock-order comment in `postgres_store.py`.** (Codex's proof: lease only locks `queued` rows, decrementers only `leased/running` — disjoint, can't even contend on the same job.)

**Decrement = bound to the ATTEMPT close (B3):** in the **same CTE that flips the attempt `open→{ok,failed}`**, gated on its `RETURNING job_id` — never on job-status. Covers complete(ok) / fail(failed, **requeue AND dead**) / reclaim(failed) / **cancel(failed)**. Because the cache is *soft*, a missed/late decrement can't breach the cap (the hard COUNT does) — reconcile heals it.
- **`cancel()` must become ONE atomic CTE** (close open attempt + flip job→dead + decrement-iff-a-real-open-attempt-closed). Today's multi-step `cancel()` races `lease_job` on a *queued* job: cancel's close hits 0 rows, blocks on the job lock, lease opens an attempt + increments, cancel resumes → job=dead with an **orphan open attempt forever** (reclaim skips it, reconcile can't tell it from a live one). Latent today; the counter amplifies it into starvation. The atomic CTE closes it.

**Reconciliation = slow backstop (M1), now pure soft-cache maintenance:** 30–60s janitor; `UPSERT` so it **heals missing rows** too (Codex — a missing row + live open attempts would otherwise read 0 and over-*filter*, never over-admit since the hard COUNT gates); derive `active = count(open attempts in leased/running)` per repo; counter `FOR UPDATE` during the write. It tunes fairness, not correctness.

**Constants (M4):** name `MAX_PER_REPO`. Set `LEASE_SECONDS` + `HEARTBEAT_SECONDS` (extend amount) together (~600); firing cadence (`serve.py`, today hardcoded 30) ≤ lease/3; `pool.py` asserts `HEARTBEAT_SECONDS ≥ LEASE_SECONDS` and firing ≤ lease/3. Tradeoff: bigger lease ⇒ worst-case slot-hold-after-crash ~lease before reclaim frees the slot — mitigated by tight reclaim cadence.

**Scheduling note:** PICK considers NULL-repo (legacy-exempt) jobs alongside non-NULL via the same ORDER BY — but because they're cap-exempt they're never filtered out, so under contention they're effectively favored. Conscious "legacy exempt" choice; revisit if legacy volume grows.

**Also:** indexes day-1 (queued; `attempt(status,job_id)` for the hard count; `repo_concurrency` PK), git index-lock backoff, per-repo not per-tip, **per-lane CUT** from v1.

**STRESS HARNESS = the gate. Assert ALL:** (a) cap never exceeded per repo under N>cap hammer (now drift-proof via the hard COUNT); (b) cold repo S throughput within X% of baseline while hot R floods — **live snapshots**, not just eventual drain; (c) no worker spins when all eligible repos capped; (d) **reconciler DISABLED → `active` reconverges to EXACTLY 0 at quiescence, asserted immediately after EACH close** (not just at the end); then a reconcile-ON run heals injected drift; (e) **same-attempt close races** (complete vs fail vs reclaim vs cancel) → exactly one decrement; (f) **queued / terminal / duplicate cancel → zero decrement**; (g) **missing-counter-row** case never over-admits; (h) **zero `40P01`** (deadlock) over the run; (i) new-repo first job leases; (j) `EXPLAIN ANALYZE` index use.

## 5. Part B — fix stage (v1 = ONE bounded attempt)
- v1 = single codex attempt → verify → diff-back. CUT from v1: sample-N, architect/editor, Aider A/B, LISTEN/NOTIFY.
- Bounded chain over `parent_id`/`base_ref`/`artifact_ref`; exact reviewed SHA as `base_ref`. N/retry-depth = bash-controller constants vs `MAX_CHILDREN`/`MAX_DEPTH`.
- Inherited-free invariants only while every mutating/verifying stage is `WORKSPACE_ACTOR` (pin it): no auto-retry of a mutation, no auto-merge.
- **Verify = a SEPARATE responder job from a CLEAN `base_ref` checkout** — never the fix worktree's harness (runners/package-scripts/mocks/lockfiles/snapshots/CI all fake green).
- Trigger v1 = human-gated. auto-on-NEEDS-FIX blocked until clean-checkout verify + Docker. Full loop deferred.
- Fence the fixer: static objective; fix-list + localize file-list as nonce-fenced DATA never argv; resolve+assert agent writes inside the worktree.

## 6. Three spine fixes (build FIRST — low-risk hardening)
- **Bounded reclaim (O3, M2):** `MAX_ATTEMPTS` constant; `count(*) all attempts ≥ MAX_ATTEMPTS` checked **before the active increment**, at **both** `reclaim_expired` and `fail_attempt` → job→`dead`+`dead_letter`. Rationale (corrected): `reclaim` *does* set `failed`, so failed-only *would* trip — but count-all is the simpler poison ceiling that also catches the in-flight Nth retry + cancels. **`reclaim_expired` must `SELECT j.repo_id`** (it doesn't today), aggregate per repo, decrement requeue AND dead outcomes, **`ORDER BY repo_id`** (deadlock-safe batch).
- **Worktree GC (O4, M3):** graceful path already covered (`worker.py:116` finally) — GC's NEW value is the hard-crash orphan. Needs a **STABLE shared worktree root** (config/env, not per-process `mkdtemp`), dirs named by `attempt_id`, sweep = remove any whose attempt isn't `open` (`git worktree remove --force` + rmtree + `prune`), never rm a dir younger than the lease.
- **Fixed hook path:** re-exec from a fixed path outside the repo. Own ~10-line PR.

## 7. Security surface
codex containment (P2: env-scrub + net policy + seatbelt; human merge gate is the v1 backstop). Verify from a clean `base_ref` checkout. Validate the diff (symlink/traversal/perm); resolve+assert agent writes inside the worktree; worktree reset between jobs. Static objective; nonce-fenced DATA; file-list never argv; never auto-trigger merge/shell; `lobster` = dedup/judge gate.

## 8. Scope guard
**v1:** P1 · P2 · per-repo cap (soft filter + hard COUNT, counter-last, attempt-close decrement, atomic cancel CTE, upsert+backfill reconcile) · bounded reclaim (count-all + repo_id, both paths) · worktree GC (stable root) · fixed hook · lease/heartbeat constants · indexes day-1 · git backoff · fix stage = ONE attempt + clean-checkout verify + diff-back · pool→8 (after cap passes stress).
**Deferred:** Docker · per-lane · sample-N · architect/editor · LISTEN/NOTIFY · auto-on-NEEDS-FIX · full loop · Aider-swap.
**Out of scope:** queue/agent engine, DAG DSL, RAG/vector, hosted SaaS reviewer.

## 9. Build order
0. **Prereqs:** P1 plumbing · P2 env-scrub/net.
1. **Hardening:** fixed hook (own PR) · bounded reclaim (count-all + repo_id, both paths) · worktree GC (stable root).
2. **Per-repo cap** — its OWN PR: the §4 PICK→LOCK→HARD-COUNT→CLAIM algorithm + counter-last order + attempt-close decrement + atomic `cancel()` CTE + upsert/backfill reconcile + day-1 indexes + lease constants, at **current pool size**, gated by the **full stress harness (a–j)** — the empirical proof.
3. **Pool → 8** — separately, only after the cap passes the harness.
4. **Fix stage** — one attempt + clean-checkout verify + diff-back, env-scrubbed codex, vs a known-buggy fixture repo.

## How it was reviewed
v0.1 → in-session panel (3 blockers) → codex (5) → Oracle (4) → **in-session §4 pass (4 blockers + 4 majors vs the real CTE)** → **codex + Oracle re-review of the concrete §4 (design-approved; 5 codified changes + the COUNT-under-lock strengthening)** → v0.6. Three families, complementary findings, all converged on the counter-row as correct. **§4 = Phase 0 complete; the `cancel()` CTE and the rest are caught at Phase 4 implementation review + the stress harness.** Research: BUILD on spine, adopt nothing.

## Changelog
**v0.6** — COUNT-under-lock = hard cap authority, cached `active` = soft filter (drift can't breach the cap); `cancel()` → one atomic CTE (fixes the queued-cancel orphan-attempt); decrement RETURNING-gated on attempt-close; `reclaim_expired` selects/aggregates `repo_id`, sorted; reconciler UPSERTs missing rows + one-time backfill migration; stress harness adds per-close drift, close-race exactly-one, cancel→zero, missing-row, live-starvation snapshots, zero-`40P01`; Phase-A scheduling note. **§4 design-approved → Phase 0 complete.**
**v0.5** — §4 as concrete pick→lock→recheck SQL; counter pinned last; decrement bound to attempt-close; upsert-seed; monotonic reconcile; O3/O4/lease corrected.
**v0.4** — Oracle: counter-row, NULL-guard, reclaim count-all, worktree GC, lease bump.
**v0.3** — codex: try-lock + CAS, exclude expired, v1 fix → one attempt, clean-checkout verify.
**v0.2** — advisory lock (was TOCTOU); P1/P2 prereqs; per-lane cut. **v0.1** — initial research-backed design.
