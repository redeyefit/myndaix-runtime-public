**VERDICT: APPROVE-WITH-FIXES**

The v2 fold is extremely rigorous. The structural security gap in `classify_diff` is fully closed: the strict `100644`/`000000` allowlist on BOTH `omode` and `nmode` mathematically eliminates symlink/gitlink/executable/typechange bypasses. The state machine correctly segregates head-terminal states (recorded) from transient states (deferred), eliminating the wedge bugs. TOCTOU vectors are closed via the owned-ref pin + `_recheck` + `sha=H` atomic PUT.

However, there is one residual flaw in how CI results are parsed, which creates a brittle crash-to-defer loop.

### FINDINGS

#### 1. MAJOR: `_ci_green` brittle string interpolation crashes on `null` (Implicit Defer Loop)
**Description:** 
The jq query `"-q", '.check_runs[] | select(.name=="test") | "\\(.status) \\(.conclusion)"'` is not valid JSONL. More critically, if a check is `in_progress` or `queued`, its `.conclusion` is `null`. In `gojq` (which `gh api` uses) and standard `jq`, interpolating `null` into a string (`\(null)`) throws a fatal error: `null (null) cannot be formatted as a string`. 

This causes `gh api` to crash and exit `1`. `_ci_green` catches this as a query failure, logs `CI: check-runs query failed — transient (retry)`, and returns `None`. While this safely defers the PR (fail-closed), it masks genuine API failures, spams the logs, and relies on an unintended crash loop rather than cleanly detecting the pending state.

**Concrete Fix:**
Use jq's `tojson` filter to emit actual JSONL objects. Since `gh api -q` unwraps the outermost string quotes for string results, `tojson` produces perfectly clean, unquoted JSON strings (JSONL) that can be safely loaded line-by-line in Python, natively handling `null`.

**Update `_ci_green` to:**
```python
def _ci_green(repo: dict, head: str) -> Optional[bool]:
    r = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo['nwo']}/commits/{head}/check-runs",
         "-q", '.check_runs[] | select(.name=="test") | {status: .status, conclusion: .conclusion} | tojson'],
        cwd=str(repo["path"]), capture_output=True, text=True, env=_git_env(),
        timeout=GH_TIMEOUT, check=False)
    if r.returncode != 0:
        log("  CI: check-runs query failed — transient (retry)"); return None
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        log("  CI: no `test` check-run yet — transient (retry)"); return None
    for ln in lines:
        try:
            run = json.loads(ln)
        except json.JSONDecodeError:
            log(f"  CI: unparseable check-run JSON {ln!r} — defer"); return None
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            log(f"  CI: a `test` run is {status!r} — still running (retry)"); return None
        if conclusion != "success":
            log(f"  CI: a `test` run concluded {conclusion!r} — FAILED for this head"); return False
    return True
```

### VERIFICATION CHECKLIST
1. **SECURITY:** PASS. `classify_diff` is airtight. `st[:1]` correctly maps `R/C` to 2 paths. The `omode`/`nmode` tuples strictly enforce standard blobs and valid transitions (deleting a `100755` executable is correctly rejected). `parse_raw_z` is NUL-strict and raises `ValueError`, which is caught and correctly records a head-terminal `skipped` decision.
2. **WEDGE:** PASS. Drafts, merge queues, fetch failures, and CI pending safely return `None` (defer), avoiding permanent wedges. CI failures, `classify_diff` rejections, and `needs_fix` verdicts return tuples (record), correctly terminating the head. `_review_pass` perfectly handles `0/1/2` exits and fresh verdict assertions.
3. **MERGE:** PASS. `fetch + refs/automerge/pr/{n}` pins the state. `B=merge-base(Mpin, H)` guarantees a deterministic diff range. The `_recheck` asserts `CLEAN/BEHIND`, and `sha=H` in the PUT provides atomic closure. No TOCTOU remains.
4. **FAIL-CLOSED:** PASS. Missing config, malformed repos.json, unknown merge queues, and exceeded rate limits all fail-closed (defer or exit).
5. **RESIDUALS:** No fail-open residuals. The only flaw was the `_ci_green` jq crash, fixed above.
