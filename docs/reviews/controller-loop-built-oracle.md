**VERDICT: NEEDS-REVISION**

This is an exceptionally well-architected reconciler. The PostgreSQL CAS logic, environment scrubbing, array-form subprocess execution, and pre-push hook simulation are all highly robust. However, there is a critical concurrency flaw in the lock-reap mechanism, a state-machine logic contradiction that triggers overlapping dispatches, and a subtle Git GC wedge.

Here is the pressure-test breakdown:
1. **State Machine:** Single-winner CAS is perfect. Ceiling/blocked logic works exactly as intended. However, the `IS DISTINCT FROM` clause intentionally bypasses the "none is in flight" requirement, causing overlapping dispatches.
2. **Synthetic Stdin:** Pre-push semantics are simulated flawlessly. Force-pushes naturally generate the correct diff (`base..head`).
3. **Security:** URL transport allowlist prevents `ext::`/`--upload-pack` RCEs. Env scrubbing thoroughly protects against `PLAY_AUTOFIX` or SSH/Askpass injection. Subprocesses are completely array-bound. 
4. **Locality (B1):** Gaps exist. `git fetch origin ref` only updates `FETCH_HEAD`. If a force-push orphans `base`, a routine `git gc` will prune it, perma-wedging the brain. 
5. **Budgets / Locks:** Daily budgets are solid, but the `rmtree` -> `mkdir` lock-reap is racy and allows budget bypasses.

---

### 🚨 BLOCKER FINDINGS

#### 1. Lock Stealing Race Condition (Concurrency / Budget Bypass)
**The Bug:** In `acquire_lock()`, reaping a stale lock uses `shutil.rmtree(LOCK)` followed by `LOCK.mkdir()`. If `launchd` triggers multiple overlapping ticks (or if manually invoked), two processes can simultaneously evaluate `age > LOCK_TTL` and both call `rmtree`. Process B will delete Process A's newly minted lock, allowing both to call `mkdir()` successfully. Both ticks will run concurrently, bypassing the daily budget and spamming the orchestrator.
**Concrete Fix:** Use atomic directory renaming to steal the lock.
```python
        log(f"reaping stale lock ({int(age)}s > {LOCK_TTL}s)")
        stale_lock = LOCK.with_suffix(f".stale.{os.getpid()}")
        try:
            LOCK.rename(stale_lock)
        except OSError:
            log("lost the reap race — exiting"); return False
        shutil.rmtree(stale_lock, ignore_errors=True)
        try:
            LOCK.mkdir()
        except FileExistsError:
            return False
```

#### 2. Overlapping Reviews / Delta Skip Contradiction
**The Bug:** The controller's requirement states it triggers a review *"if HEAD advanced... and none is in flight"*. However, the `claim_dispatch` query explicitly bypasses this via `(pending_sha IS DISTINCT FROM $3)`. If commit A is in flight, and commit B arrives, the controller immediately overwrites `pending_sha` with B. 
This abandons the state tracking for A (its `done-A` file will be permanently ignored) and launches a *second* concurrent `play-review.sh` for `base..B`. This wastes system resources on overlapping diffs.
**Concrete Fix:** Enforce the "none is in flight" rule in the SQL gate. Reject new claims if a non-stale review is running, ensuring we queue properly. Update the `claim_dispatch` WHERE clause:
```sql
                    WHERE repo_id = $1 AND ref = $2
                      AND reviewed_sha <> $3
                      AND NOT (state = 'blocked' AND pending_sha = $3)
                      -- FIX: Must be NULL, a dead dispatch (stale), or escaping a blocked state
                      AND (pending_sha IS NULL OR state = 'blocked' OR updated_at < $4)
```

---

### ⚠️ MAJOR FINDINGS

#### 3. Cursor Perma-Wedge on Pruned Objects (B1 Gap)
**The Bug:** `process_repo` fetches the watched ref into `FETCH_HEAD`, which is an ephemeral reference. It does not update local tracking branches. If a force-push occurs (orphaning the old tip `base`), and the repo sits idle long enough for `git gc` to run, `base` will be pruned from the object database. 
Once pruned, the safety check `cat-file -e base` will fail *forever*. The controller will silently log "head/base objects not present locally — skip" on every tick, wedging the repo permanently with no auto-recovery.
**Concrete Fix:** Protect the delivered cursor from garbage collection. In `advance_cursor` (or immediately after `_done` validation in `process_repo`), update a dedicated local ref so `git gc` knows `reviewed_sha` is reachable.
```python
        if await led.advance_cursor(rid, ref, cur["pending_sha"]):
            # FIX: Pin the cursor locally so git gc never prunes the base
            _git(repo.path, "update-ref", f"refs/myndaix/cursor", cur["pending_sha"])
            log(f"{rid}: cursor advanced to {cur['pending_sha'][:8]} (review delivered)")
```

---

### 🛠 MINOR FINDINGS

#### 4. Missing Synchronous Failure Check on Trigger
**The Bug:** `trigger_review` calls `subprocess.run(..., check=False)` and returns `True` indiscriminately. If `play-review.sh` fails synchronously (e.g., syntax error, bad interpreter) and returns non-zero, the controller still charges the local `budget[0]` and writes to the daily `_charge_day()` file.
**Concrete Fix:** Check the return code. If it fails to detach/execute, don't charge the budget (the database `attempts` backoff will still correctly handle the failure).
```python
    try:
        proc = subprocess.run(...)
        if proc.returncode != 0:
            log(f"{repo.repo_id}: play-review trigger failed (exit {proc.returncode})")
            return False
```

#### 5. Daily Budget File Uses Local Time
**The Bug:** `_day_file()` formats the log file suffix using `_dt.datetime.now().strftime('%Y%m%d')`. Depending on system TZ configurations, cron/launchd often executes in UTC while the Python process inherits a local timezone, leading to inconsistent midnight rollovers for the budget.
**Concrete Fix:** Explicitly use UTC to ensure reliable budget resets.
```python
def _day_file() -> Path:
    return STATE / f"controller-day-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d')}"
```
