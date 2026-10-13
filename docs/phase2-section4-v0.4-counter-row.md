# §4 v0.4 — per-repo concurrency via a counter row (SPEC FOR RE-REVIEW)

This replaces the v0.3 §4 "`pg_try_advisory_xact_lock` + re-count CAS in the lease CTE"
with Oracle's proposed **`repo_concurrency` counter row joined in the lease path**. It is a
brand-new spec; nobody has reviewed an *actual* spec of it (Oracle only proposed the idea).

Validate this against the **real** `postgres_store.py` / `schema.sql` included in the dispatch,
not against the prose. Confirm the four points in §4.6.

---

## 4.1 Schema addition

```sql
CREATE TABLE repo_concurrency (
    repo_id    text PRIMARY KEY,
    open_count int  NOT NULL DEFAULT 0 CHECK (open_count >= 0)   -- # of OPEN attempts for this repo
);
```

- One row per repo. Created lazily: `submit_job` does `INSERT INTO repo_concurrency(repo_id)
  VALUES ($repo_id) ON CONFLICT DO NOTHING` whenever it inserts a job with non-NULL `repo_id`.
- `open_count` is the count of **open attempts** whose `job.repo_id = this repo`. It is a
  denormalized cache of `SELECT count(*) FROM attempt a JOIN job j ON j.id=a.job_id
  WHERE a.status='open' AND j.repo_id = R`. The CHECK is the underflow tripwire for drift.
- Cap = `MAX_PER_REPO` (policy constant, e.g. 4). `repo_id IS NULL` ⇒ **cap-exempt** (legacy).

## 4.2 Canonical lock order (the deadlock-critical part)

The store today pins ONE order: **attempt (A) → job (J)** (see the ABBA note in the module
docstring; `cancel()` was fixed to attempt-first). The counter row (R) is a third lockable.
**New canonical order: A → J → R** (R is acquired LAST, after any attempt/job lock).

- Every **decrement** path (`complete_attempt`, `fail_attempt`, `reclaim_expired`, `cancel`)
  already locks A then J; it then locks R last → in order.
- **`lease_job`** never locks a pre-existing attempt; it locks **J then R** (a sub-order of
  A→J→R), then *inserts* a new attempt (no lock on an existing A). So no cycle with the
  decrementers. This is the property reviewers must confirm (§4.6.2).

## 4.3 `lease_job` (rewritten)

Two phases in ONE acquired connection. **Phase A handles legacy NULL-repo jobs with zero
per-repo logic; Phase B handles capped repos.**

**Phase A — NULL-repo fast path (unchanged semantics):** the *existing* lease CTE, but with
`AND j.repo_id IS NULL` added to the `WHERE`. No `repo_concurrency` touch at all. If it leases a
job, return it. (Confirmation §4.6.3: legacy jobs lease exactly as today, never blocked by any
repo's cap, never reading/writing the counter.)

**Phase B — capped repos (only if Phase A returned nothing), in one transaction:**

1. **Pick a candidate from an UNDER-CAP repo** (does NOT lock the counter; locks only the job):
   ```sql
   SELECT j.id, j.repo_id
     FROM job j
     LEFT JOIN repo_concurrency rc ON rc.repo_id = j.repo_id
    WHERE j.status = 'queued'
      AND j.repo_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM attempt a WHERE a.job_id = j.id AND a.status='open')
      AND COALESCE(rc.open_count, 0) < $cap          -- committed value filters out capped repos
    ORDER BY j.priority DESC, j.created_at, j.id
    FOR UPDATE OF j SKIP LOCKED                       -- lock the JOB row only; skip jobs in flight
    LIMIT 1;
   ```
   LEFT JOIN + COALESCE means a brand-new repo with no counter row yet (count 0) is leasable.
   The `< $cap` filter is what prevents the spin/starvation Oracle flagged: capped repos are
   excluded from candidacy by the committed counter, so a flooded hot repo is simply not picked
   — a cold repo's job is.

2. If no row → return None (nothing leasable). Else lock + recount the counter (the race backstop):
   ```sql
   INSERT INTO repo_concurrency(repo_id) VALUES ($rid) ON CONFLICT DO NOTHING;
   SELECT open_count FROM repo_concurrency WHERE repo_id = $rid FOR UPDATE;   -- per-repo serialize
   ```
   The `FOR UPDATE` here is **blocking** (not SKIP LOCKED) but only ever contends with another
   worker leasing/closing the SAME repo (sub-ms). A worker leasing repo S locks a different row
   and never waits on repo R. (Confirmation §4.6.2.)

3. **Recount check (the TOCTOU backstop):** if `open_count >= $cap` → the committed filter in
   step 1 raced; ROLL BACK / return None for this poll (the picked job stays `queued`; the worker
   re-polls). Bounded by the number of concurrent workers (≤ pool size), NOT by queue depth —
   there is no per-candidate retry loop, so no CPU spin.

4. Else commit the lease atomically:
   ```sql
   UPDATE repo_concurrency SET open_count = open_count + 1 WHERE repo_id = $rid;
   UPDATE job SET status='leased' WHERE id=$jid AND status='queued';   -- CAS guard (we hold FOR UPDATE OF j)
   INSERT INTO attempt(id, job_id, worker_id, lease_expires_at, status)
        VALUES ($aid, $jid, $worker, statement_timestamp() + $lease*interval '1 second', 'open');
   ```
   Increment is bound to the same tx that opens the attempt → **increment exactly once** (the
   `attempt_one_open_per_job` unique partial index is the structural backstop, as today).

## 4.4 Decrement — exactly once, on every close path

The decrement MUST ride on the *winning* attempt-close transition (the RETURNING-gated CAS that
flips `attempt.status` open→ok/failed), so it fires exactly once and only when a close actually
happened. For NULL-repo jobs, **no decrement** (they never incremented).

- **`complete_attempt`**: its CTE already returns the job id only on the winning open→ok + job→done
  transition. Add, in the same statement/tx, for the closed job's repo (if non-NULL):
  `UPDATE repo_concurrency SET open_count = open_count - 1 WHERE repo_id = (that repo)`. Tie it to
  the `closed`/`done` CTE output so a LostLease (zero rows) decrements nothing.
- **`fail_attempt`**: decrement once, gated on the `UPDATE attempt ... status='failed'` actually
  having closed an open attempt (it already guards on `status='open'` + LostLease).
- **`reclaim_expired`**: batched. After the `closed` CTE, decrement per repo by the number of
  attempts reclaimed for that repo: `UPDATE repo_concurrency SET open_count = open_count - cnt
  WHERE repo_id = $rid` for each repo in the batch, locking the rc rows in `repo_id` order
  (deadlock-free among concurrent reclaimers; normally a single janitor).
- **`cancel`**: decrement once iff it closed an open attempt for a non-NULL repo.

**The drift risk reviewers must probe (§4.6.1):** any close path that flips an attempt open→closed
WITHOUT a matching decrement → counter drifts HIGH → that repo permanently under-admits (starves).
A double-decrement → drifts LOW → over-admits past the cap. Because each decrement is in the SAME
tx as the (exactly-once) close CAS, steady-state drift should be zero; confirm there is no close
path that escapes this rule.

## 4.5 Self-healing reconciliation

Belt-and-suspenders for drift from bugs / manual surgery / a future verb that forgets to
decrement. Runs piggybacked on the janitor (alongside `reclaim_expired`), conservatively — it only
corrects a repo's counter while holding that repo's `rc` row `FOR UPDATE` and recomputing truth
under the same lock, so it cannot itself introduce a race:

```sql
-- for each rc row, under FOR UPDATE on that row:
UPDATE repo_concurrency rc
   SET open_count = COALESCE((SELECT count(*) FROM attempt a JOIN job j ON j.id=a.job_id
                               WHERE a.status='open' AND j.repo_id = rc.repo_id), 0)
 WHERE rc.repo_id = $rid;
```

Reviewers (§4.6.1) must confirm this actually *heals* a too-high counter (the starvation case)
back to truth, and does not fight an in-flight lease/close (it locks the same rc row those paths
lock last, so it serializes with them).

## 4.6 What the re-review MUST confirm

1. **Decrement-exactly-once across complete / fail / dead(reclaim→dead) / reclaim / cancel** — no
   close path drifts the counter; AND the reconciliation in §4.5 actually heals a missed decrement
   (too-high counter → recomputed to truth → repo un-starved).
2. **`FOR UPDATE` on the counter row composes with `SKIP LOCKED`** — per-repo serialization ONLY:
   locking repo R's counter (step 2/3) must never block leasing repo S, and must never re-introduce
   the ABBA deadlock given canonical order A→J→R.
3. **The NULL-guard path (Phase A) leases legacy jobs with zero per-repo logic** — `repo_id IS NULL`
   jobs never read/write `repo_concurrency`, never error on a NULL lock key, and are never blocked
   by any repo's cap.
4. **The stress-harness assertions are sufficient to catch a broken counter** (see §4.7).

## 4.7 Stress-harness assertions (proposed)

- (a) **Hard cap:** N≫cap workers hammering ONE repo → max simultaneous open attempts for that repo
  ≤ cap at all times, AND `open_count` never observed > cap.
- (b) **No starvation:** cold repo S (1 job) + hot repo R flooded at cap → S's job leases within a
  bounded number of polls (assert it is NOT starved by R).
- (c) **No drift across all four close paths:** storm of lease→{complete | fail | reclaim | cancel}
  → for every repo, `open_count == COUNT(open attempts)` at quiescence.
- (d) **Self-heal:** inject drift (manually set `open_count` wrong, both high and low) → after one
  reconciler pass, `open_count == COUNT(open attempts)`.
- (e) **NULL exemption:** NULL-repo jobs lease freely regardless of any repo being at cap; they
  never touch `repo_concurrency`.
- (f) **No double-lease** (existing invariant) still holds under all of the above.
- (g) **No deadlock/serialization errors:** concurrent lease + complete + fail + reclaim + cancel
  across multiple repos run clean (validates canonical order A→J→R).
