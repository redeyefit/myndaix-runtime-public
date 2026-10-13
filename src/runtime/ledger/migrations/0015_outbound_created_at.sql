-- outbound.created_at — deterministic "latest reply" ordering for `mxr get --reply`
-- (phone-surface review r1 M-9: json_agg without ORDER BY made bodies[-1] arbitrary,
-- and outbound ids are UUID4 — no insertion-order correlation). Existing rows backfill
-- to now(): pre-migration order across old rows is unknowable, and every job in the
-- current flows has at most one outbound row, so the backfill is inert in practice.
-- Idempotent: serve re-runs all migrations on every boot.
ALTER TABLE outbound ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
-- The reply read path aggregates per job ORDER BY (created_at, id); this index serves it and
-- gives the migration a to_regclass-checkable object (substrate migration_head verification
-- can't see columns, only relations).
CREATE INDEX IF NOT EXISTS outbound_created_at_idx ON outbound (job_id, created_at);
