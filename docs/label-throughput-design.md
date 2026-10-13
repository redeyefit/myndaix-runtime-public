# Label-throughput — human bulk-confirm, terminal-first (PR-A of the self-labeling rollout)

**DESIGN v0.2 — 2026-07-12** (v0.1 + the 8-finding cross-family fold; reorder/D-1/D-2 verified
sound by both families). Child of `docs/self-labeling-design.md` v0.4 (branch
`design/self-labeling-system`, dual-family APPROVED): this is the buildable spec for the HUMAN
side of its §6 confirmation stage, re-scoped against the shipped code (fence = migration 0010,
PR #81; lifecycle fixes = 0011, PR #84) and the measured live state.

## 1. Why now, and why THIS first

The bottleneck is measured, not theoretical: the Mini's `finding_labelqueue` is **42 deep,
growing ~6/day, with 2 human labels total** — and the acting rungs gate on
`finding_precision_promoted`, which reads ONLY human labels. Two grounding facts flip the v0.4
rollout order (PR-2 exec-prior before PR-3 human batch):

1. **`confirm_outcome` is unreachable.** It is the ONLY writer of
   (`human_confirm`,`confirmed_real`) — the numerator of the gating precision — and it has NO
   CLI wiring (`postgres_store.py:1662` has zero callers). The keys-file offers only
   `fp|wontfix`. As shipped, promoted precision can never exceed 0/n: the flywheel cannot
   produce the very number the autonomy ladder gates on.
2. **PR-2's substrate does not exist.** On the auto path the regression check NEVER runs
   (autofix requires `fail_to_pass:null` in repos.json — play-review.sh:263-264 — and play-fix
   with a null proof finishes UNVERIFIED at play-fix.sh:318 BEFORE the verify suite). play-fix
   receives no finding keys (the fixlist is free-text triage — play-review.sh:603), and its
   verify signal is per-PATCH, not per-finding. Exec-prior today = heavy new plumbing for a
   non-gating prior with ~zero firings.

So PR-A = make the human's labels cheap and complete, on the substrate that is 100% ready.
Throughput target (inherited): **~3–5 batch commands/week, not 42 taps.**

## 2. What PR-A builds (4 small pieces, one PR)

### 2a. `real` — wire the confirm path
`mxr outcome <key12> real` joins `fp|wontfix`. Routes to the EXISTING
`confirm_outcome(prefix, "all", kind, principal_role="admin")`.

**The exact kind→row table** (all three pairs already legal under the 0010 pair-CHECK; NO new
pair, hence no schema change — this table IS the verification):

| CLI kind | outcome_source | outcome | source_event (server-minted) |
|---|---|---|---|
| `real` | `human_confirm` | `confirmed_real` | `human:<key12>:real` |
| `fp` | `human_dismiss` | `dismissed_false_positive` | `human:<key12>:fp` |
| `wontfix` | `human_dismiss` | `dismissed_wontfix` | `human:<key12>:wontfix` |

`confirm_outcome` inserts these rows DIRECTLY (the shipped verb at postgres_store.py:1662 does
its own INSERT with the mapped pair — it does NOT call the legacy `human_dismiss` API, so no
legacy open-state guard is inherited; a builder must keep it that way).

**`"all"` family semantics (unchanged from today's `mxr outcome`):** `"all"` expands to
[kilabz, oracle] and writes one row PER family that has a `finding_current` row for the key —
i.e. only families that actually raised the finding (0, 1, or 2 rows). This does not label
unseen rows: the key identifies one code-level claim; if both families raised it, the human's
verdict on the claim applies to both (precision is per-family, and both were right/wrong
together). A family that never raised it is skipped by `_finding_fields` returning None.

- **All three kinds route through `confirm_outcome`** (D-1 below): one verb, one semantics.
  `human_dismiss` stays for API/back-compat, but the CLI stops calling it.
- **Any-state semantics, enumerated.** The outcome vocabulary is CLOSED (10 values; there are
  no in-flight/pending states — nothing is "mid-action" in this ledger, every row is a
  completed fact). What a human label means per possible `finding_current` state:
  `open` → the normal case; `applied_fixed`/`reverted` → post-hoc truth on an auto-closed
  finding (exactly what the promoted metric wants); `expired` → truth recorded, queue
  membership UNCHANGED (human rows are terminal for membership; the 0011 toggle is untouched —
  no resurrection); `dismissed_*`/`confirmed_real` → a correction (higher seq wins in
  `finding_current_human`); `exec_real_prior`/`panel_*` (future) → the human verdict lands on
  top of the machine prior, which never gated anyway. No state cancels or waits on anything.
- Prefix resolution, ambiguity refusal, idempotent re-issue: inherited unchanged from the verb.

### 2b. Multi-key batch — same verb, N keys
`mxr outcome <kind> <key12> [<key12>...]` (kind-first when multiple keys; the existing
`<key12> <kind>` single form stays). Loops the single-key verb per key — no new ledger verb, no
new transaction shape.
- **Validation is PER-KEY, at that key's turn** (not whole-argv prevalidation): each token is
  checked ≥12-hex immediately before ITS resolve+write; a junk/ambiguous token refuses THAT
  key (prints candidates) and the loop continues — earlier valid keys are already written,
  later ones still proceed. §6's "validated before any DB touch" means before THAT KEY's DB
  touch.
- **Duplicates within one invocation are deduped** (first occurrence kept, order preserved)
  before the loop. Summary line reports all four counts:
  `labeled <unique keys> (<rows> rows), refused <n>, duplicates <n>` — "rows" is ledger rows
  actually inserted this call (0–2 per key via family expansion; idempotent re-issues insert 0).
  Exit 0 always (operator-retry, per the outcome verb's tier-3 discipline).
- **Explicit keys ONLY** (D-2): every labeled key was enumerated by the human (typed/pasted).
  NO `--tag`/`--play`/`--all` selector bulk in PR-A — a selector labels rows the human never
  saw, which is exactly the authority-laundering D3 forbids without shown evidence. When the
  panel (PR-C) exists to supply concurrence evidence, selector-bulk gets its own reviewed PR.
- Batch cap 200 keys/invocation (queue is 42; the cap is a fat-finger guard, not a quota).

### 2c. `mxr labelqueue` — the evidence surface (read-only)
The terminal edition of v0.4's "cluster/rank + audit sample shown". Reads
`finding_labelqueue` joined to each finding's **latest RAISE row** (`review_raised`, any
current state — NOT "latest open": the queue deliberately contains auto-closed
`applied_fixed` findings awaiting human truth, and the join must not hide them), clustered by
(`rule_tag`, `reviewer_family`), ordered by cluster size desc:

```
unsanitized-injection  oracle   6   src/runtime/curate.py +4 more paths
  keys: 383c0f3f5ab1  5d1adc49f850  386fa1de6433  37b10cfd99ae  ab24c61b7223  8ccd998f3ba4
```

- **Emitted keys are exactly 12-hex** — the minimum the CLI contract accepts — and a test
  asserts `labelqueue` output keys are accepted VERBATIM by the 2b batch verb (copy-paste
  round-trip is the whole point).
- Scope note: the browser shows the ACTIVE queue (`finding_labelqueue` membership — so not
  expired-tombstoned findings). An expired finding can still be labeled by explicit key from
  an old keys-file (2a any-state semantics); the browser just doesn't advertise it.
- Per cluster: count, the key12s (paste-ready for 2b), paths, and `ref`/`play` of the latest
  raise (so the human can pull the verdict file's claim text if wanted).
- Read-only SQL over existing views — no new view, no materialization (v0.4 §4: UI joins are
  request-time, never core views).
- Fail-closed on DB unreachable (exit 2, operator command tier — mirrors knowledge verbs).

### 2d. Keys-file completes the label set
`play-review.sh outcomes_record` adds the third paste-ready line per finding:
`mxr outcome <key12> real      # reviewer was RIGHT — confirmed ground truth`
plus one batch hint at the bottom: `# batch: mxr outcome real <key12> <key12> …`.
(One printf format change + the hint line; fail-open discipline unchanged.)

## 3. Deliberately NOT in PR-A (each with its un-gating condition)

| Deferred | Why | Builds when |
|---|---|---|
| PR-B exec-prior (play-fix observe → `record_exec_prior`) | no auto-path firings (fail_to_pass null), no per-finding linkage; non-gating prior | a repo gains a real `fail_to_pass` proof AND the fixlist carries finding keys (its own plumbing PR) |
| PR-C panel (`propose_outcome` sweep + cluster/rank by uncertainty) | paid decorrelated sweeps; evidence pane for selector-bulk | after PR-A ships and the queue's shape (which classes stay contested) says panels earn their spend |
| Selector-bulk (`--tag`/`--play`) | labels unseen rows; needs shown concurrence evidence (D3) | with PR-C |
| Phone-first UI | it's the Telegram transport build (docs/telegram-transport-design.md v0.2, pre-oracle, not built) | its own design gauntlet + build |
| `labeler_accuracy` view | audits machine labelers; none exist yet | with PR-B/PR-C |

## 4. Data flow

```
review (play-review) ──finding:<tag>@path:line──▶ outcome-record ──▶ finding_outcome (open)
                                                        │
                                              keys-file → jefe inbox (fp|wontfix|real ×3 + batch hint)
                                                        │
human reads (Mack relays / labelqueue browser) ──▶ mxr outcome <kind> <keys…>
                                                        │
                                          confirm_outcome (admin) ──▶ human_confirm/human_dismiss rows
                                                        │
                                      finding_current_human ──▶ finding_precision_promoted (THE gate)
                                                        │
                                            finding_labelqueue shrinks (human-label-terminal)
```

## 5. Failure modes & edges
- **Ambiguous key12 in a batch** → that key refused with candidates, batch continues (per-key
  fail-closed). Grinding a collision yields refusals, never a mislabel (inherited).
- **Same-kind re-issue / batch re-run** → idempotent no-op rows (5-col index), count reports 0.
- **Kind correction (fp→real)** → new row, higher seq, `finding_current_human` takes the latest
  (inherited correction semantics). Known residual (documented in verb): exact flip-flop BACK
  to a prior kind can't re-win — acceptable at solo scale, unchanged by PR-A.
- **`real` on never-raised key** → `_finding_fields` returns None → error surfaced, no row.
- **DB down** → labeling verb prints the error and exits 0 having written nothing (tier-3);
  `labelqueue` exits 2 (operator tier).
- **Concurrent TTL sweep during a batch** → rows are append-only + idempotent; a finding
  expiring mid-batch still takes the human label (human rows are terminal; expiry only matters
  for queue membership, and human-label wins the membership test).

## 6. Security surface
- **No new trust boundary.** Local shell = trusted operator (existing CLI contract,
  postgres_store.py:1642-1648); the CLI hard-codes `principal_role="admin"` exactly as
  `mxr outcome` does today. No new network surface, no new inputs from untrusted sources.
- Keys arrive as argv hex tokens, validated ≥12-hex per key before any DB touch; junk refuses
  per-key. The keys-file remains outbound-only (loop-immunity invariant untouched — no agent
  reads the inbox).
- The fence is untouched: PR-A writes only through the two human verbs already gated + pair-CHECKed.

## 7. Decisions for Jefe
- **D-1 [lean yes]:** route CLI `fp|wontfix` through `confirm_outcome` too (uniform semantics;
  the any-state delta is argued correct in 2a). Alternative: keep `human_dismiss` for fp/wontfix
  and add only `real` via `confirm_outcome` — zero behavioral delta, two code paths forever.
- **D-2 [strong]:** explicit-keys-only bulk in PR-A; selector-bulk waits for panel evidence.

## 8. Test plan (test-first, deterministic)
Extend `tests/test_self_labeling_fence.py` + a new CLI-level check set:
- confirm-real lands in the promoted numerator; real-after-`applied_fixed` writes;
  fp-after-expired writes WITHOUT resurrecting queue membership.
- **each CLI kind maps to exactly its §2a table pair** (assert outcome_source+outcome per
  kind); an illegal pair attempted raw still fails at the DB pair-CHECK (fence holds beneath
  the CLI).
- **`"all"` scope:** a key raised by ONE family writes exactly 1 row (the other family is
  skipped, never labeled unseen).
- batch: mixed valid/ambiguous/junk keys → per-key refusals, earlier+later valid keys all
  written, four-count summary correct; duplicate keys in one argv → deduped, counted;
  batch re-run → 0 rows, idempotent.
- `labelqueue`: clustering matches a seeded queue; includes an `applied_fixed` (auto-closed)
  finding; **emitted keys are 12-hex and round-trip verbatim into the batch verb**.
- keys-file carries all three paste-ready commands + the batch hint (orchestrator/test.sh).
