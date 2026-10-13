-- Migration 0003: review_cursor — the controller-loop ("the brain") durable state.
--
-- schema.sql is fresh-DB DDL (run once by init_schema). An EXISTING Postgres
-- deployment runs THIS idempotent migration on every serve() boot (behind the
-- migrate() advisory lock), so it MUST be safe to re-run: CREATE ... IF NOT EXISTS
-- only, no destructive backfill.
--
--   psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0003_review_cursor.sql
--
-- WHY a table (controller-loop DESIGN v0.2 §2, B4): the proactive review scheduler
-- is a level-triggered reconciler that must know, per (repo, ref), the last SHA
-- whose review actually DELIVERED and whether one is in flight. Neither is derivable
-- from job.base_ref (stamped = reviewed tip BEFORE delivery, so a row exists even for
-- a review that later aborts) nor from play-review's done-<sha> file markers (state
-- belongs in the ledger, not files — north-star litmus). This row is the brain's only
-- state; it is inert (read by nothing) until the controller runs, so shipping the
-- migration is safe ahead of the controller.
--
--   baseline_sha — HEAD at first sight; seeded as a high-water mark, NOT reviewed
--                  (avoids the whole-tree EMPTY_TREE diff blow-up on a fresh repo, B2)
--   reviewed_sha — last SHA whose review DELIVERED; the cursor advances here only on
--                  a confirmed done-<sha>. head == reviewed_sha => up to date.
--   pending_sha  — the SHA currently dispatched/in-flight; NULL when idle.
--   state        — observability + the sticky 'blocked' terminal (per pending head).
--   attempts     — consecutive dispatch attempts for the CURRENT pending_sha; reset to
--                  1 when a new head is claimed, 0 on advance. >= MAX => blocked.
CREATE TABLE IF NOT EXISTS review_cursor (
    repo_id      text NOT NULL,
    ref          text NOT NULL,
    baseline_sha text NOT NULL,
    reviewed_sha text NOT NULL,
    pending_sha  text,
    state        text NOT NULL DEFAULT 'baseline'
        CHECK (state IN ('baseline','dispatching','delivered','blocked')),
    attempts     int  NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, ref)
);
