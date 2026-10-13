Verdict: **APPROVE-WITH-FIXES**

No residual fail-open found in the prior blocker path. `classify_diff` now rejects `T`, `X`, symlink/gitlink/exec modes, unsafe old-side copy/modify modes, and denylisted rename sides. `parse_raw_z` is now strict and `evaluate_pr` records parse failures as human skips.

Findings:

- **MAJOR wedge:** [orchestrator/play-review.sh](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:266) / [orchestrator/play-review.sh](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:267) route deterministic diff failures through `abort`, and gate abort now writes `ABORTED` + exits 2 at [orchestrator/play-review.sh](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:122). `_review_pass` treats every rc=2/`ABORTED` as transient at [src/runtime/automerge.py](/Users/you/code/active/myndaix-runtime/src/runtime/automerge.py:342), and `evaluate_pr` returns `None` at [src/runtime/automerge.py](/Users/you/code/active/myndaix-runtime/src/runtime/automerge.py:438). An over-`PLAY_MAX_DIFF` docs PR is head-terminal but will retry every tick forever. Split terminal diff-cap/empty-diff aborts from transient canary/oracle/contention aborts, or pre-cap and record a human skip in `automerge.py`.

- **MINOR strictness gap:** [src/runtime/automerge.py](/Users/you/code/active/myndaix-runtime/src/runtime/automerge.py:149) whitelists by `st[:1]`, so direct core probes like status `A999` with `000000->100644 x.md` still classify as docs-only. I do not see a real `git diff --raw` path that emits that, but for the claimed strict parser/security boundary, validate status grammar exactly: `A`, `D`, `M`/`M<score>`, `R<score>`, `C<score>`.

Everything else requested checks out: transient CI/review/caps defer, terminal unsafe diffs record, `_recheck` fails closed, merge uses `sha=H` and asserts `merged: true`, rate-limit/merge-queue/counter parsing fail closed, and the fresh random verdict dir closes the stale-PASS path.

I could not run `tests/test_automerge.py` directly here because `asyncpg` is missing; I did run targeted direct probes with an import stub for the classifier/parser paths.
