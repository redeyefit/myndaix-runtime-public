-- Migration 0007: capture_candidate + capture_occurrence — the auto-capture rung ("the proposer")
-- recurrence ledger (v0.4). Idempotent (CREATE ... IF NOT EXISTS); re-run on every serve() boot
-- under migrate()'s advisory lock. Inert until the proposer runs. Design: docs/auto-capture-design.md.
--
-- This migration is being authored on the (unmerged, never-applied) feat/auto-capture branch, so its
-- CREATE is rewritten in place rather than healed — the v0.3 shape never shipped to any DB. Once this
-- lands on main, the migration-append-only rule applies: change the schema via a guarded ALTER, never
-- by editing this CREATE.
--
-- WHY: the +learning rung injects HUMAN-seeded skills; a corpus that depends on the human remembering
-- to seed it won't get fed. Auto-capture watches our OWN reviewers: when the SAME allowlisted finding-
-- class (rule:<tag>) recurs with enough INDEPENDENT signal, it DRAFTS a skills/<slug>/SKILL.md and
-- opens a PR — the proposer NEVER promotes (skills/ is denylisted from auto-merge; the human merge
-- under branch protection is the unchanged promotion). NO LLM in the trigger.
--
-- v0.4 keys the class on (repo_scope, rule_tag) [Recon delta], NOT a file glob. Recurrence is
-- MULTI-SIGNAL (S3): one occurrence row per (class, commit), recorded ONLY when both families agreed,
-- and a class becomes `ready` only on distinct-commit + distinct-event + distinct-author thresholds.
-- The glob is kept as SECONDARY locality (it becomes the proposed skill's path_trigger).

-- HEAL a pre-ship v0.3 remnant: an early cut of THIS branch shipped a (repo, path-glob)-keyed
-- capture_candidate with a seen_count column (and a serve re-migrate then created the v0.4
-- capture_occurrence beside it — a broken MIXED state). migrate() re-runs every boot via CREATE IF
-- NOT EXISTS, so the old table would silently survive and v0.4 inserts (rule_tag, ...) would crash.
-- 0007 never reached main, so drop the remnant and let the v0.4 CREATE below apply cleanly. Guarded
-- on the ABSENCE of the v0.4 rule_tag column → idempotent (after the heal it never fires again) and
-- it NEVER drops a healthy v0.4 table, so it does NOT churn data on every boot (migration-append-only).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
              WHERE table_schema = 'public' AND table_name = 'capture_candidate')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_schema = 'public' AND table_name = 'capture_candidate'
                AND column_name = 'rule_tag') THEN
    DROP TABLE IF EXISTS capture_occurrence, capture_candidate CASCADE;
  END IF;
END $$;

-- The recurrence CLASS: one row per (repo, allowlisted rule_tag). S6 state machine.
CREATE TABLE IF NOT EXISTS capture_candidate (
    fingerprint   text PRIMARY KEY,                 -- sha256(repo_scope \0 rule_tag): the class key
    repo_scope    text NOT NULL,
    rule_tag      text NOT NULL,                     -- allowlisted taxonomy tag (recurrence class)
    path_glob     text,                              -- SECONDARY locality -> proposed path_trigger
    state         text NOT NULL DEFAULT 'new'        -- S6 two-phase idempotent state machine
                  CHECK (state IN ('new','accumulating','ready','proposing',
                                   'proposed','promoted','declined','stale','error')),
    branch        text,                              -- skill/auto/<slug> (pinned at ready->proposing)
    draft_sha     text,                              -- sha256(rendered SKILL.md) (S6 idempotency)
    pr_number     int,                               -- the open proposal PR (state='proposed')
    decline_count int NOT NULL DEFAULT 0 CHECK (decline_count >= 0),  -- repropose floor (S8)
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    proposed_at   timestamptz                        -- when the PR opened (TTL anti-wedge, S8)
);

-- One row per recorded sighting, deduped per (class, commit) so distinct-signal counts are exact.
-- A row is inserted ONLY for a cross-family-agreed occurrence (the caller enforces both families);
-- distinct commits = COUNT(*), distinct events = COUNT(DISTINCT event_id), authors likewise.
CREATE TABLE IF NOT EXISTS capture_occurrence (
    fingerprint  text NOT NULL REFERENCES capture_candidate(fingerprint) ON DELETE CASCADE,
    commit_sha   text NOT NULL,
    event_id     text NOT NULL,                      -- the review/push event (run id): temporal indep.
    author       text NOT NULL,
    path_glob    text,
    seen_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fingerprint, commit_sha)            -- one occurrence per (class, commit)
);

CREATE INDEX IF NOT EXISTS idx_capture_candidate_state ON capture_candidate (state);
