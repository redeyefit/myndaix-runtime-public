**VERDICT: APPROVE-WITH-FIXES**

The v0.4 delta is exceptionally well-engineered. The design choices cleanly and permanently resolve the previous race conditions and architectural gaps without introducing new TOCTOU bugs or stalling vectors. 

Here is the evaluation of the new mechanisms:
1. **`fcntl.flock`**: **Flawless.** The kernel guarantees mutual exclusion and automatic crash-release when the file descriptor is closed (either explicitly or upon process exit/crash). Python 3.4+ opens FDs as non-inheritable by default, and `subprocess.run` sets `close_fds=True`, so the lock does not leak to `play-review.sh` or `git`. `O_RDWR` ensures the offset starts at 0, making the `ftruncate`/`write` sequence perfectly safe.
2. **`GIT_ALLOW_PROTOCOL`**: **Perfect.** This cleanly blocks `ext::` and `fd::` transports across all spawned git commands (including `ls-remote` inside `play-review.sh`) while preserving `url.insteadOf` and credential helpers. This plugs the RCE vector without destroying authenticated local development.
3. **`PLAY_FORCE_DONE=1`**: **Correct.** Bypasses the branch-move suppression in `confirm_pushed`. The controller now reliably receives its post-delivery signal regardless of whether the branch advanced mid-review, permanently fixing the "wedged cursor" blocker. No premature advances can occur because `advance_cursor` strictly matches `pending_sha` to the delivered marker.
4. **`release_dispatch`**: **Correct & Elegant.** Forcing `updated_at = to_timestamp(0)` allows immediate re-dispatch on the next tick without waiting out `PENDING_STALE`. Because `pending_sha` is unchanged, the next `claim_dispatch` correctly increments the `attempts` counter. It reliably climbs to `MAX_ATTEMPTS` and hits the blocked ceiling without infinite retries.
5. **Pinning Heads**: **Great design.** Using a deterministic slug (`refs/myndaix/pending/<slug>`) ensures there is exactly one anchor ref per watched branch, preventing unbounded ref growth. Pinning the reviewed ref *before* advancing the ledger strictly prevents moving the cursor onto a missing object.

### FINDINGS

**MINOR: Ignored `_pin` failure on the in-flight head defeats GC safety**
Before dispatching a review, the controller attempts to anchor the pending head:
```python
    _pin(repo, _ctl_pending_ref(ref), head)
    if trigger_review(repo, head, base, url):
```
If `_pin` fails (e.g., due to a stale `.lock` file in `.git/refs/`), the failure is silently ignored and the unanchored review fires anyway. If the remote branch is force-pushed and `git gc` runs while the worker is reviewing, the unanchored commit could be pruned, causing `cat-file` to crash mid-review.

**Fix:**
Check the return value of the pending `_pin`. If it fails, safely abort the dispatch and yield the claim so it can be retried on the next tick:
```python
    if not _pin(repo, _ctl_pending_ref(ref), head):
        log(f"{rid}: could not pin pending {head[:8]} — skipping dispatch")
        await led.release_dispatch(rid, ref, head)
        return
    
    if trigger_review(repo, head, base, url):
        # ...
```
