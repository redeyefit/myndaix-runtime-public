-- Migration 0010: self-labeling fence (docs/self-labeling-design.md v0.4, dual-family APPROVE).
--
-- PR-1 = THE FENCE ONLY (no labeler yet). It HEALS 0008's finding_outcome so machine-proposed
-- labels can NEVER reach the autonomy-gating metric OR remove a finding from the human queue until
-- a HUMAN confirms. Append-only rule: this evolves the schema via guarded ALTER; NEVER edit 0008.
--
-- The fence is a closed algebra over (outcome_source, outcome):
--   • GATING inputs  = human sources only {human_confirm, human_dismiss} -> finding_precision_promoted
--   • LABEL-terminal = the same two human sources only -> a machine never leaves finding_labelqueue
--   • LIFECYCLE      = ttl_sweep/expired ages out a stale unlabeled finding (a tombstone, not a label)
-- Write-authority (the three server-minted verbs + principal->source matrix) lives in postgres_store.
--
-- Idempotent (DROP/ADD CONSTRAINT, IF (NOT) EXISTS, CREATE OR REPLACE VIEW); re-run every serve boot
-- under migrate()'s advisory lock. 0008/schema.sql recreate finding_precision every boot; this drops
-- it every boot AFTER (filename order) — a harmless recreate/drop that nets to "gone".

-- (1) Widen the two INDEPENDENT value CHECKs (each a SUPERSET of 0008's, so ADD CONSTRAINT can never
--     reject an existing row). New label sources + a human-REAL source; new label outcomes.
ALTER TABLE finding_outcome DROP CONSTRAINT IF EXISTS finding_outcome_outcome_check;
ALTER TABLE finding_outcome ADD  CONSTRAINT finding_outcome_outcome_check CHECK (outcome IN
    ('open','applied_fixed','dismissed_false_positive','dismissed_wontfix','reverted','expired',
     'confirmed_real','exec_real_prior','panel_real','panel_fp'));
ALTER TABLE finding_outcome DROP CONSTRAINT IF EXISTS finding_outcome_outcome_source_check;
ALTER TABLE finding_outcome ADD  CONSTRAINT finding_outcome_outcome_source_check CHECK (outcome_source IN
    ('review_raised','auto_fix_landed','auto_git_revert','human_dismiss','ttl_sweep',
     'panel_proposed','exec_verified','human_confirm'));

-- (1b) PAIR check: the views TRUST outcome_source (finding_current_human filters human sources;
--      finding_labelqueue treats any 'expired' as a tombstone; finding_precision_promoted counts
--      confirmed_real). Independent value-CHECKs alone would let a RAW/buggy INSERT forge an illegal
--      pair — e.g. ('panel_proposed','expired') to tombstone a finding via a MACHINE, or
--      ('review_raised','confirmed_real') to gate — breaking the fence outside the verbs. This closed
--      pair algebra makes the DB back up the §5 verb matrix (kilabz code-review HIGH). Non-breaking:
--      every existing + verb-minted row matches exactly one pair.
ALTER TABLE finding_outcome DROP CONSTRAINT IF EXISTS finding_outcome_source_outcome_pair;
ALTER TABLE finding_outcome ADD  CONSTRAINT finding_outcome_source_outcome_pair CHECK (
       (outcome_source = 'review_raised'   AND outcome = 'open')
    OR (outcome_source = 'auto_fix_landed' AND outcome = 'applied_fixed')
    OR (outcome_source = 'auto_git_revert' AND outcome = 'reverted')
    OR (outcome_source = 'human_dismiss'   AND outcome IN ('dismissed_false_positive','dismissed_wontfix'))
    OR (outcome_source = 'ttl_sweep'       AND outcome = 'expired')
    OR (outcome_source = 'human_confirm'   AND outcome = 'confirmed_real')
    OR (outcome_source = 'exec_verified'   AND outcome = 'exec_real_prior')
    OR (outcome_source = 'panel_proposed'  AND outcome IN ('panel_real','panel_fp')));

-- (2) Source-aware idempotency: add outcome_source to the unique tuple so cross-source events can
--     never collide or silently shadow a human promotion. New index name; drop the 4-col one.
--     (The four existing ON CONFLICT writers are updated to the 5-col target in postgres_store.py.)
CREATE UNIQUE INDEX IF NOT EXISTS finding_outcome_event_src_once
    ON finding_outcome (finding_key, reviewer_family, outcome, outcome_source, source_event);
DROP INDEX IF EXISTS finding_outcome_event_once;

-- (3) finding_current_human: ONE current HUMAN label per (finding_key, reviewer_family) — the ONLY
--     source of the gating metric. Latest human row by seq (a correction has a higher seq); no
--     machine row can appear here (WHERE clause), so DISTINCT ON needs no precedence trick.
CREATE OR REPLACE VIEW finding_current_human AS
SELECT DISTINCT ON (finding_key, reviewer_family)
       finding_key, reviewer_family, repo_id, ref, rule_tag, path, line_hash,
       outcome, outcome_source, tip_sha, source_event, created_at, seq
  FROM finding_outcome
 WHERE outcome_source IN ('human_confirm','human_dismiss')
 ORDER BY finding_key, reviewer_family, seq DESC;

-- (4) finding_precision_promoted: THE autonomy-facing metric. confirmed_real / (confirmed_real +
--     dismissed_false_positive) over CURRENT HUMAN labels only. Machine outcomes are structurally
--     absent (finding_current_human excludes them); the CUT lever (applied_fixed) is absent too.
CREATE OR REPLACE VIEW finding_precision_promoted AS
SELECT rule_tag, reviewer_family,
       count(*) FILTER (WHERE outcome = 'confirmed_real')            AS confirmed_real,
       count(*) FILTER (WHERE outcome = 'dismissed_false_positive')  AS dismissed_false_positive,
       count(*)                                                      AS volume,
       count(*) FILTER (WHERE outcome = 'confirmed_real')::numeric
         / NULLIF(count(*) FILTER (WHERE outcome IN
             ('confirmed_real','dismissed_false_positive')), 0)      AS precision
  FROM finding_current_human
 GROUP BY rule_tag, reviewer_family;

-- (5) finding_labelqueue: the human queue + the labeler sweep input. A (finding_key, family) is
--     present iff it has NO human label AND is not lifecycle-tombstoned (expired). EVERY machine
--     LABEL source (panel_proposed, exec_verified, auto_fix_landed, review_raised, auto_git_revert)
--     is invisible to terminal resolution — a machine can NEVER remove a finding from this queue.
CREATE OR REPLACE VIEW finding_labelqueue AS
SELECT DISTINCT ON (fo.finding_key, fo.reviewer_family)
       fo.finding_key, fo.reviewer_family, fo.repo_id, fo.ref, fo.rule_tag,
       fo.path, fo.line_hash, fo.tip_sha, fo.seq
  FROM finding_outcome fo
 WHERE NOT EXISTS (SELECT 1 FROM finding_outcome h
                    WHERE h.finding_key = fo.finding_key
                      AND h.reviewer_family = fo.reviewer_family
                      AND h.outcome_source IN ('human_confirm','human_dismiss'))
   AND NOT EXISTS (SELECT 1 FROM finding_outcome e
                    WHERE e.finding_key = fo.finding_key
                      AND e.reviewer_family = fo.reviewer_family
                      AND e.outcome = 'expired')
 ORDER BY fo.finding_key, fo.reviewer_family, fo.seq DESC;

-- (6) Rename the v1 all-source precision to _raw (diagnostic-only, the accuracy-audit baseline). The
--     ONLY autonomy-facing name is finding_precision_promoted; no all-source view keeps a
--     gating-suggestive name, and there is NO finding_current_resolved (oracle r3 attractive-nuisance).
CREATE OR REPLACE VIEW finding_precision_raw AS
SELECT rule_tag, reviewer_family,
       count(*) FILTER (WHERE outcome = 'applied_fixed')              AS applied_fixed,
       count(*) FILTER (WHERE outcome = 'dismissed_false_positive')   AS dismissed_false_positive,
       count(*)                                                       AS volume,
       count(*) FILTER (WHERE outcome = 'applied_fixed')::numeric
         / NULLIF(count(*) FILTER (WHERE outcome IN
             ('applied_fixed','dismissed_false_positive')), 0)        AS precision
  FROM finding_current
 GROUP BY rule_tag, reviewer_family;
DROP VIEW IF EXISTS finding_precision;
