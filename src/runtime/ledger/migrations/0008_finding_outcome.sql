-- Migration 0008: finding_outcome — the outcomes-ledger rung (the per-finding OUTCOME LABEL).
-- Append-only event log + computed views on the existing spine (v0.3, cross-family reviewed).
-- Idempotent (CREATE TABLE/INDEX IF NOT EXISTS, CREATE OR REPLACE VIEW); re-run on every serve()
-- boot under migrate()'s advisory lock. Inert until play-review wiring lands (PR-B) + the
-- OUTCOMES_ENABLED flag is set. Design: docs/outcomes-ledger-design.md.
--
-- This migration is being authored on the (unmerged, never-applied) feat/outcomes-ledger branch, so
-- there is nothing to HEAL. Once it lands on main the migration-append-only rule applies: evolve the
-- schema via a guarded ALTER, never by editing this CREATE.
--
-- WHY: every review the brain runs is fire-and-forget — nothing records what HAPPENED to each
-- finding (fixed? dismissed as wrong? expired?). Without that label there is no ground truth, so no
-- per-class precision, no evidence-based autonomy widening. The fix is the universal prior-art
-- pattern (GitHub code-scanning / Semgrep / SonarQube): a per-finding STATE MACHINE with a STABLE
-- identity across reviews (path-scoped line-hash) and a CONSTRAINED dismissal enum. NO LLM anywhere
-- in the pipeline. v1 COLLECTS ONLY — no dial acts on the data.

-- One append-only EVENT per (finding, reviewer_family, outcome, source_event). NEVER UPDATE/DELETE.
-- Current state is COMPUTED (finding_current view): the latest human row if one exists (human-
-- terminal precedence), else the latest machine row, ordered by `seq` (a timestamp can tie and a
-- uuid is unordered — kilabz). `line_hash` is a stored COLUMN (not just folded into finding_key) so
-- the CLOSE phase can ask "is this hash still in the file" without rescanning.
CREATE TABLE IF NOT EXISTS finding_outcome (
    seq             bigserial,       -- monotonic EVENT ORDER: 'latest row' is by seq, not created_at
    id              uuid PRIMARY KEY,
    finding_key     text NOT NULL,   -- sha256(repo_id \0 rule_tag \0 path \0 line_hash) — PATH is in
                                     -- the key so cross-file identical lines never collide (CRIT fix)
    repo_id         text NOT NULL,
    ref             text NOT NULL,   -- the reviewed ref the finding was raised on (EXACT close scope)
    rule_tag        text NOT NULL,   -- allowlisted capture taxonomy (shared, single source of truth)
    reviewer_family text NOT NULL CHECK (reviewer_family IN ('kilabz','oracle')),
    path            text NOT NULL,   -- validated ∈ the reviewed diff's changed-file set
    line_hash       text NOT NULL,   -- sha256 of the normalized flagged-line CONTENT at tip_sha
    source_event    text NOT NULL,   -- 'review:<play>' | 'human:<finding_key12>:<kind>' | 'sweep:<utcday>'
                                     -- (kind ∈ fp|wontfix is IN the human event so a fp<->wontfix
                                     -- correction is a distinct tuple that inserts, not a no-op)
    tip_sha         text NOT NULL,   -- the sha the line-hash was computed/checked at (NOT base_sha)
    outcome         text NOT NULL CHECK (outcome IN
                     ('open','applied_fixed','dismissed_false_positive',
                      'dismissed_wontfix','reverted','expired')),  -- 'reverted' reserved, no v1 writer
    outcome_source  text NOT NULL CHECK (outcome_source IN
                     ('review_raised','auto_fix_landed','auto_git_revert','human_dismiss','ttl_sweep')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- idempotency: one row per (finding_key, reviewer_family, outcome, source_event). A re-run of the
-- SAME review/sweep/dismissal is an ON CONFLICT DO NOTHING no-op, not a duplicate event.
CREATE UNIQUE INDEX IF NOT EXISTS finding_outcome_event_once
    ON finding_outcome (finding_key, reviewer_family, outcome, source_event);
CREATE INDEX IF NOT EXISTS finding_outcome_key_idx
    ON finding_outcome (finding_key, created_at DESC);
-- the CLOSE + sticky-dismiss scans read open findings by (repo, path)
CREATE INDEX IF NOT EXISTS finding_outcome_open_idx
    ON finding_outcome (repo_id, path) WHERE outcome = 'open';

-- CURRENT STATE per (finding_key, reviewer_family): a HUMAN row (dismissed_*) is TERMINAL — it
-- outranks any later machine row (human-terminal precedence, enforced here so a view reader sees the
-- same winner the verbs enforce). Otherwise latest-by-seq wins. DISTINCT ON picks the first row per
-- key×family under the ORDER BY: human rows first (outcome_source='human_dismiss' -> rank 0), then
-- by seq DESC, so a dismissed_* row always beats a later applied_fixed/open/expired for that key.
CREATE OR REPLACE VIEW finding_current AS
SELECT DISTINCT ON (finding_key, reviewer_family)
       finding_key, reviewer_family, repo_id, ref, rule_tag, path, line_hash,
       outcome, outcome_source, tip_sha, source_event, created_at, seq
  FROM finding_outcome
 ORDER BY finding_key, reviewer_family,
          (outcome_source = 'human_dismiss') DESC,   -- human-terminal precedence FIRST
          seq DESC;                                  -- then latest event wins among the rest

-- PRECISION per (rule_tag × reviewer_family), over ALL history (NO time window in v1 — at solo
-- volume a window silently discards most of the scarce history). Reads CURRENT state, never raw
-- event counts (event-counting double-counts re-raises). precision = applied_fixed /
-- (applied_fixed + dismissed_false_positive); NULL when that denominator is 0 (no labels yet).
-- volume = count of CURRENT findings in the class (any resolved state).
CREATE OR REPLACE VIEW finding_precision AS
SELECT rule_tag,
       reviewer_family,
       count(*) FILTER (WHERE outcome = 'applied_fixed')              AS applied_fixed,
       count(*) FILTER (WHERE outcome = 'dismissed_false_positive')   AS dismissed_false_positive,
       count(*)                                                       AS volume,
       count(*) FILTER (WHERE outcome = 'applied_fixed')::numeric
         / NULLIF(count(*) FILTER (WHERE outcome IN
             ('applied_fixed','dismissed_false_positive')), 0)        AS precision
  FROM finding_current
 GROUP BY rule_tag, reviewer_family;
