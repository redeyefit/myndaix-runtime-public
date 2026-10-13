# Shadow Dial — DESIGN (v0.6 — CONVERGED, post-fence measurement surface)

_v0.6 folds the r3 gauntlet's one residual MAJOR (three-way pair handling: legal-excluded dismissed_wontfix ≠ invalid off-set pair — a residual of the r2 fail-safe). r3 CONFIRMED the bound-direction invariant correct across all three gates + the measure-only boundary intact. v0.5 folded the r2 gauntlet (eval-gate Wilson direction B1-again → the bound-direction invariant is now stated once and applied everywhere; snapshot captures eval params; unexpected-pair fail-safe). v0.4 folded the r1 gauntlet on v0.3 (2 BLOCKER + 5 MAJOR + 1 MINOR): the Wilson gate
direction was inverted (B1), the eval/arming gate is now fully specified (B2/M2), the label
include/exclude set is enumerated (M4), the snapshot schema is made reproducible (M5), and the
small-n example is corrected (M1). The MEASURE-ONLY thesis was not challenged._

**Builds directly on `docs/outcomes-dials-design.md` v0.2** (branch `feat/outcomes-precision-dials`;
dual-family + 42-agent adversarial review, mechanism ENDORSED, 2 BLOCKERs + 5 MAJORs folded). This
is that design's **PR-A SHADOW**, re-scoped for two things that changed since 2026-07-03:
1. **The fence shipped** (migration 0010, PR #81): the gating metric is now
   `finding_precision_promoted` — **human labels only** (`confirmed_real / (confirmed_real +
   dismissed_false_positive)`), not the old all-source `applied_fixed`-based `finding_precision`.
2. **Real data exists**: 81 human labels across ~24 (tag × family) cells (2026-07-12). The
   data-gate the v0.2 doc named ("has nothing to recommend today — correct and expected") is met.

## 0. Scope — MEASURE ONLY. This rung acts on NOTHING.

The v2.0-era autonomy ladder's first rung, in its safest form: a read surface that says what a
suppress dial *would* do, and changes nothing. No prompt weighting, no note injection, no
suppression, no arm state. The ONLY write is the opt-in `--snapshot` append to the rung's own
`dial_shadow_snapshot` table, which no review/prompt/gate code ever reads (m1). The entire
deliverable is **visibility + an honest evaluation loop**. Acting (the v0.2 doc's PR-B) stays
deferred, now gated on this rung showing a *stable* signal Jefe agrees with (§4) AND the unresolved
benign-taxonomy question (below).

Why measure-only is the right first rung (the v0.2 reframe, still true): the live taxonomy is
**entirely correctness/security classes**, and under fail-closed policy essentially none is safe to
auto-suppress — a noisy security detector is still a detector. So the dial's real v1 value was
always the MEASUREMENT, not the acting. This rung ships exactly that, and nothing else.

## 1. What the fence changed (why M1 is moot, and what's newly honest)

v2.0's worst finding **M1 — "PROMOTE is broken, CUT it"** — was that `applied_fixed` is written
automatically on line-hash disappearance (refactor/churn/delete all count), so a high-churn class
inflates precision→1.0 with ZERO human labels. **The promoted metric does not read `applied_fixed`
at all** (it's `confirmed_real`/`dismissed_fp` over `finding_current_human`). So promote-by-machine-
inflation is structurally impossible now. Consequence: this shadow surface can HONESTLY show a
would-trust signal (high human-confirmed precision) alongside would-suppress — both are pure
human-label measurements. **Neither ever auto-acts here.**

M3 (accumulator trap — all-time precision anchors a class to old failures) still applies to the
eventual *acting* path; this measurement surface addresses it by showing BOTH all-time and a
recency window, so a recovering class is visible, not hidden.

## 2. The computation (per rule_tag × reviewer_family, over human labels only)

**The admitted label set (M4 — the M1-closure requirement, enumerated).** The surface reads
`finding_current_human` (0010): `SELECT DISTINCT ON (finding_key, reviewer_family) … WHERE
outcome_source IN ('human_confirm','human_dismiss') ORDER BY seq DESC` — so per (finding, family)
exactly ONE current human row, latest-by-seq (a correction supersedes). Then:
- **INCLUDE** `(human_confirm, confirmed_real)` → the numerator, and `(human_dismiss,
  dismissed_false_positive)` → the fp denominator term.
- **EXCLUDE** `applied_fixed` / `auto_fix_landed` (machine, not in `finding_current_human` at all —
  this is the structural reason v0.2's M1 is moot), `dismissed_wontfix` (real-but-declined, not a
  precision signal), superseded rows (the DISTINCT ON keeps only the latest human row), and ANY
  future non-human source (they never enter `finding_current_human`). A builder must NOT widen the
  admitted set without a design change.
- **Pair handling is THREE-WAY (MAJOR-r3 — legal-excluded ≠ invalid):** a current human row is
  classified by its exact `(source, outcome)` pair:
  1. **Included precision pair** — `(human_confirm, confirmed_real)` / `(human_dismiss,
     dismissed_false_positive)` → counted in `n` + provenance.
  2. **Legal excluded pair** — `(human_dismiss, dismissed_wontfix)` → excluded from `n` +
     provenance, and **NOT** `invalid` (it's a valid human label, just not a precision signal).
  3. **Impossible / off-set pair** — anything else (the fence pair-CHECK makes it unreachable) →
     excluded AND counted into an `invalid` tally surfaced in the verb (a nonzero `invalid` = a
     visible fence-integrity alarm). Never affects a classification count.
  Tests assert: a `dismissed_wontfix` row leaves `n`, provenance, AND `invalid` all unchanged; a
  forged off-set row increments ONLY `invalid`.

Per (tag, family) over that set:
- `n` = confirmed_real + dismissed_false_positive (the labeled denominator; `dismissed_wontfix`
  is excluded — "real but declining" is not a precision signal, matching the promoted view).
- `precision` = confirmed_real / n (point estimate; NULL if n = 0).
- **Wilson score interval** (z = 1.96, 95%) `[lo, hi]` on `precision` — the statistical-stability
  fold. **THE BOUND-DIRECTION INVARIANT (stated once, applied everywhere a CI gates a decision —
  §2 classifier AND §4 eval):** *confident the true rate is BELOW a threshold T* ⟺ **`hi < T`**;
  *confident the true rate is ABOVE T* ⟺ **`lo ≥ T`**. Never the midpoint, never the wrong end.
  Applied here: would-suppress = precision confidently BELOW floor = **`hi < FLOOR`**; would-trust =
  precision confidently ABOVE ceiling = **`lo ≥ CEILING`**.
- `n_recent` / `wilson_recent` = the same interval over the last `SHADOW_RECENCY_N` (default 30)
  labeled events by `seq` (M3: a recovering class shows a rising recent precision even while all-time
  lags — and the eval/arming gate in §4 requires BOTH windows to clear, not just all-time).
- `provenance` = distinct source refs (`finding_outcome.ref`) AND distinct source plays
  (`source_event` `review:<play>`) among the labeled findings. NOTE (M3, post-fence): every human
  label is authored by the single trusted operator (`principal_role='admin'`), so author-diversity
  is N/A here — a REDUCTION in attack surface vs v0.2's all-source metric (an attacker cannot forge
  the operator's labels at all). The meaningful anti-poisoning signal is that the labels span
  independent reviews/branches, not one bulk-dismissal of one decoy PR's findings — hence distinct
  refs AND distinct plays, so 8 labels from 1 review read as thin, not as 8 independent signals.

Classification (INFORMATIONAL — no action attached):
| would-say | condition |
|---|---|
| `insufficient` | n < `SHADOW_MIN_N` (default 10) OR distinct-refs < `SHADOW_MIN_REFS` (2) OR distinct-plays < `SHADOW_MIN_PLAYS` (2) |
| `would-suppress` | Wilson **upper** bound `hi` < `SHADOW_FLOOR` (0.30) AND dismissed_fp ≥ `SHADOW_MIN_FP` (3) AND not `insufficient` |
| `would-trust` | Wilson **lower** bound `lo` ≥ `SHADOW_CEILING` (0.90) AND not `insufficient` |
| `hold` | otherwise |

**Fail-closed suppressibility is retained and code-owned** (the v0.2 B1 fold): a `would-suppress`
carries a `suppressible` boolean from a code-defined `SUPPRESSIBLE` set — **empty in v1** (no
current tag is safe to auto-suppress). So even the *label* would-suppress is annotated "not
suppressible (no benign tier)". This keeps the eventual acting rung honest from day one: the
measurement can say "low precision here," but the design refuses to imply any of it is
auto-suppressible yet.

## 3. Surface (one read verb, zero writes)

`mxr dial-shadow` — prints, per (tag × family): n, precision, Wilson [lo, hi], recent precision,
distinct-refs, the would-say, and the suppressible flag. Rides the morning brain-check next to
`mxr outcome-stats`. OPERATOR tier: fail-CLOSED (exit 2) on an unreachable ledger (a dial that
can't read must not print an empty "nothing to suppress"). Thresholds are read from env in the
VERB (per v0.2 D1); the SQL is a pure projection.

Example (n=8 is BELOW MIN_N=10 → `insufficient`, not `hold` — M1):
```
rule_tag                  family  n   prec  wilson[lo,hi]  recent  refs  would-say     suppressible
missing-scoping           oracle  3   0.00  [0.00,0.56]    0.00    2     insufficient  no (n<10)
unsanitized-injection     oracle  8   0.50  [0.22,0.78]    0.55    3     insufficient  no (n<10)
silent-error-suppression  kilabz  5   1.00  [0.57,1.00]    1.00    2     insufficient  no (n<10)
```
**Expected today: `insufficient` for essentially EVERY cell.** At current volumes (~3–8 human
labels/cell) nothing clears MIN_N=10 — the surface correctly refuses to classify, and that refusal
IS the honest v1 output. It becomes informative only as labels accrue over weeks.

## 4. The EVALUATE loop (the point of shadowing)

A shadow prediction is only worth arming if it's STABLE and the human AGREES. Two mechanisms:
- **Snapshot** (`mxr dial-shadow --snapshot`): append the current classification to
  `dial_shadow_snapshot` (a NEW additive table — schema below). Weekly cron or manual. This is the
  rung's ONLY write, to its OWN table, which no review/prompt/gate code ever reads.
- **Agreement report** (`mxr dial-shadow --eval`): for each past snapshot's `would-suppress`
  classes, compute the human labels that landed on that (tag, family) SINCE the snapshot's
  `data_cutoff_seq`, and report whether the new labels confirm low precision (fp) or contradict it
  (real).

_Two build-time resolutions (PR-A code gate, r1 — kilabz):_ (1) **snapshot consistency** — the
verb reads `data_cutoff_seq` FIRST and then reads labels **bounded to `seq <= cutoff`**, so a
snapshot's cells are exactly its cutoff's labels; a label racing in mid-snapshot is cleanly
post-cutoff (a future eval cohort), never lost from both sides of the boundary. (2) **the eval
cohort anchors at the LATEST would-suppress snapshot's cutoff** (fresh-evidence-only, `max` not
`min`): a label that landed between snapshots already fed later snapshots' classifications (the
stability sub-gate), so re-admitting it as agreement evidence would count one label at two
sub-gates — the strict fail-closed reading of "post-cutoff" in sub-gate 3.

**The arming gate — every sub-gate numeric (B2 fold), NONE of which the eval may skip.** A
(tag × family) `would-suppress` is "arming-eligible" ONLY if ALL hold:
1. **Stability:** the SAME `would-suppress` appears in ≥ `SHADOW_STABLE_SNAPS` (4) snapshots spanning
   ≥ 4 distinct ISO weeks (not 4 same-day snaps).
2. **Both windows:** in the LATEST snapshot, BOTH all-time `hi < FLOOR` AND recent-window `hi <
   FLOOR` (M2 — a class recovering in its recent window is NOT eligible even if all-time still lags).
3. **Subsequent cohort size:** the post-cutoff human labels number ≥ `SHADOW_EVAL_MIN_N` (10), from
   ≥ `SHADOW_MIN_REFS` distinct refs AND ≥ `SHADOW_MIN_PLAYS` distinct plays (the §2 provenance/
   min-FP guards apply to the eval cohort, not just the classifier).
4. **Agreement with a CI, not a point rate:** the agreement rate = fraction of subsequent labels
   that are fp (confirming the would-suppress). We arm only when confident this is HIGH, so by the
   bound-direction invariant the Wilson **LOWER** bound of the agreement rate must clear ≥
   `SHADOW_EVAL_AGREE` (0.70) — a bare 7/10 (lo ≈ 0.40) does NOT pass.
5. **Empty-denominator = not eligible:** zero subsequent labels → `insufficient`, never a pass.

Pre-committed (written here so it can't be rationalized away later): **PR-B (acting) is not even
DESIGNED for a class until that class is arming-eligible by ALL of 1–5.** Shadow is the instrument
that earns its own arming — or refuses it cheaply.

`dial_shadow_snapshot` schema (M5 — self-contained so any future eval reproduces from the snapshot
alone): `captured_at`, `data_cutoff_seq` (max seq of finding_outcome at capture — the "labels
since" boundary), `rule_tag`, `reviewer_family`, `confirmed_real`, `dismissed_fp`, `n`, `precision`,
`wilson_lo`, `wilson_hi`, `n_recent`, `wilson_recent_lo`, `wilson_recent_hi`, `distinct_refs`,
`distinct_plays`, `would_say`, `suppressible`, and the FULL policy context — classifier params
`floor`, `ceiling`, `min_n`, `min_refs`, `min_plays`, `min_fp`, `recency_n`, `z`; **eval params
`stable_snaps`, `eval_min_n`, `eval_agree`, `week_span_rule` (M5-r2 — so an eval verdict is
reproducible from the snapshot alone, not a live config)**; and versions `suppressible_set_version`,
`taxonomy_version`.

## 5. What this rung deliberately does NOT do (each with its un-gating condition)

| Not built | Why | Un-gates when |
|---|---|---|
| Any acting (note injection, suppression) | the v0.2 PR-B; needs stable signal + a benign taxonomy tier | §4 gate met AND a suppressible class exists |
| A non-empty `SUPPRESSIBLE` set | the taxonomy is all correctness/security; nothing is safe to mute | a benign/"style-nit" tier is added (its own design) |
| Promote acting | endorsed as measurable now (fence mooted M1) but acting on trust still couples to autonomy — out of scope | its own rung |
| A miss/false-negative signal | nothing records a bug the review MISSED (v0.2 D3) | a separate rung entirely |
| A dashboard / web UI | terminal read verb is the point | never (anti-over-engineering) |

## 6. Security & failure modes
- **Read-only except the opt-in `--snapshot` append to `dial_shadow_snapshot`** (never read by
  review/prompt/gate code): no fence table is written, no acting path exists, so the suppress-lever
  attack surface from v0.2 §6 is absent by construction. There is nothing to weaponize — the worst
  an attacker who could forge labels achieves is a wrong number in a report a human reads (and they
  cannot forge the operator's human labels in the first place).
- `rule_tag`/`reviewer_family` are validated against the taxonomy allowlist before display (no
  path component, no prompt — they only ever reach stdout here).
- Wilson math + all thresholds are pure/deterministic; env knobs parsed fail-safe (default on
  malformed, per the CLI env-knob convention).
- Gate mode: N/A (this is a manual operator read, never on the review/merge path).

## 7. Test plan (test-first, deterministic)
- Wilson bound math: known (k, n) → known [lo, hi] within tolerance; n=0 → NULL, no divide.
- **B1 direction (the load-bearing test):** a middling sample (k=5, n=10, p=0.5, interval
  ≈[0.24,0.76]) must be `hold`, NOT would-suppress (proves the gate reads `hi<FLOOR`, not `lo`); a
  genuinely-low sample (k=0, n=12, hi≈0.24<0.30) → would-suppress; k=n high → `lo≥CEILING` →
  would-trust.
- classification edges: n<MIN_N → insufficient; distinct-refs<MIN_REFS → insufficient (even at high
  n); distinct-plays<MIN_PLAYS → insufficient; dismissed_fp<MIN_FP → not would-suppress; suppressible
  flag always "no" while SUPPRESSIBLE is empty; `dismissed_wontfix` + `applied_fixed` rows never
  enter n (the admitted-set enumeration).
- recency: old fp's + recent confirms → all-time hi below vs recent hi above (arming-ineligible by §4.2).
- provenance: 8 labels from 1 ref/1 play → insufficient; from ≥2 refs AND ≥2 plays → eligible.
- snapshot: append-only, ALL schema columns populated (incl. data_cutoff_seq + policy context);
  reproducible (recomputing from the snapshot's own columns yields its would_say).
- **eval gate (B2, each sub-gate):** stability needs 4 snaps across 4 distinct weeks (4 same-day →
  fail); both-windows required; subsequent cohort < EVAL_MIN_N → insufficient; agreement uses the
  Wilson **LOWER** bound ≥ EVAL_AGREE (a 7/10 cohort, lo≈0.40, does NOT pass); zero subsequent
  labels → insufficient, never a pass.
- **three-way pair handling (MAJOR-r3):** a dismissed_wontfix row leaves n + provenance + invalid
  ALL unchanged (legal-excluded, not an alarm); a forged off-set row increments ONLY invalid.
- verb: fail-closed exit 2 on unreachable ledger; empty ledger → honest "insufficient everywhere".

## 8. Build plan
- **PR-A (this):** the Wilson/classification core (pure, unit-tested) + `label-shadow` read over
  `finding_current_human` + `mxr dial-shadow [--snapshot|--eval]` + one additive migration
  (`dial_shadow_snapshot` table; guarded, idempotent). Zero acting, zero fence-table writes.
- **PR-B (deferred, v0.2's ACT):** designed only after §4's evidence gate + the taxonomy question.

Deploy = normal serve-restart auto-migrate. Arms nothing; lets Jefe WATCH the signal accrue and
prove (or refute) itself — the evidence the acting rung needs to be designed honestly, or dropped.
