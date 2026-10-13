**Verdict:** `APPROVE-WITH-FIXES`

The security model here is exceptionally solid. The adversarial surface is tightly bounded: exact SHA pinning prevents TOCTOU races, the `git diff --raw -z` parser is resilient, and the atomic `sha=H` merge argument delegates the final race-condition defense to GitHub’s backend. There are absolutely no fail-OPEN paths or injection vulnerabilities.

However, the system is fundamentally plagued by **logic bugs that will permanently fail-close PRs on transient states**, completely wedging the auto-merge pipeline. 

Here is the adversarial breakdown and findings.

### 1. The Security Boundary (`classify_diff` & `parse_raw_z`)
**Status: IMPENETRABLE**
- **Non-.md / Executables / Symlinks:** Cannot bypass. The gate strictly mandates `nmode == "100644"`, outright rejecting submodules (`160000`), symlinks (`120000`), and executables (`100755`), even if they are named `.md`.
- **Rename/Copy Handling:** Flawless. `st[:1] in ("R", "C")` reliably forces `n=2` extraction, and the denylist iteration (`for p in paths`) guarantees BOTH the source and destination paths are vetted. You cannot sneak an executable through by renaming it to `.md`.
- **Parser Robustness:** Git’s `-z` output uses `\x00` delimiters, meaning spaces, newlines, and malicious characters in paths are tokenized safely. Truncated payloads elegantly trigger the `len(raw_paths) < n` break, which safely processes what it has without throwing exceptions or allowing malformed trailing data to become a bypass.

### 2. The Merge (`gh api PUT`)
**Status: ATOMIC & SAFE**
- Passing `-f sha=H` correctly binds the exact commit to the merge. If the PR’s head moves via a force-push during the `tick` execution, GitHub's API natively rejects it with a `409 Conflict`.
- `res.get("merged") is True` accurately validates the operation before charging the budget.
- The `merge_queue` pre-flight correctly evaluates before the git merge, failing closed if undetermined.

### 3. Pinned SHAs & Moving Refs
**Status: SECURE**
All operations correctly thread the immutable `H` and `B` values. There are no `refs/heads/main` lookups inside the diff logic—`merge-base` properly computes `B`, guaranteeing the diff boundary is purely between immutable objects.

### 4. Review Gate & Replay Prevention
**Status: SECURE BUT TOO STRICT**
- `vpath.unlink()` combined with `run_id` pinning (`am-{n}-{H[:12]}`) definitively prevents stale reads and replay attacks.
- `play-review.sh` safely fails-closed (`exit 1` + writes `NEEDS-FIX`) on contention or timeouts. However, `automerge.py` mishandles this (see Finding 2).

---

### Findings & Concrete Fixes

#### 🚨 BLOCKER 1: Transient states cause permanent wedge (`evaluate_pr`)
When `evaluate_pr` encounters an expected transient state, it returns `("skipped", <reason>)`. Because `automerge.py` writes *any* tuple to the `PostgresLedger` as a final decision for that SHA, **a PR evaluated while CI is running will be permanently skipped and never retried**.

**Fix:** Return `None` (which the loop correctly uses to defer without recording) instead of a tuple for transient states:
```python
    # Fix 1: Objects not local yet
    if _git(... "-e", f"{H}^{{commit}}").returncode != 0 ...
        return None # retry next tick

    # Fix 2: Pending CI
    ci = _ci_green(repo, H)
    if ci is None:
        return None # retry next tick
    if not ci:
        return ("skipped", "CI failed for this head")
        
    # Fix 3: Transient merge states (e.g. pending checks)
    st = pr.get("mergeStateStatus")
    if st in ("BLOCKED", "PENDING", "UNKNOWN"):
        return None
```

#### 🚨 BLOCKER 2: Review gate contention permanently kills the PR
`play-review.sh` exits `1` and writes `NEEDS-FIX` if it encounters a transient lock contention or if the LLM pool timeouts. Currently, `_review_pass` reads this, evaluates to `False`, and `evaluate_pr` permanently records it as a model rejection.

**Fix:** Use the script's exit code to differentiate a true LLM rejection (exit 0, verdict = NEEDS-FIX) from a transient worker abort (exit != 0).
```python
    rev = subprocess.run([str(PLAY_REVIEW), ...], check=False, ...)
    if rev.returncode != 0:
        return None # worker aborted (contention/timeout) — retry next tick
        
    # proceed to read verdict JSON...
```

#### 🔴 MAJOR: Pagination JSON parsing bug in `_ci_green`
`gh api --paginate` accompanied by a `jq` object creation (`-q '[...]'`) forces `gh` to apply the filter *independently per page*. If a PR has enough checks to paginate, it outputs multiple disconnected JSON arrays (e.g. `[{...}] \n [...]`). `json.loads()` will throw a `JSONDecodeError`, and `_ci_green` will fail-closed permanently.

**Fix:** Parse it as JSONL line-by-line. Remove the array wrapping in `jq` and correctly return `None` when pending/missing.
```python
def _ci_green(repo: dict, head: str) -> Optional[bool]:
    r = subprocess.run(["gh", "api", "--paginate", 
                        f"repos/{repo['nwo']}/commits/{head}/check-runs", 
                        "-q", '.check_runs[] | select(.name=="test") | {s:.status,c:.conclusion}'], ...)
    if r.returncode != 0:
        return None
    
    out = r.stdout.strip()
    if not out:
        return None # no test workflow found yet (pending)
        
    for line in out.splitlines():
        if not line.strip(): continue
        c = json.loads(line)
        if c.get("s") != "completed":
            return None # pending
        if c.get("c") != "success":
            return False # failed
    return True
```
