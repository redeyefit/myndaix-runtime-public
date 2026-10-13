-- Migration 0006: skill PK -> composite (repo_scope, name).
--
-- 0005 created `skill` with a global `name PRIMARY KEY`. But a skill name is unique PER REPO
-- (it is the skills/<name>/ directory), and selection is scoped by repo_scope. A global name PK
-- lets two watched repos that each ship a skills/<same-name>/SKILL.md collide on index_skills'
-- UPSERT — one repo silently steals/suppresses the other's skill (cross-family review: kilabz
-- HIGH + oracle CRITICAL). This migration corrects the PK to (repo_scope, name).
--
-- APPEND-ONLY: a shipped migration's CREATE is never edited in place — migrate() re-runs every
-- *.sql on each boot via CREATE TABLE IF NOT EXISTS, which does NOT alter an existing table, so
-- an in-place 0005 edit would be invisible to any DB that already ran 0005 (it would keep the
-- single-column PK, and index_skills' ON CONFLICT (repo_scope, name) would then raise 42P10
-- forever — a fail-OPEN: the indexer never blocks, so a clobbered cross-repo row stays
-- selectable). This file is the fix for those DBs AND for fresh ones.
--
-- IDEMPOTENT + re-run-every-boot-SAFE: a bare `DROP CONSTRAINT skill_pkey; ADD PRIMARY KEY ...`
-- would fail (or briefly drop the live PK) on every subsequent boot. This guarded DO-block only
-- acts when the skill PK is still a SINGLE column, and is a no-op once composite. Data-safe: the
-- old PK made `name` globally unique, so (repo_scope, name) is unique too.
DO $$
DECLARE pk text;
BEGIN
    SELECT c.conname INTO pk
      FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
     WHERE t.relname = 'skill' AND c.contype = 'p' AND array_length(c.conkey, 1) = 1;
    IF pk IS NOT NULL THEN
        EXECUTE format('ALTER TABLE skill DROP CONSTRAINT %I', pk);
        ALTER TABLE skill ADD PRIMARY KEY (repo_scope, name);
    END IF;
END $$;
