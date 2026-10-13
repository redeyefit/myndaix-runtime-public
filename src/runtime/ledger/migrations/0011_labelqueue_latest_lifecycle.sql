-- Migration 0011: fix finding_labelqueue's expired-tombstone to be LATEST-lifecycle-aware.
--
-- BUG (found by the autonomous review loop on its own merge of 0010, verified against the code):
-- 0010's finding_labelqueue tombstoned a (finding_key, reviewer_family) whenever ANY historical row
-- had outcome='expired'. But the ledger EXPLICITLY allows re-raise-after-expired (postgres_store.py:
-- "Re-raise after 'expired' or 'applied_fixed' is allowed (a regression)"): a finding that expired via
-- ttl_sweep and later legitimately re-detects gets a fresh 'open'/review_raised row and becomes current
-- again. The all-history NOT EXISTS check kept it hidden from the human label queue FOREVER — silently
-- dropping a real, re-raised finding from the very queue the fence exists to feed.
--
-- FIX: tombstone iff the LATEST open/expire LIFECYCLE-TOGGLE row (newest by seq among ONLY
-- review_raised [open] and ttl_sweep [expired]) is 'expired'. A genuine re-raise inserts a higher-seq
-- review_raised row -> latest toggle is no longer 'expired' -> the finding reappears. LABEL sources
-- (panel_proposed, exec_verified, auto_fix_landed, auto_git_revert) are DELIBERATELY excluded from the
-- toggle: a machine write must NEVER change queue membership (adding OR removing) — only a human label
-- (handled by the first NOT EXISTS) or a real detect/expire lifecycle transition may (fence invariant).
-- (An earlier form checked the latest NON-human row, which let a stale post-TTL panel_proposed/
-- exec_verified write RESURRECT an expired finding without a fresh re-detection — kilabz, r1.)
--
-- Idempotent (CREATE OR REPLACE VIEW); re-run on every serve boot. Data-safe: only redefines a view.

CREATE OR REPLACE VIEW finding_labelqueue AS
SELECT DISTINCT ON (fo.finding_key, fo.reviewer_family)
       fo.finding_key, fo.reviewer_family, fo.repo_id, fo.ref, fo.rule_tag,
       fo.path, fo.line_hash, fo.tip_sha, fo.seq
  FROM finding_outcome fo
 WHERE NOT EXISTS (SELECT 1 FROM finding_outcome h
                    WHERE h.finding_key = fo.finding_key
                      AND h.reviewer_family = fo.reviewer_family
                      AND h.outcome_source IN ('human_confirm','human_dismiss'))
   -- latest-lifecycle-aware tombstone: exclude ONLY if the newest open/expire TOGGLE row is 'expired'.
   -- The toggle is JUST review_raised (open) + ttl_sweep (expired); label sources can't move it, so a
   -- machine write can never resurrect an expired finding — only a real re-detection (review_raised).
   AND 'expired' IS DISTINCT FROM (
         SELECT x.outcome
           FROM finding_outcome x
          WHERE x.finding_key = fo.finding_key
            AND x.reviewer_family = fo.reviewer_family
            AND x.outcome_source IN ('review_raised','ttl_sweep')
          ORDER BY x.seq DESC
          LIMIT 1)
 ORDER BY fo.finding_key, fo.reviewer_family, fo.seq DESC;
