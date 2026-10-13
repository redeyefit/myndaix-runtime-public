# S7 proposer — Mini arm runbook (one-time, after the PR merges)

The proposer ships OFF. Arming is deliberate, on the **Mini only** (the autonomy host — its ledger
holds the clean `ready` signal). Each step is reversible; rollback is always `rm $ORCH/PROPOSER_ENABLED`.

## 0. Prereqs (already true when the PR merges)
- The branch is on `main` and the Mini reconcile has CONVERGED past the merge commit
  (check `~/.myndaix/state/reconcile.out` for the new sha).
- `capture_candidate` on the Mini shows the 2 `ready` classes (`toctou-race`, `fail-open`).

## 1. Credentials (the gate-b step — do this FIRST)
1. Mint the **proposer's own fine-grained PAT** (dedicated bot identity — NOT the automerge token):
   repo `redeyefit/myndaix-runtime` only; permissions: Contents (r/w), Pull requests (r/w),
   Metadata (read). Nothing else (no workflow, no admin).
2. Install it on the Mini: paste into `~/.myndaix/.proposer-token`, then `chmod 600` it.
   The tick REFUSES to run live if this file is missing or empty (identity fallback guard).
3. Rotate the shared automerge `r2` (the old exposure): mint a Mini-only `r3` with the same scopes
   as `r2`, replace `~/.myndaix/.automerge-token` on the Mini, verify an automerge tick is healthy,
   then REVOKE `r2` on GitHub and delete the MacBook's `~/.myndaix/.automerge-token.decommissioned`.
   After this, each autonomous git-writer holds its own single-host credential.
4. Know the push identity: the proposer's `git push` does NOT authenticate with `GH_TOKEN` — git
   uses the machine's ambient credential (the `gh` credential helper for https, or the SSH agent).
   `GH_TOKEN` covers only `gh` API calls (PR create/list). On the current single-account setup
   (everything resolves to the same GitHub account) this is identity-equivalent, but the PAT's
   least-privilege scoping does not cover the push path. After ANY credential change on the Mini,
   verify with `gh auth status` there. (Verified today: auto PRs #145/#146 were opened by the
   expected account.)

## 2. Launchd job — NOTHING TO DO (substrate-managed)
`substrate/plists/ai.myndaix.proposer.json` ships the job; the Mini's reconcile installs + loads it
in its own launchd context on the first converge after merge (hourly at :45 — offset from
controller :00 / automerge :30; `launchctl load` over SSH fails on macOS, which is why this is
reconcile's job, not a hand step). Verify after converge: `launchctl list | grep proposer`.
(`orchestrator/ai.myndaix.proposer.plist.example` remains for a hand-managed/lab install only.)

## 2b. Bootstrap quiesce list — the ONE hand deploy step (static installed copy)
`substrate/bootstrap-fetch.sh` changes do NOT reach the Mini by pull alone — the machine runs a
STATIC installed copy at `$MYNDAIX_HOME/bin/bootstrap-fetch`, refreshed only by an explicit
human-approved `substrate/reconcile.sh --update-bootstrap`. Any change touching `QUIESCE_LABELS`
(e.g. adding a new launchd label like `ai.myndaix.proposer`) must be followed by
`--update-bootstrap` on the Mini, verified by comparing hashes against the repo file:
```bash
shasum -a 256 "$MYNDAIX_HOME/bin/bootstrap-fetch" substrate/bootstrap-fetch.sh   # must match
```
(Verified today: the installed copy currently matches origin/main — this documents the standing
procedure, not a pending action.)

## 3. Green dry-run BEFORE arming (the ≤15s check)
DRY_RUN deliberately bypasses the `PROPOSER_ENABLED` flag (a dry tick is proven side-effect-free),
so this diagnostic runs while the scheduled live job stays fully disarmed:
```bash
cd ~/code/active/myndaix-runtime
MYNDAIX_PROPOSER_DRY_RUN=1 MYNDAIX_DSN=postgresql://127.0.0.1/runtime \
  PYTHONPATH=src .venv/bin/python -m runtime.proposer tick
```
Expect: `would propose toctou-race → skill/auto/toctou-race for redeyefit/myndaix-runtime` (+
`fail-open`), and NOTHING opened. Any error → stop, do not arm.

## 4. Arm
```bash
touch ~/.myndaix/orchestrator/PROPOSER_ENABLED
```
The next :45 tick opens the first `--draft` PR. Disarm any time: `rm` the flag.

## 5. The human loop (what Jefe does per proposal)
- A draft PR `skill(auto): <tag> — recurring review finding` appears. Read the provenance commits
  in the body/diff, REPLACE the stub body with the real lesson, DELETE the `MDX-AUTOPROPOSED-STUB-
  UNAUTHORED` line, mark ready, merge. The controller indexes it next tick (an unedited stub is
  refused at index — merging without authoring cannot pollute reviews, it just no-ops with an alert).
- Close = declined (backoff applies). Un-acted for 14 days = auto-closed as stale.
