# Outcomes Ledger — DESIGN (v0.3)

_The self-learning rung's missing primitive: the per-finding OUTCOME LABEL. One append-only
Postgres table + computed SQL views on the existing spine. **v1 COLLECTS ONLY — no dial acts on
the data** (suppression/promotion is a LATER rung, gated on accrued signal + human approval).
Inputs: `docs/research/outcomes-ledger-prior-art.md` (17 confirmed claims, 19 sources) + the
optimal-team brief §3._

_v0.2 folds a 29-agent adversarial in-session review (4 lenses, every non-minor finding
independently re-verified against the code): 23 confirmed findings + 13 minors folded, 2 claims
refuted. Headline fixes: path joined into `finding_key`; recording moved to EVERY delivered
review (the v0.1 seam only fired on NEEDS-FIX, so clean-PASS fixes could never close); sticky
dismissals; human labels outrank auto closes; finding keys surfaced in the delivered verdict
(else the dismissal verb is dead-on-arrival and precision is identically 1.0); revert detection
CUT from v1 as a theory-build._

_v0.3 folds the kilabz (codex) design review — NEEDS-REVISION, 6/6 findings accepted: dismissal
keys move to a SEPARATE follow-up inbox file (the verdict is written before the recorder runs —
annotating it post-hoc was impossible, and a pre-delivery step would re-introduce the stalled-
call-delays-verdict hazard capture was explicitly moved post-delivery to avoid); migration DDL
made idempotent (the migrator re-runs every boot); close scoping tightened to EXACT ref match;
human dedupe made deterministic; `seq bigserial` added as the ordering key; `line_hash` stored
as a column. Status: kilabz-approved shape, oracle (Gemini) pass rides the docs-only automerge
gate on the design PR (or a manual dispatch from the Mini)._

## 1. What & why

Every review the brain runs today is fire-and-forget: kilabz+oracle find issues, lobster triages,
the verdict lands in the jefe inbox — and nothing ever records what HAPPENED to each finding
(fixed? dismissed as wrong? reverted after merge?). Without that label there is no ground truth,
so no precision measurement per finding-class, no evidence-based autonomy widening, no
data-driven skill capture calibration. The fix is the universal prior-art pattern (GitHub
code-scanning / Semgrep / SonarQube): a **per-finding state machine** with a **stable identity
across reviews** and a **constrained dismissal enum**.

Non-goals (v1, explicit): no auto-suppression or promotion dials acting on the data; no LLM
anywhere in the pipeline; no dashboard/UI; no per-finding thumbs-up/down labeling flow; no
embeddings/vector store; no automerge-gate coupling of any kind; **no revert detection** (the
`reverted` enum value is reserved; the detector is a shared primitive that rides the
autonomy-ladder rung, where demotion needs it anyway — building it twice or early is waste).

## 2. Data flow

```
EVERY delivered push review (play-review.sh, NOT gate mode) — PASS and NEEDS-FIX alike:
  └─ post-delivery, bounded + fail-open:
     mxr outcome-record [--kilabz "$review" --oracle "$oracle_review"] -- <repo_path> <tip> <ref> <play> [changed...]
       PHASE 1 — CLOSE (runs on every review, incl. PLAY_PASS):
         for each 'open' finding of THIS repo whose path ∈ this diff's changed set,
         where this review's ref EXACTLY matches the finding's ref (v1 — no default-branch
         closure: a main review must not close findings raised on unrelated feature branches
         whose fix never merged; ancestry-proofed default-branch closure is a LATER refinement,
         and the controller's backstop reviews main anyway, so main-raised findings — the
         common case — close on main):
           stored line_hash no longer present in that file at <tip>
             → INSERT 'applied_fixed' (outcome_source=auto_fix_landed)   [SonarQube auto-Fixed]
       PHASE 2 — OPEN (NEEDS-FIX reviews only; reviews that PASS raised nothing actionable):
         parse per-family `finding:` lines → validate → line-hash at <tip> → INSERT 'open' rows
         SKIP any key whose latest state for that family is dismissed_* (sticky dismissals —
         GitHub/SonarQube both make dismissal suppress re-detection; that's what the stable
         key exists FOR). Re-raise after 'expired' or 'applied_fixed' is allowed (regression).

human (Jefe, or Mack relaying the inbox — ONE command per finding)
  └─ outcome-record, AFTER recording, writes a SEPARATE small inbox file next to the verdict
     ("outcome keys — <play>") listing each recorded finding's short key + a paste-ready command:
         mxr outcome <finding_key12> fp        # reviewer was WRONG → down-weights the class
         mxr outcome <finding_key12> wontfix   # reviewer was RIGHT, human declines → neutral
     (kilabz BLOCKER: the verdict file is WRITTEN before the recorder runs — it cannot be
     annotated post-hoc, and moving recording pre-delivery would re-introduce the stalled-call-
     delays-verdict hazard capture was explicitly moved post-delivery to avoid. A follow-up
     file keeps the verdict path untouched; Mack relays both together.)
     Without this surfacing, dismissal never happens solo and the fp side of precision stays
     empty forever — the dataset would be worthless. The verb FAILS CLOSED on a non-unique or
     <12-hex-char prefix (prints colliding full keys; grinding a 48-bit prefix collision into a
     crafted diff line is no longer a mislabel, just an error message). A dismissal writes one
     row per reviewer_family currently 'open' on that key, with the DETERMINISTIC
     source_event 'human:<finding_key12>' — re-running the command is an index conflict no-op,
     not a duplicate event (kilabz: 'human:<uuid>' would have broken the idempotency claim).

expiry sweep (piggybacks the same outcome-record invocation, cheap SQL)
  └─ 'open' > OUTCOME_TTL_DAYS (default 30, flagged) → INSERT 'expired'
     (keeps denominators honest; expired counts toward neither precision side)

PRECEDENCE (enforced in the verbs, not just convention): a HUMAN row (dismissed_*) is terminal —
auto closes and re-opens never override it; latest-row-wins applies only among machine rows.
```

## 3. Schema (migration `0008_finding_outcome.sql` + `schema.sql` mirror, lockstep)

All DDL is IDEMPOTENT (`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` /
`CREATE OR REPLACE VIEW`) — `PostgresLedger.migrate()` re-runs every migration on every boot
under the advisory lock, exactly like 0007 (kilabz HIGH; also the repo's own promoted
`migration-append-only` skill). Sketch below omits the IF NOT EXISTS noise for readability:

```sql
CREATE TABLE finding_outcome (
    seq             bigserial,       -- monotonic EVENT ORDER: 'latest row' is by seq, not
                                     -- created_at (timestamp ties) or uuid (unordered) — kilabz
    id              uuid PRIMARY KEY,
    finding_key     text NOT NULL,   -- sha256(repo_id \0 rule_tag \0 path \0 line_hash)
                                     -- path IS in the key: SonarQube issue identity is per-file
                                     -- (component+rule+hash); a path-free key lets identical
                                     -- normalized lines in different files collide, and lets a
                                     -- crafted diff line mint a legit finding's key (v0.1 CRIT)
    repo_id         text NOT NULL,
    ref             text NOT NULL,   -- the reviewed ref the finding was raised on (close scoping)
    rule_tag        text NOT NULL,   -- allowlisted capture taxonomy (same list, single source of truth)
    reviewer_family text NOT NULL CHECK (reviewer_family IN ('kilabz','oracle')),
    path            text NOT NULL,   -- validated ∈ the reviewed diff's changed-file set
    line_hash       text NOT NULL,   -- stored, not just folded into the key: the CLOSE phase
                                     -- checks "is this hash still in the file" — recomputing
                                     -- candidate keys by scanning would be absurd (kilabz)
    source_event    text NOT NULL,   -- 'review:<play>' | 'human:<finding_key12>' | 'sweep:<utcday>'
                                     -- (v0.1 had review_run_id NOT NULL — dismiss/sweep rows
                                     -- have no review run; idempotency was undefined for them)
    tip_sha         text NOT NULL,   -- the sha the line-hash was computed/checked at
                                     -- (named tip_sha, NOT base_sha — this repo has already
                                     -- been bitten once by a base/tip mis-wire)
    outcome         text NOT NULL CHECK (outcome IN
                     ('open','applied_fixed','dismissed_false_positive',
                      'dismissed_wontfix','reverted','expired')),   -- 'reverted' reserved, no writer in v1
    outcome_source  text NOT NULL CHECK (outcome_source IN
                     ('review_raised','auto_fix_landed','auto_git_revert','human_dismiss','ttl_sweep')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- append-only: NEVER UPDATE/DELETE. Current state = per (finding_key, reviewer_family):
--   the latest human row if one exists (human-terminal precedence), else the latest row.
-- idempotency: one row per (finding_key, reviewer_family, outcome, source_event) — a re-run of
-- the same review/sweep/dismissal is a no-op conflict, not a duplicate event.
CREATE UNIQUE INDEX finding_outcome_event_once
    ON finding_outcome (finding_key, reviewer_family, outcome, source_event);
CREATE INDEX finding_outcome_key_idx  ON finding_outcome (finding_key, created_at DESC);
CREATE INDEX finding_outcome_open_idx ON finding_outcome (repo_id, path) WHERE outcome = 'open';
```

`line_hash` (SonarQube borrow, pure fns in a new `runtime/outcomes.py`, DB-free like
`capture.py`): sha256 of the whitespace-normalized CONTENT of the flagged line as read from git
objects at the reviewed tip (`git show <tip>:<path>`, line N) — never the line number, so the
identity survives diff shifts. Validation is layered and fail-closed for data quality: the
reviewer's `<path>:<line>` must (a) name a path in the changed-file set, (b) fall INSIDE one of
this diff's changed hunks for that path (a wrong-but-resolvable line number can't silently key a
finding to unrelated code), (c) resolve to a non-empty line. Any miss → the finding row is
dropped + noted. Known, accepted aliasing: two identical lines in the SAME file share a hash
(SonarQube accepts the same); cross-FILE aliasing is gone because path is in the key.

## 4. Finding extraction — a NEW line, capture's line untouched

v0.1 extended capture's `rule:<tag>` line in place. That was wrong twice over: the two rungs
have CONFLICTING emission semantics (capture asks for tags on RECURRING classes only; outcomes
needs EVERY finding keyed — one line biases one rung or dilutes the other), and capture is LIVE
and armed (changing its parser input distribution ships a silent behavior change to a running
rung). So:

```
rule:<tag>                       ← capture's line, UNCHANGED (recurring classes only)
finding:<tag> @ <path>:<line>    ← outcomes' line, one per finding, both reviewers
```

- Same allowlisted taxonomy for `<tag>` (one source of truth, `--list-tags`).
- The `finding:` prompt sentence is emitted when `$ORCH/OUTCOMES_ENABLED` — its OWN flag, no
  CAPTURE_ENABLED coupling in either direction (v0.1 claimed independence but rode capture's
  prompt; false claim, now true by construction).
- Tokenization is anchored: the LAST ` @ ` on the line splits fields; `<path>` may contain
  spaces/`:`/`@`; `<line>` is the digits after the final `:`. Ctrl chars rejected on the RAW
  line before any strip (capture round-2 lesson). Paths under `skills/` and refs matching
  `skill/auto/*` are excluded (the sibling rung's self-exclusion rules apply here too).
- Per-family attribution: outcome-record parses `$review` and `$oracle_review` SEPARATELY —
  per-family precision is the whole measurement.
- Per-run bound: at most OUTCOME_MAX_ROWS (default 50, flagged) finding rows per review; the
  honest spam bound is tags × LINES in the diff (not × files), so an explicit cap replaces the
  v0.1 hand-wave. Overflow → recorded count noted, remainder dropped.

## 5. SQL views (COMPUTE only in v1 — nothing acts)

Views read CURRENT STATE (latest row per key×family, human-terminal precedence), never raw event
counts — event-counting double-counts re-raises and lets churn distort the ratios (v0.1 elided
this and either reading broke a metric):

```sql
CREATE VIEW finding_current AS ...;   -- one row per (finding_key, reviewer_family): resolved
                                      -- state = latest-by-seq (human-terminal precedence first)
CREATE VIEW finding_precision AS      -- per (rule_tag × reviewer_family), over ALL history:
  -- precision = applied_fixed / NULLIF(applied_fixed + dismissed_false_positive, 0)
  -- volume    = count(*) of current findings
  ...;
```

No time window in v1: at solo review volume a 90-day window can silently discard most of the
scarce history (a class may see a handful of labels per quarter). Windowing is a LATER-rung
tuning decision made with real volume in hand — a one-line view change on an append-only table
that loses nothing meanwhile. Ad-hoc visibility: `mxr outcome-stats` prints the views (no
dashboard; this also serves the morning brain-check).

Deferred to the LATER rung (do NOT build now): the suppress/promote dial, the weekly SQL report,
revert detection, any threshold acting on these views.

## 6. Security surface & failure modes

- **Untrusted input:** finding lines are LLM output over an untrusted diff. Defenses (reusing
  the capture rung's proven pieces): rule_tag ∈ allowlisted taxonomy; path ∈ changed-file set;
  line ∈ a changed hunk of that path; ctrl-chars rejected raw; `sys.argv` only, no
  interpolation; git-object reads (`git show tip:path`), never the working tree; per-run row cap.
- **Key forgery:** finding_key covers (repo, tag, path, line-content) — to collide with a legit
  finding an attacker must plant the SAME normalized line in the SAME file the legit finding is
  in, i.e. touch the code the finding is about, which a human reviews on merge. Cross-file
  minting (the v0.1 hole) is closed by path-in-key; prefix-grinding is closed by the fail-closed
  ≥12-hex dismissal verb.
- **Availability:** recording is post-delivery, bounded (cap_run alarm — build must verify ONE
  bounded invocation covers the git-show batch; batch the reads, don't N× the alarm), fail-open.
  Default OFF (`$ORCH/OUTCOMES_ENABLED`), HARD no-op in gate mode (`PLAY_GATE=1`).
- **Human-vs-auto race:** precedence rule in §2 — verbs never write an auto row over a human
  terminal state, so the winner is policy, not wall-clock.
- **Parser drift → zero rows:** fail-open means silence, not breakage; `mxr outcome-stats` in
  the morning brain-check makes starvation VISIBLE (v0.1 pointed at a health check that doesn't
  exist; the stats verb is the concrete surface).
- **Known-accepted false labels (documented, tolerated in v1):** file rename or whole-file
  delete reads as applied_fixed (line gone); a fix that lands on a branch never reviewed again
  expires instead of closing. Both are noise the volume floor at the LATER rung must absorb;
  neither is attacker-steerable beyond what merging arbitrary code already implies.

## 7. Borrowed / built / rejected (from the brief §F–G)

| Piece | Verdict |
|---|---|
| append-only `finding_outcome` + views | BUILD (one table on the spine) |
| line-hash stable identity, path-scoped | BORROW pattern (SonarQube — identity is per-file) |
| dismissal enum fp vs wontfix, STICKY on re-detection | BORROW pattern (GitHub/SonarQube) |
| auto fix-landed on next scan | BORROW pattern (SonarQube auto-Fixed) |
| reviewer-as-classifier per-family precision | BORROW pattern (LLM-judge lit) |
| revert detection | DEFER to the autonomy-ladder rung (shared primitive there) |
| fine-tuning / embeddings / vector store / dashboard / kappa stats / per-finding voting UI | REJECT |

## 8. Build plan (feature-flagged, each PR suite-green)

- **PR-A** `outcomes.py` pure (line-hash, `finding:` parser, hunk validation) + migration 0008 +
  schema.sql mirror + ledger verbs (`record_findings` incl. sticky-dismiss + close-phase,
  `human_dismiss` fail-closed prefix, `expire_open`, `outcome_stats`) + tests (adversarial:
  tag/path/line injection, hunk-outside line, cross-file collision attempt, dup spam, human-
  precedence race, PASS-review close).
- **PR-B** `mxr outcome-record` / `mxr outcome` / `mxr outcome-stats` + play-review wiring on
  BOTH verdict branches (close-phase on PASS too) + the `finding:` prompt sentence behind
  OUTCOMES_ENABLED + per-finding keys & paste-ready dismiss commands in the delivered verdict +
  fixture tests. Capture's parser and prompt line are untouched by construction.

Deploy = normal serve-restart auto-migrate; arm = `touch $ORCH/OUTCOMES_ENABLED`; disarm = rm.
