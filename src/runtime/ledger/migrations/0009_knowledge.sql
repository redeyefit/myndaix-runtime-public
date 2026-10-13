-- Migration 0009: knowledge_doc — the curator rung's derived FTS index over a corpus folder
-- (docs/curator-design.md v0.4). Append-only EVENT rows + computed current/active views, mirroring
-- finding_outcome's discipline; files on disk are the SOURCE OF TRUTH and this table is a derived,
-- rebuildable index (rebuild = archive-tombstone sweep + re-ingest — all appends, never TRUNCATE).
-- Idempotent (IF NOT EXISTS / CREATE OR REPLACE VIEW); re-run on every serve() boot under
-- migrate()'s advisory lock.
--
-- WHY: the research corpus rots without retrieval ("I gather reports and don't reopen them") —
-- recall-before-re-research needs a deterministic full-text surface. Storage per the
-- memory-second-brain analysis §6: stock-Postgres tsvector (weighted title/tags/body), GIN, NO
-- pgvector/embeddings (rejected 3x on record; the LLM reading top-k hits IS the semantic layer).

CREATE TABLE IF NOT EXISTS knowledge_doc (
    seq         bigserial,        -- monotonic EVENT ORDER: 'latest row' is by seq, not created_at
    id          uuid PRIMARY KEY, -- tie-break for a stable total order in the current view
    scope       text NOT NULL,    -- corpus id ('research'); resolved via a STATIC allowlist in code,
                                  -- never derived from input (unknown scope = hard error, all verbs)
    path        text NOT NULL,    -- relative to the corpus root, traversal-checked at ingest
    title       text NOT NULL DEFAULT '',
    tags        text NOT NULL DEFAULT '',
    doc_date    date,             -- YYYY-MM-DD filename prefix (wins) else frontmatter date:; NULL
                                  -- if unparseable. Citation metadata — disagreement WARNs at ingest.
    body        text NOT NULL,    -- UTF-8 decoded errors='replace', NULs stripped, capped ~900KB
    content_sha text NOT NULL,    -- sha256 of the RAW file bytes; 'absent' marks a tombstone row
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    lossy       boolean NOT NULL DEFAULT false,  -- decode-replaced / NUL-stripped / truncated body:
                                  -- surfaced in recall so corrupted text is never cited as clean
    -- 2-arg to_tsvector (explicit 'english') is REQUIRED: the 1-arg form is only STABLE (tracks
    -- default_text_search_config) and a GENERATED column rejects non-IMMUTABLE expressions.
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(tags,  '')), 'B') ||
        setweight(to_tsvector('english', coalesce(body,  '')), 'D')) STORED,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- NO (scope,path,content_sha) unique index ON PURPOSE: it would swallow a restore-after-archive of
-- identical content (the historic active row conflicts -> DO NOTHING -> a ghost tombstone stays
-- current). Idempotency is compare-current-before-insert under the per-scope advisory lock instead.
CREATE INDEX IF NOT EXISTS knowledge_doc_tsv_idx  ON knowledge_doc USING GIN (tsv);
CREATE INDEX IF NOT EXISTS knowledge_doc_cur_idx  ON knowledge_doc (scope, path, seq DESC);

-- CURRENT state per (scope, path): latest event wins by seq, uuid id as the deterministic
-- tie-break (a stable total order — bigserial is unique in practice, not declared).
CREATE OR REPLACE VIEW knowledge_doc_current AS
SELECT DISTINCT ON (scope, path)
       seq, id, scope, path, title, tags, doc_date, body, content_sha, status, lossy, tsv, created_at
  FROM knowledge_doc
 ORDER BY scope, path, seq DESC, id DESC;

-- ACTIVE = what recall queries: tombstoned (archived) docs are invisible (a deleted file must
-- never be cited as knowledge).
CREATE OR REPLACE VIEW knowledge_doc_active AS
SELECT * FROM knowledge_doc_current WHERE status = 'active';
