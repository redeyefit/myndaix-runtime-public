-- Migration 0005: skill / skill_use — the "+learning" rung (review skills) cache + audit.
--
-- Idempotent (CREATE ... IF NOT EXISTS); re-run on every serve() boot behind the
-- migrate() advisory lock. Inert until runtime.controller (indexer) + runtime.skillselect run.
--
-- DO NOT apply this file ALONE: it creates the ORIGINAL single-column `name` PK; the composite
-- (repo_scope, name) PK is applied by the APPEND-ONLY follow-up 0006_skill_pk.sql. Always run the
-- WHOLE migration path (serve() auto-migrates on boot; the controller self-migrates each tick) —
-- never a single migration file by hand.
--
-- WHY (learning-rung DESIGN v0.3, governing sections): a review skill is a
-- skills/<name>/SKILL.md promoted ONLY by a human PR-merge under branch protection. On-disk
-- markdown + git history stay the human-facing source, but the BODY lives HERE (Postgres is
-- the runtime read path): the controller/automerge review owned refs / base..tip OBJECTS, not
-- the worktree, so a selector must NOT rehash a possibly-stale/dirty/unmerged SKILL.md off
-- disk (codex MAJOR) — the indexer reads the body from the trusted merged ref and stores it
-- here; skillselect reads from Postgres. `provenance` is stamped server-side at the human arm,
-- never copied from the artifact (an agent cannot self-promote). Selection is per-repo and is
-- injected ONLY into push/controller reviews, NEVER a merge-gating (PLAY_GATE) review.
-- Everything is reversible (archive-not-delete): prune flips `state`, never deletes a row/file.
--   state: active -> stale -> archived  (by inactivity; reactivation = human re-arm only)
-- NOTE: this CREATE is the ORIGINAL (single-column `name` PK). The PK is corrected to the
-- composite (repo_scope, name) by the APPEND-ONLY migration 0006_skill_pk.sql — never edit a
-- shipped migration's CREATE in place (a DB that already ran this file would keep the old PK,
-- since migrate() re-runs via CREATE TABLE IF NOT EXISTS). schema.sql (fresh DBs) carries the
-- final composite PK directly. See 0006 for the why (cross-repo same-name collision).
CREATE TABLE IF NOT EXISTS skill (
    name         text PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9._-]*$'),
    description  text NOT NULL CHECK (length(description) <= 60),
    body         text NOT NULL CHECK (length(body) <= 2048),
    body_sha     text NOT NULL,
    content_sha  text NOT NULL,
    repo_scope   text NOT NULL,
    path_trigger text NOT NULL,
    provenance   text NOT NULL DEFAULT 'promoted' CHECK (provenance IN ('promoted')),
    state        text NOT NULL DEFAULT 'active'   CHECK (state IN ('active','stale','archived')),
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit: reconstructs every skill's influence on every review. No FK to
-- skill(name) — the audit must survive an archive/rename (mirrors dead_letter.source_id).
CREATE TABLE IF NOT EXISTS skill_use (
    id          uuid PRIMARY KEY,
    review_play text NOT NULL,
    skill_name  text NOT NULL,
    body_sha    text NOT NULL,
    repo_scope  text NOT NULL,
    used_at     timestamptz NOT NULL DEFAULT now()
);
