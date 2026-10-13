-- Migration 0014: inbox_cursor — the Inbox Assistant's per-account Gmail pull state.
--
-- An EXISTING Postgres deployment runs THIS idempotent migration on every serve()
-- boot (behind the migrate() advisory lock), so it MUST be safe to re-run:
-- CREATE ... IF NOT EXISTS only, no destructive backfill.
--
--   psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0014_inbox_cursor.sql
--
-- WHY a table (inbox-assistant DESIGN §3.9): the morning tick pulls each Gmail
-- account incrementally via users.history.list, which needs the last historyId whose
-- slice was fully PROCESSED — advanced only after labels/drafts/brief delivered, never
-- on pull alone, so a crash mid-run re-pulls rather than drops threads. State belongs
-- in the ledger, not files (north-star litmus). Google expires historyIds (a 404 from
-- users.history.list, DESIGN §4 CRITICAL); the tick then re-establishes via a bounded
-- backfill (checkpoint-before-scan: getProfile FIRST) and advances here. This row is
-- inert (read by nothing) until the tick runs, so shipping the migration is safe
-- ahead of the tick.
--
--   account_id — the full Gmail address (one row per INBOX_ACCOUNTS entry).
--   history_id — last historyId whose slice fully PROCESSED; next pull starts here.
--                NULL = an error-SEEDED row (r10 #2): a first-run account whose classify
--                failed before its first advance. The pull path treats NULL exactly like
--                no-row (bounded backfill), so a seed can never corrupt incremental
--                pulls — it exists ONLY so `attempts` accumulates and the bounded-loss
--                valve reaches first-run accounts too. The first successful advance sets
--                a real history_id (its CAS expectation NULL matches the seeded row).
--   state      — 'active' (healthy) | 'error' (pull/process failed) | 'stale'
--                (cursor expired, awaiting backfill re-establish). Advance resets
--                to 'active'.
--   attempts   — consecutive failed ticks for this account; reset to 0 on advance.
CREATE TABLE IF NOT EXISTS inbox_cursor (
    account_id text NOT NULL PRIMARY KEY,
    history_id text,
    state      text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','error','stale')),
    attempts   int  NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- NOTE: 0014 never shipped to main, so the nullable history_id above IS the canonical
-- shape. If a dev DB ran this migration's brief interim NOT NULL form (pre-merge PR-95
-- branch only), fix it manually — the substrate migration lint deliberately fail-closes
-- ALTER contractions, so no automated ALTER lives here:
--   ALTER TABLE inbox_cursor ALTER COLUMN history_id DROP NOT NULL;
