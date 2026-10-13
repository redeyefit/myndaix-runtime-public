# Autonomy-Widening Ladder — DESIGN ADDENDUM (v0.1)

_An addendum to `automerge-design.md`, NOT a new system. Defines HOW the auto-merge gate's class
of eligible PRs widens over time — by recorded evidence, never by ambition. Input: optimal-team
brief §5. **DESIGN ONLY this week** (Fable): build is gated on the outcomes-ledger landing first
(the ladder's promotion evidence lives in the same append-only-outcomes discipline). Sibling
design: `outcomes-ledger-design.md` — the two SHARE the revert-detection primitive, deliberately
built once here where demotion needs it. Status: pre cross-family review._

## 1. Why an addendum, not a build

Today the auto-merge gate merges exactly ONE class: docs-only PRs (`classify_diff` proves every
changed file is inert `.md`). That's rung L1. The question this addendum answers is the one that
will keep recurring: "can it now also merge X?" The failure mode to prevent is answering that by
GUT ("X feels safe") instead of by EVIDENCE ("X has run in shadow N times with zero
disagreement and zero reverts"). This is the Kayenta/Flagger canary-promotion pattern applied to
merge autonomy: every new class earns promotion by recorded outcomes, and a single revert demotes
it instantly.

**Non-negotiable invariant (L0 forever):** the runtime NEVER auto-merges its OWN runtime code —
the brain does not modify the code that runs the brain without a human. This is a hard denylist,
not a rung; no amount of accrued evidence promotes self-runtime code past human merge.

## 2. The rungs (each its own POSITIVELY-DEFINED allowlist, never a denylist)

| Rung | Class | Verify that makes it safe | Status |
|---|---|---|---|
| L0 | anything (human merges) | human judgment | backstop, always available |
| L1 | docs-only, narrow (`.md` only) | `classify_diff` proves inert prose; the whole content IS the diff | **CURRENT** |
| L1b | docs incl. design/spec/`.txt` | same mechanical class, lift the narrow denylist | next candidate |
| L2 | mechanical code | verify is UN-GAMEABLE BY CONSTRUCTION (see below) | shadow-only until evidence |
| L3 | broader semantic code | CI-green + cross-family PASS + human-fallback | far; do not design yet |

**L2 is the interesting one and promotes BEFORE any semantic class** precisely because its
verify can't be gamed: each L2 sub-class is its own tiny allowlist where "correct" is
machine-checkable without judgement — lockfile bumps that pass CI, formatter/import-sort
no-ops (re-run the formatter, assert zero diff), generated-file regen (re-run the generator,
assert identical), single-line constant/version bumps. Each sub-class is added individually with
its own check; "mechanical code" is never one blanket rung.

## 3. Mechanics — SHADOW → ARMED → WIDE, 1-strike demote

Every new class starts in **SHADOW**: the gate runs the full decision (classify + CI + review)
and RECORDS what it WOULD do, but does NOT merge. This reuses the auto-capture observe-only
pattern — un-fakeable promotion evidence, free, and it exercises the real code path before it can
act. Promotion is a DETERMINISTIC count over the outcomes ledger (`automerge_outcome`, §4), no
statistics infrastructure:

```
SHADOW  → ARMED : shadow_agreed ≥ 10  AND  shadow_disagreed = 0
                  (or ≥ 20 with cross-family PASS for an LLM-gated class)
ARMED   → WIDE  : merged ≥ 10 within the class  AND  0 reverts in a trailing 30 days
ANY     → SHADOW: the FIRST revert of an auto-merged PR in the class (1-strike) + alert Jefe
```

- "agreed" = the shadow decision matched what the human actually did with that PR (merged what
  it would have merged / declined what it would have declined). "disagreed" = the human did the
  opposite. A disagreement in shadow is free — that's the whole point of shadow.
- Demotion is **instant and automatic** on the first revert; it never waits for a threshold. A
  class can only climb by evidence and falls on the first mistake — asymmetric on purpose.
- **Freeze widening entirely** if the trailing auto-action revert-rate exceeds ~5% (DORA
  change-fail proxy) — a global circuit breaker across all classes, not just per-class.

## 4. The missing primitive both this and the outcomes ledger need: REVERT DETECTION

Neither the ladder nor the outcomes ledger can self-correct without automatically detecting that
a merged auto-action was later reverted. Built ONCE, here (the outcomes ledger reserves the
`reverted` enum value and consumes this):

```
per controller tick, over new commits on main since the last scan:
  a commit is a REVERT of a known auto-action iff:
    (a) `git log --format=%B` contains "This reverts commit <sha>"  AND that <sha> is a recorded
        auto-merge/auto-fix head, OR
    (b) the commit's parent-diff exactly inverts a recorded auto-action's merge diff
        (belt-and-braces for hand-rolled reverts without the standard message)
  → write an `automerge_outcome` row {class, pr, head_sha, outcome:'reverted', within_days}
  → trigger the 1-strike demotion of that class
```

Commit-message text is attacker-writable, so (a) alone is not trusted for anything but a
*conservative* signal (a false "revert" only DEMOTES — fail-safe direction); (b) is the
corroborating structural check. A revert detection can only ever tighten autonomy, never widen
it, so the trust requirement is asymmetric and low-risk.

## 5. Schema (rides the outcomes-ledger migration or its own `0009`)

```sql
-- append-only; mirrors the outcomes-ledger discipline. One row per auto-action lifecycle event.
CREATE TABLE IF NOT EXISTS automerge_outcome (
    seq          bigserial,
    id           uuid PRIMARY KEY,
    repo_id      text NOT NULL,
    pr_number    int  NOT NULL,
    head_sha     text NOT NULL,
    action_class text NOT NULL,   -- 'docs-narrow' | 'docs-wide' | 'lockfile' | 'formatter' | ...
    mode         text NOT NULL CHECK (mode IN ('shadow','armed','wide')),
    outcome      text NOT NULL CHECK (outcome IN
                  ('would_merge','would_decline','merged','declined',
                   'human_agreed','human_disagreed','reverted')),
    decided_at   timestamptz NOT NULL DEFAULT now()
);
-- promotion/demotion are plain SQL COUNTs over this table, run in the controller tick.
-- NO threshold acts until a class has been promoted by a HUMAN-approved config flip (§6).
```

## 6. Human gates that never automate (the boundary)

- **Adding a new class to SHADOW** = a code/config change Jefe merges (a new allowlist entry).
  The machine never invents a class to observe.
- **SHADOW → ARMED** = the evidence bar is deterministic, but FLIPPING a class from shadow to
  armed is a human action (`touch $ORCH/ARMED_<class>` or a repos.json field) — the machine
  presents "class X has met the bar (10/0), promote?" in the weekly report; Jefe flips. The
  count is necessary, not sufficient. (Mirrors the "human flips the arm" boundary the classifier
  already enforces elsewhere.)
- **ARMED → WIDE** raises per-tick/per-day caps for the class; also a human flip.
- **Only DEMOTION is fully automatic** (evidence can pull autonomy DOWN without a human, never
  push it UP). This asymmetry is the safety property.

## 7. Build order (AFTER the outcomes ledger lands; each its own PR, cross-family reviewed)

1. `automerge_outcome` table + shadow-mode recording in `automerge.py` (record would-decisions;
   merge nothing new) — collect shadow evidence for L1b/L2 with zero risk.
2. revert detection in the controller tick (shared primitive; wire the outcomes ledger's
   `reverted` writer to the same code).
3. the deterministic promotion-evidence SQL views + the weekly report line ("class X: 10/0,
   eligible to arm").
4. L1b as the first real promotion (docs-wide) — smallest lift, exercises the whole ladder.
5. the ~30-line promote/demote evaluator + the global revert-rate freeze.
L2 sub-classes are added one at a time, each starting at shadow, only after L1b proves the
machinery end-to-end.

## 8. Explicitly NOT building (reject as bloat / premature)

- A learned/statistical canary (Kayenta's Mann-Whitney machinery) — count-based gates suffice at
  this volume; statistics infra is enterprise weight for a solo repo.
- Per-class ML risk scoring / a confidence model — the allowlist + CI + cross-family review IS
  the risk model.
- Auto-promotion (machine flips shadow→armed without a human) — the count is evidence, the flip
  is Jefe's; removing the human from PROMOTION is the one thing this design refuses.
- Self-runtime code auto-merge, ever (L0-forever invariant, §1).
