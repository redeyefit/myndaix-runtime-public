-- Migration 0004: automerge_seen — the docs-only PR auto-merge gate's decision log.
--
-- Idempotent (CREATE ... IF NOT EXISTS); re-run on every serve() boot behind the
-- migrate() advisory lock. Inert until runtime.automerge runs.
--
--   psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0004_automerge_seen.sql
--
-- WHY (automerge DESIGN v0.3 §4): the gate must NOT re-review / re-attempt the same
-- (PR, head) every hourly tick — that burns the GitHub rate limit + the LLM review
-- budget and (on a NEEDS-FIX) would loop forever. One row per (repo_id, pr_number,
-- head_sha) records the terminal decision; a new head (a fresh push) is a new row, so
-- a re-pushed PR is re-evaluated. Keyed by HEAD sha so a stale decision can never apply
-- to changed content.
--   decision: merged | needs_fix | skipped | error   (reason carries the detail)
CREATE TABLE IF NOT EXISTS automerge_seen (
    repo_id    text NOT NULL,
    pr_number  int  NOT NULL CHECK (pr_number > 0),
    head_sha   text NOT NULL,
    decision   text NOT NULL
        CHECK (decision IN ('merged','needs_fix','skipped','error')),
    reason     text,
    decided_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, pr_number, head_sha)
);
