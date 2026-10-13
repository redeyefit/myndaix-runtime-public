# Auto-merge (docs-only PR-gate) — Prior-Art Brief (Phase 0)

_North-star rung 4: the brain auto-merges a narrow proven-safe class. **v1 = docs-only PRs**, gated by diff-class + CI-green + review-PASS; the human authored the change, the brain only gates+merges. First removal of the human merge gate. See [[north-star-autonomous-brain]]._

**Date:** 2026-06-26 · **Author:** Mack (interactive, with Jefe) · **Status:** brief → DESIGN → adversarial workflow → Jefe plan approval → build.

## Constraints (the lens)
Local-first · solo · bash-on-Postgres spine + the live controller-loop infra · non-Claude trigger (launchd, classifier-clean) · **safety RELOCATES** when the human merge gate is removed (north-star): verify load-bearing + un-gameable, bounded blast radius, instant rollback, self-runtime gated longest.

## Capability verdicts

### 1. PR detection + state (open / mergeable / CI status) — **ADOPT `gh`**
`gh pr list --json` / `gh pr view <n> --json files,mergeable,statusCheckRollup,headRefOid`. Already authenticated on this box (HOME-based token → works under launchd, same as ssh — verified pattern). No API client to build.

### 2. Docs-only diff-class gate — **BUILD thin (the crux; must be un-gameable)**
Every changed path (add/modify/delete, incl. both sides of a rename) must be a pure-document file (`*.md` v1; `.rst`/`.txt` candidates). ANY other path (`src/`, `tests/`, `*.py`, `*.sh`, manifests, lockfiles, `.github/`, images, a `.md` *renamed from* code) → **reject → human**. Empty diff → reject. This is the load-bearing verifier; it is the only net-new security-critical code.

### 3. CI-green gate — **ADOPT `gh`** (statusCheckRollup == SUCCESS for the required `test` check).

### 4. Review gate (PASS) — **REUSE play-review** (the controller's pipeline) on the PR range `base...head`. Reuses cross-family review + lobster triage; a NEEDS-FIX or a non-PASS → don't merge, surface.

### 5. The merge action — **ADOPT `gh pr merge <n> --merge`** (a revertible merge commit; never `--admin` bypass of a *failing* gate, never force).

### 6. Bounding · lock · durable flag · schedule — **BORROW from the controller-loop**
launchd (sibling agent), `fcntl.flock` single-instance, per-tick + daily caps, a durable `$ORCH/AUTOMERGE_ENABLED` flag (OFF by default, mirrors `AUTOFIX_ENABLED`), DRY-RUN seam. Near-zero net-new infra.

### 7. Rollback — **BORROW git** — every auto-merge is an ordinary revertible commit; flag-off halts instantly.

## Summary
| Capability | Verdict |
|---|---|
| PR detect/state | **ADOPT** gh |
| Docs-only diff gate | **BUILD** thin (un-gameable allowlist) |
| CI-green | **ADOPT** gh |
| Review PASS | **REUSE** play-review |
| Merge | **ADOPT** gh pr merge |
| Lock/caps/flag/schedule | **BORROW** controller-loop |
| Rollback | **BORROW** git revert + flag |

## Prior art on "a bot merges PRs when conditions are met"
GitHub **native auto-merge** (merge when checks pass), **Dependabot auto-merge**, **Mergify / Kodiak** (merge-queue bots). Established pattern. We **BORROW** the pattern but BUILD the *decision* (the brain picks which PRs qualify by diff-class + review), because: native auto-merge only waits for checks (no diff-class/review gate, needs per-PR human toggle + branch-protection); a hosted merge-queue (Mergify/Kodiak SaaS) is bloat for solo and off our spine / non-Claude-trigger model. **REJECT** the SaaS bots; **optionally** let `gh`/native do the final merge mechanic under our gate.

## Deliberately NOT building
Code/test/manifest/lockfile auto-merge (later, evidence-earned) · self-fix auto-merge (rung-after-next) · evidence/learning-based promotion (the learning rung we skipped — v1 uses a HARD-CODED narrow class instead) · a merge queue · branch-protection automation · any code auto-merge to the runtime itself.

## Key inputs to DESIGN
1. The **diff-class gate is the whole ballgame** — it must be impossible for a non-doc change to slip through (rename tricks, symlinks, `.md` under code dirs treated as code, submodule bumps). Strict allowlist by extension on BOTH rename sides; reject anything uncertain.
2. **Off by default** (`AUTOMERGE_ENABLED`), DRY-RUN first, per-day cap, single-instance.
3. **Docs-only is exempt from the self-runtime rule** (docs can't brick the brain) — so auto-merging docs PRs *on the runtime* is allowed; CODE auto-merge (future) excludes the runtime.
4. Without the learning rung, promotion is hard-coded to this one class — **log every auto-merge loudly**; widening waits for the outcomes ledger.
