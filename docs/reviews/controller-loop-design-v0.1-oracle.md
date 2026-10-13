# Oracle Design Review: Controller-Loop ("the brain")

**Verdict: APPROVE-WITH-FIXES**

This is an elegant, minimal, and highly defensible step toward autonomy. Using a level-triggered reconciler on a cron to backstop the reactive push-hooks is textbook K8s-style orchestration. Crucially, keeping it stateless and decoupled from the Claude decision path preserves the security boundary.

However, there are two mechanical blockers regarding git object locality and history bootstrapping, plus a logical bug in the ledger query that will cause overlapping diffs. 

Here are the findings to fix before writing code.

## Findings

### 1. [BLOCKER] Local Object Missing (G1: REMOTE-SHA LOCALITY)
**Problem:** `git ls-remote` fetches the SHA from the network, but it does **not** download the git objects. When you pipe this into `play-review.sh`, the worker eventually runs `git cat-file -e` and `git diff` against the local worktree. Because this is an autonomous loop ticking in the background, the local clone will be behind the remote, those objects won't exist locally, and `play-review.sh` will `abort diff`.
**Fix:** The brain MUST run `git fetch` before dispatching. 
- **Concrete action:** If `head != last`, run `git -c fetch.negotiationAlgorithm=skipping fetch --quiet --no-tags origin "$watch_ref"` (with a subprocess timeout). Once fetched, you safely hold the objects, and `play-review.sh` can compute the diff locally.

### 2. [MAJOR] First-Ever Repo Bootstrap (G2: EMPTY_TREE Abort)
**Problem:** For a newly watched repo with no prior review history, `last` will be unresolved (or `0000...`). The `play-review.sh` script handles `0000...` by diffing against `EMPTY_TREE`. This will attempt to review the entire history of the repository, hitting the `MAX_DIFF` limit and aborting, meaning the repo will **never** be successfully reviewed by the loop.
**Fix:** Explicit bootstrap logic. 
- **Concrete action:** If the ledger returns no history for a repo, the brain should **not** dispatch a review. Instead, it must gracefully insert a "bootstrap" record into the ledger with the current remote HEAD, treating it as the high-water mark. The next time the loop ticks and HEAD advances, it will successfully review `bootstrap_sha..new_head`. 

### 3. [MAJOR] Ledger Query Bug & Dedup Race (Pressure Test a & d)
**Problem 1 (Query logic):** The design says `last = ledger: most-recent review job ... -> its base_ref`. This is backwards. If the last job reviewed `A..B`, its `base_ref` is `A`. If you use `A` as `last` for the next tick, you will review `A..C`, re-reviewing `B` redundantly. 
- **Fix:** You must query the previous job's **`tip`** (the head it reviewed), not its base.

**Problem 2 (The Race):** The brain reads state, and simultaneously a human pushes. Both the brain and the pre-push hook spawn `play-review.sh`. Both pass the `done-<sha>` check (because neither has finished the review to write it yet). Both charge the daily cap, both run the diff, and both submit jobs.
- **Fix:** Since the brain writes no state and `play-review.sh` is zero-touch, this race window exists. To close it statelessly, ensure the downstream `submit_job` / ledger has a **database-level UNIQUE constraint** on `(repo_id, head_sha)`. If the race occurs, the second worker to hit the ledger will gracefully bounce. (If you can't guarantee a DB unique constraint, accept the race for v1: it wastes one `DAILY_CAP` unit on the rare chance the hourly cron fires precisely during a manual push).

### 4. [MAJOR] Stale Lock Wedging (Pressure Test b)
**Problem:** Using an atomic `mkdir` lock for the controller tick is correct, but if the launchd process is SIGKILL'd or crashes mid-tick, the directory remains. The loop will wedge forever, requiring human intervention.
**Fix:** Implement stale-lock reaping in the brain itself.
- **Concrete action:** Before acquiring the lock, check if the lock directory exists AND its `mtime` is older than, say, 15 minutes. If it is, log a "reaping stale lock" warning, `rmdir` it, and proceed to acquire. 

### 5. [MINOR] Security: Subprocess Execution (Pressure Test c)
**Problem:** You are passing strings derived from `repos.json` and network output (`ls-remote`) into a shell script execution.
**Fix:** 
- Strict regex validation on the `ls-remote` output: `assert re.match(r'^[0-9a-f]{40}$', head_sha)`.
- **NEVER** use `shell=True` in Python. Execute the synthetic stdin using standard array-based subprocess bindings: `subprocess.run(["./play-review.sh", "origin", remote_url], input=f"{watch_ref} {head} {watch_ref} {last}\n".encode(), cwd=repo_path)`. This guarantees no bash interpolation or injection vulnerabilities.

---

## Architectural Decision: Option A vs B

**Recommendation: (A) Synthetic-stdin is unequivocally the right choice.**

By passing `"<watch_ref> <head> <watch_ref> <last>"`, you perfectly mimic the git pre-push protocol (`<localref> <localsha> <remoteref> <remotesha>`). 
- When `play-review.sh` parses this, it sees `remotesha` (which is your `last`) and successfully computes `base="$last"` using its existing `cat-file` check. 
- It maps the arguments exactly as needed, leaving the heavily-audited `play-review.sh` pipeline **byte-unchanged**. 
- It keeps the brain as a decoupled external actor simulating a hook, which is conceptually clean and minimizes blast radius. 

Lock in Option A, apply the fixes above, and you are clear to build.
