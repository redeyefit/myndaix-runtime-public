-- Migration 0012: finding_current's human-terminal precedence covers BOTH human sources.
--
-- BUG (label-throughput PR-A code-gate, kilabz MED): confirm_outcome now mints human_confirm
-- rows (`mxr outcome <key> real`), but finding_current boosted ONLY human_dismiss — so after an
-- fp -> real correction the raw current-state view kept showing the STALE fp (while
-- finding_current_human correctly showed real), and downstream readers of finding_current
-- (record_findings, verbs' _finding_fields, outcome_stats) saw a state inconsistent with the
-- human's latest word.
--
-- FIX: ANY human row (human_dismiss OR human_confirm) is terminal for current-state purposes;
-- among multiple human rows the LATEST wins (a correction has higher seq). Machine rows can
-- never outrank a human row, exactly as the fence promises everywhere else.
--
-- Idempotent (CREATE OR REPLACE VIEW); re-run on every serve boot. Data-safe: view-only.

CREATE OR REPLACE VIEW finding_current AS
SELECT DISTINCT ON (finding_key, reviewer_family)
       finding_key, reviewer_family, repo_id, ref, rule_tag, path, line_hash,
       outcome, outcome_source, tip_sha, source_event, created_at, seq
  FROM finding_outcome
 ORDER BY finding_key, reviewer_family,
          (outcome_source IN ('human_dismiss', 'human_confirm')) DESC,  -- ANY human row is terminal
          seq DESC;                                 -- then latest event wins (incl. among human rows)
