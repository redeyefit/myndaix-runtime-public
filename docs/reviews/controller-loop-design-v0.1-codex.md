**Verdict: NEEDS-REVISION**

The synthetic-stdin approach can work, but the current design is not safe/correct as written. It needs fetch/locality, bootstrap baseline policy, and a durable controller dispatch/cursor record.

**Blockers**

- **B1 - G1 confirmed: remote SHA locality breaks synthetic stdin.**  
  [docs/controller-loop-design.md:30](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:30) observes a remote SHA, but [play-review.sh:59](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:59) and [play-review.sh:239](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:239) require local commit objects. If `head` or `last` is absent locally, base computation falls through or `git diff` aborts.  
  **Fix:** controller must fetch before dispatch: exact refspec only, no tags/submodules, timeout, argv/env-scrubbed, protocol allowlist, then assert `git cat-file -e "$head^{commit}"` and, when nonzero, `"$last^{commit}"`. Fetch is safe only as a `.git` write, not “read-only”; update [docs/controller-loop-design.md:73](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:73).

- **B2 - G2 confirmed: first-ever review maps to empty-tree/whole-repo diff.**  
  [docs/controller-loop-design.md:99](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:99) asks about `last == 0000…`; [play-review.sh:59-65](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:59) falls to merge-base/empty tree, and [play-review.sh:241](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:241) will likely abort on `MAX_DIFF`.  
  **Fix:** do not dispatch with zero. Require an explicit bootstrap baseline: seed `repo_id/ref -> current head` as “baseline, not reviewed”, then review future deltas. Do not use `HEAD~N`; it is arbitrary and can miss or over-review.

- **B3 - v1 says “NO auto-fix”, but the reused pipeline can auto-fix.**  
  [docs/controller-loop-design.md:15](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:15) excludes auto-fix, but [play-review.sh:158](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:158) treats `$ORCH/AUTOFIX_ENABLED` as durable arming, and [play-review.sh:299-300](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:299) fires `autofix_fire`.  
  **Fix:** controller must fail closed if `AUTOFIX_ENABLED` exists and strip `PLAY_AUTOFIX` from env. If controller reviews must coexist with armed autofix, zero-touch is incompatible; add a scheduled-review mode or disable flag in `play-review.sh`.

- **B4 - “stateless, no ledger table” is not sufficient for correctness.**  
  [docs/controller-loop-design.md:31](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:31) treats latest job `base_ref` as last-reviewed, but [play-review.sh:77](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:77) stamps `base_ref=$tip` before final delivery. A kilabz/oracle/lobster job can exist for `head` while the play later aborts and never writes [done-$tip](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:225).  
  **Fix:** add a durable `review_dispatch` or `review_cursor` table with unique `(repo_id, ref, head_sha)` and states like `dispatching/running/delivered/blocked`. Only advance the cursor after durable delivery/done confirmation.

**Majors**

- **M1 - Double-review dedup is not “airtight” as claimed.**  
  [docs/controller-loop-design.md:58](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:58) overclaims. There is a window before any scoped `mxr` job exists; [play-review.sh:67](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:67) detaches first. The global lock at [play-review.sh:207](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:207) prevents two full reviews in one `$ORCH`, but the loser emits SKIPPED/noise, and failed first attempts will retry.  
  **Fix:** dispatch row with unique key, or soften the claim to at-least-once with possible contention notes.

- **M2 - Controller lock needs stale/crash handling.**  
  [docs/controller-loop-design.md:57](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:57) says atomic mkdir only. A crash leaves a permanent lock.  
  **Fix:** copy the stale-lock pattern from [play-review.sh:207-218](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:207): pid/start/host metadata, TTL greater than max tick runtime, trap cleanup, stale reap, exit 0 when merely contended.

- **M3 - Security validation is incomplete.**  
  [docs/controller-loop-design.md:69](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:69) covers SHA/ref, but not trusted script path, remote URL protocol, inherited env, or repo path canonicalization. [play-review.sh:53-55](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:53) can fall back to a worktree copy; [play-review.sh:140](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:140) runs `git ls-remote "$remote_url"`.  
  **Fix:** execute only fixed `$ORCH/play-review.sh` after owner/mode/symlink checks; use `subprocess` argv with `cwd`, no shell; exact `refs/heads/main`; URL scheme allowlist; scrub `GIT_*`, `BASH_ENV`, and protocol-helper env.

- **M4 - Existing bounds are overstated.**  
  [docs/controller-loop-design.md:64](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:64) cites `submit_job` bounds, but root `mxr` jobs are inserted directly at [postgres_store.py:233-238](/Users/you/code/active/myndaix-runtime/src/runtime/ledger/postgres_store.py:233); depth/children checks only apply under a parent at [postgres_store.py:216](/Users/you/code/active/myndaix-runtime/src/runtime/ledger/postgres_store.py:216). `cost_budget`/`chain_ttl` are protocol claims, not implemented here.  
  **Fix:** controller needs its own per-tick and per-day dispatch budget plus blocked-head backoff.

- **M5 - Pool-down durability claim is false.**  
  [docs/controller-loop-design.md:65](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:65) says dispatch drains later, but [play-review.sh:233-236](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:233) aborts on canary failure, while `mxr` times out at [cli.py:51-60](/Users/you/code/active/myndaix-runtime/src/runtime/cli.py:51).  
  **Fix:** document retry-next-tick behavior, or add a durable scheduled-review job before running the shell pipeline.

- **M6 - `repo_id` identity is misaligned.**  
  Design implies repo IDs from `repos.json`, but [play-review.sh:76](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:76) uses `basename "$repo"`.  
  **Fix:** for zero-touch v1, controller must use that basename and reject duplicate basenames at config load. Longer term, pass/configure logical `repo_id`.

**Minors**

- [docs/controller-loop-design.md:5](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:5) says hourly, but [line 20](/Users/you/code/active/myndaix-runtime/docs/controller-loop-design.md:20) says every 30 min. Pick one.
- Use exact ref equality for `refs/heads/main`, not a broad “allowlist pattern”.
- Parse `ls-remote --exit-code` as exactly one two-field line; reject multiple/noisy output.
