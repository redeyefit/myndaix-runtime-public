-- Migration 0001: add job.context (free-form per-job input, e.g. {"image_url": ...}).
--
-- schema.sql is fresh-DB DDL (plain CREATE TABLE, run once by init_schema). An EXISTING
-- Postgres deployment must run THIS idempotent statement to add the column without a
-- full rebuild. Safe to run more than once.
--
--   psql "$MYNDAIX_DSN" -f src/runtime/ledger/migrations/0001_add_job_context.sql

ALTER TABLE job ADD COLUMN IF NOT EXISTS context jsonb NOT NULL DEFAULT '{}';
