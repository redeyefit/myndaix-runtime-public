**VERDICT: NEEDS-REVISION**

The cursor advancement, state machine, and autonomous-fix lockdown logic are beautifully engineered and hold up perfectly to adversarial scrutiny. The no-supersede and blocked-head escape paths are correct. 

However, the file-system lock and git sandbox hardening introduced severe regressions. 

### FINDINGS

**1. BLOCKER: TOCTOU in lock stealing destroys mutual exclusion**
`LOCK.rename(stale)` followed by `mkdir` is fundamentally racy for >1 stealer. If Tick A and Tick B both read `st_mtime` and see a stale lock, Tick A will rename it and create a fresh lock. Tick B then wakes up, blindly renames Tick A's *fresh* lock to `stale_B`, and creates its own. Both ticks are now running concurrently. 
*Fix:* Drop the directory-rename gymnastics. Use a standard file lock with `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`. The kernel guarantees atomic acquisition and automatically releases the lock if the process crashes, completely eliminating the need for stale-reap logic.

**2. BLOCKER: Heartbeat updates the file, but `acquire_lock` checks the directory**
`heartbeat_lock()` writes to `LOCK/meta`. Modifying an existing file **does not** update the parent directory's `st_mtime` in POSIX. Because `acquire_lock()` checks `LOCK.stat().st_mtime`, a tick taking >15 minutes will have its lock incorrectly reaped by the next cron execution, despite actively heartbeating.
*Fix:* Change `acquire_lock` to check `(LOCK / "meta").stat().st_mtime`, or have the heartbeat run `os.utime(LOCK)`.

**3. MAJOR: Extreme git hardening breaks legitimate auth (answers your question C)**
By setting `GIT_CONFIG_GLOBAL=/dev/null` and overriding `-c credential.helper=`, you break users who rely on global `insteadOf` rewrites (e.g., routing `https://github.com/` to `git@github.com:`) or global credential helpers (like `osxkeychain`). Without these, fetches for private repos will silently fail due to `GIT_TERMINAL_PROMPT=0`.
*Fix:* Remove `GIT_CONFIG_GLOBAL=/dev/null` and `-c credential.helper=`. Your inclusion of `-c protocol.ext.allow=never` is already the correct and sufficient mitigation against RCE via malicious repo-local `ext::` URLs or rewrites. 

**4. MINOR: 1-hour stall on synchronous trigger failure (answers your question D)**
If `trigger_review` fails synchronously (e.g., `play-review.sh` timeout, missing script, OS error), `claim_dispatch` is never rolled back. The database row remains stuck in `state='dispatching'` until `updated_at` exceeds `PENDING_STALE` (1 hour). For that hour, no new dispatches can occur for the repo.
*Fix:* If `trigger_review` returns `False`, explicitly un-claim the dispatch in the database (or mark it failed) so the next tick can immediately retry.

### ANSWERS TO SPECIFIC ATTACKS
* **(a) Stall forever?** No. Your logic works perfectly. While an old head is in flight, new commits wait. Once the old head delivers (or hits `PENDING_STALE`), the cursor advances and cleanly claims the *new* head. No starvation.
* **(b) Advance on the WRONG job?** If a branch is force-pushed to a previously reviewed SHA, `review_delivered` will match an old `done` job and instantly advance. This is a **feature, not a bug**—it correctly avoids re-reviewing a known-safe commit. 
* **(c), (d), (e)** answered in the findings above.
