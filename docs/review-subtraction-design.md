# Review subtraction — cut the scaffolding, keep the gate (design, 2026-09-23)

**Status:** SUPERSEDED 2026-09-24 by `review-on-github-design.md [not in the public snapshot]` (this draft's review: NOT PASS,
2 blockers). Items 2 and 3 carry over there. Kept for the evidence and the review trail.
Direction approved by Jefe 2026-09-23 ("yes do it").
**Supersedes** the review-dedup design (chain-certified backstop skip via GitHub commit statuses +
lazy canary). That design drew 5 blocking findings in its own review (xreview design 2026-09-23):
3 were soundness holes in the certification with no attacker needed. Adding a mechanism to trim
mechanisms is the failure pattern; this design REMOVES instead.

## Evidence (ledgers + inbox, 7 days to 2026-09-23)

- The load-bearing part: **kilabz on PR branches**: 481 findings confirmed/fixed, 3 labeled fp.
- **Post-merge `main` backstop:** 33 reviews/week (runtime 20, FV 13) of code already
  push-reviewed on its PR branch, plus the autofix chains they spawn. Both repos: PR-required
  main, `enforce_admins: true`.
- **Push-mode lobster synthesis cannot filter by construction:** its prompt keeps the UNION and
  closes an issue only on the raiser's retraction (`play-review.sh:938`). Ledger: kept 17 of 18
  oracle findings later labeled fp; never emitted PLAY_PASS for a run with oracle findings.
  Cost: ~150 Sonnet synthesis calls + ~160 lobster canaries per week (Claude budget).
- **Bugs in the checks themselves:** `play-review.sh:703` builds the review input with
  `git diff … 2>/dev/null || true` (a partial diff from a failing git is reviewed as complete);
  `xreview.sh` rejects git worktrees at two sites (`:85` `_repo_path`, `:98`).

## Changes (each one deletes or narrows; nothing new to trust)

**1. Backstop off for verified PR-only repos** (`controller.py`, `repos.json` data).
A repos.json entry may set `"backstop": false`. It is honored ONLY if `_branch_protection_ok()`
(`controller.py:793` — existing, fail-closed: PR reviews required + enforce_admins + no
force-push, on the WATCHED ref) returns True this tick. Honored → skip the review decide/dispatch
and `skip_to(head)` with a loud log line `backstop OFF (PR-only main verified): advanced past
<range> NOT reviewed`; the advance pass and skills indexing are untouched. Any gh/protection
failure, or the key absent → today's behavior (review). Consequences stated plainly:
- autofix goes dormant (it fires only on `main` reviews since #179; those came from the backstop);
- automerge unaffected (gate mode runs its own review before merging);
- a PR whose push-review ABORTED or never ran (Dependabot, web edit, un-hooked clone) is no longer
  caught after merge. Replacement is a merge RULE, not a mechanism: a PR merges only with a
  delivered verdict on its head; Mack checks it when relaying a merge.

**2. No lobster synthesis in push mode** (`play-review.sh`).
- Push-mode canary list: `(kilabz)` only. Gate mode unchanged: `(kilabz lobster oracle)`.
- Push mode skips the stage-2 lobster call. Delivered body = kilabz reply under
  `## KilaBz (gate)`, then oracle reply under `## Oracle (second opinion — UNVERIFIED, reviews
  code blind)`, or a one-line "oracle skipped/unavailable".
- Headline/branch: NEEDS-FIX iff either reply carries a `finding:` line, OR outcome tagging was
  not requested this run (`$ORCH/OUTCOMES_ENABLED` absent → can't tell → NEEDS-FIX, fail toward
  attention). PASS only when tagging was requested and neither reviewer emitted a tag. Known
  residual: a reviewer that raises a finding without tagging it yields a PASS headline; the body
  still carries the full reviews (Mack reads bodies, not headlines).
- `fixlist.txt` (push mode) = the kilabz reply. Oracle-only findings no longer feed autofix
  (dormant anyway per item 1); they still reach Jefe via the relay.
- Gate mode keeps synthesis + exact `PLAY_PASS` until kilabz has a structured verdict (#117).

**3. Fail closed on a failing diff** (`play-review.sh:703`). Capture `git diff`'s exit status;
nonzero → `diff_fail` naming the rc, stderr to `$run/diff.err`. Empty output keeps its message.

**4. `repo_id` = the repo, not the worktree dir** (`play-review.sh:258`, `:311`).
`cd=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir)`; if `basename "$cd"` is
exactly `.git` → `repo_id = basename(dirname cd)`; ANY other layout (bare, submodule, failure) →
today's `basename "$repo"`. Changes only `agent-<id>` worktree pushes (→ real repo name); every
normal clone and the named `~/code/worktrees/*/myndaix-runtime` worktrees resolve identically.
Old `agent-*` markers/cursors are abandoned (throwaway dirs; no state worth migrating).
`xreview.sh`: accept a worktree at BOTH `:85` and `:98` via `git -C "$rp" rev-parse --git-dir`.

## Deliberately NOT built / not touched

- No commit-status signal, no coverage chain, no lazy canary, no new table, no Command-API change.
- kilabz canary stays; kilabz stays gpt-6-astra xhigh; push review stays incremental.
- `deploy-sync` worktree rejection stays in backlog.

## Doc rule (FV CLAUDE.md, plan slice 5)

Don't manually re-dispatch a CODE review for a pushed branch — read the push verdict.

## Next (separate PR): make the checks honest

`orchestrator/test.sh` has 9 failures on main and is not in CI → fix or delete those cases, then
wire it in. Sweep `! grep -q … && ok || bad` assertions (pass on grep errors).

## Test plan (`orchestrator/test.sh` cases + controller unit test)

Backstop: key absent → reviews; `false` + protection ok → skip_to + log, no dispatch; `false` +
gh failure/weak protection → reviews. Push mode: no lobster call/canary (mxr stub records calls);
body carries both sections; tag present → NEEDS-FIX; no tags + OUTCOMES on → PASS; OUTCOMES off
→ NEEDS-FIX. Gate mode byte-identical behavior (existing gate cases stay green). Diff: failing
git with partial stdout → abort diff. repo_id: worktree → main clone name; bare/odd layout → old
basename. xreview: worktree path accepted.
