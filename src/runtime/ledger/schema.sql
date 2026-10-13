-- MyndAIX Team Runtime - ledger (DESIGN.md C2). The durable state machine that
-- replaces file-IPC. The Command API is the ONLY writer. State transitions here
-- are the only legal moves; row-locking + leases give crash recovery and
-- exactly-once-ish behavior.

CREATE TABLE inbound_event (
    id          uuid PRIMARY KEY,
    transport   text NOT NULL,
    envelope    jsonb NOT NULL,                 -- the transport envelope (C3)
    body        text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    dedupe_key  text NOT NULL UNIQUE            -- exactly-once ingest
);

CREATE TABLE job (
    id                  uuid PRIMARY KEY,
    parent_id           uuid REFERENCES job(id),
    root_id             uuid NOT NULL,          -- for cost_budget / chain_ttl over a tree
    depth               int  NOT NULL DEFAULT 0,-- for max_depth admission limit
    created_by          text NOT NULL,          -- agent_id | 'human' | inbound_event id
    inbound_event_id    uuid REFERENCES inbound_event(id),  -- originating transport event; lets outbound resolve transport+reply_target
    to_agent            text NOT NULL,
    body                text NOT NULL,
    context             jsonb NOT NULL DEFAULT '{}',  -- free-form per-job input (e.g. {"image_url": ...}); existing DBs: migrations/0001_add_job_context.sql
    capability_required text,
    priority            int  NOT NULL DEFAULT 0,
    status              text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','leased','running','done','failed','dead')),  -- legal states enforced at the DB
    -- workspace (workspace-actors); base_ref = a prior job's artifact_ref enables chaining
    repo_id       text,
    base_ref      text,
    base_sha      text,
    worktree_path text,
    artifact_ref  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_queued_idx ON job (priority DESC, created_at) WHERE status = 'queued';
-- exactly-once dispatch: at most ONE job per originating inbound event (idempotent submit)
CREATE UNIQUE INDEX job_one_per_inbound ON job (inbound_event_id) WHERE inbound_event_id IS NOT NULL;

CREATE TABLE attempt (
    id               uuid PRIMARY KEY,
    job_id           uuid NOT NULL REFERENCES job(id),
    worker_id        text NOT NULL,
    lease_expires_at timestamptz NOT NULL,      -- expiry -> reclaim (crashed worker)
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    status           text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','ok','failed')),
    result           jsonb,
    error_class      text                       -- retryable|terminal|needs_human
);
CREATE INDEX attempt_lease_idx ON attempt (lease_expires_at) WHERE status = 'open';
-- at most ONE open attempt per job: the hard backstop behind lease_job's CAS (no double-lease)
CREATE UNIQUE INDEX attempt_one_open_per_job ON attempt (job_id) WHERE status = 'open';
-- per-repo concurrency cap (phase2 §4): the HARD count = count(*) open attempts joined to a repo's jobs
CREATE INDEX attempt_status_job_idx ON attempt (status, job_id);

-- per-repo concurrency cap counter (phase2 §4). `active` is a SOFT filter (perf only);
-- the COUNT(*) of open attempts under this row's FOR UPDATE lock at lease time is the HARD
-- cap authority, so counter drift can never breach the cap. Lazily seeded by lease_job
-- (INSERT ... ON CONFLICT DO NOTHING), self-healed by the reconciler. CHECK is a guard:
-- decrements use GREATEST(active-1,0) so it can never fire from correct code.
CREATE TABLE repo_concurrency (
    repo_id text PRIMARY KEY,
    active  int  NOT NULL DEFAULT 0 CHECK (active >= 0)
);

-- append-only progress side channel; NOT part of the hot state machine
CREATE TABLE attempt_log (
    id         bigserial PRIMARY KEY,
    attempt_id uuid NOT NULL REFERENCES attempt(id),
    ts         timestamptz NOT NULL DEFAULT now(),
    stream     text NOT NULL,                   -- stdout|stderr|heartbeat
    chunk      text NOT NULL
);

-- outbox pattern -> reliable, deduped delivery (decoupled from job completion)
CREATE TABLE outbound (
    id              uuid PRIMARY KEY,
    job_id          uuid NOT NULL REFERENCES job(id),
    transport       text NOT NULL,
    reply_target    text NOT NULL,              -- from the inbound envelope
    body            text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','leased','sent','failed')),
    provider_msg_id text UNIQUE,                -- dedupe delivery (exactly-once send)
    tries           int  NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()  -- deterministic latest-reply order (0015)
);
CREATE INDEX outbound_pending_idx ON outbound (status) WHERE status = 'pending';
CREATE INDEX outbound_created_at_idx ON outbound (job_id, created_at);  -- reply ordering (0015)

CREATE TABLE dead_letter (
    id         uuid PRIMARY KEY,
    source_id  uuid NOT NULL,                   -- the job/outbound that exhausted retries
    reason     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- controller-loop ("the brain") cursor — the proactive review scheduler's only state
-- (DESIGN v0.2 §2). A level-triggered reconciler tracks, per (repo, ref): the last SHA
-- whose review DELIVERED (reviewed_sha, the cursor) and whether one is in flight
-- (pending_sha). baseline_sha is the high-water mark seeded at first sight so a fresh
-- repo is NOT whole-tree-reviewed. Existing DBs: migrations/0003_review_cursor.sql.
CREATE TABLE review_cursor (
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

-- docs-only PR auto-merge gate decision log (automerge DESIGN v0.3 §4). One row per
-- (repo, PR, head sha) records the terminal decision so the hourly gate never re-reviews
-- the same head; a new push = a new head = a new row. Existing DBs: migrations/0004_automerge_seen.sql.
CREATE TABLE automerge_seen (
    repo_id    text NOT NULL,
    pr_number  int  NOT NULL CHECK (pr_number > 0),
    head_sha   text NOT NULL,
    decision   text NOT NULL
        CHECK (decision IN ('merged','needs_fix','skipped','error')),
    reason     text,
    decided_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, pr_number, head_sha)
);

-- +learning rung (review skills): cache + audit. Mirrors migrations/0005_skill.sql (fresh DBs
-- get this; existing prod gets the migration on serve boot). The BODY lives here (the indexer
-- reads it from a trusted merged ref); selection never rehashes disk.
-- PK is (repo_scope, name): a skill name is unique PER REPO, not globally (kilabz+oracle:
-- a global name PK lets two repos shipping the same skill slug collide on UPSERT).
CREATE TABLE skill (
    name         text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]*$'),
    description  text NOT NULL CHECK (length(description) <= 60),
    body         text NOT NULL CHECK (length(body) <= 2048),
    body_sha     text NOT NULL,
    content_sha  text NOT NULL,
    repo_scope   text NOT NULL,
    path_trigger text NOT NULL,
    provenance   text NOT NULL DEFAULT 'promoted' CHECK (provenance IN ('promoted')),
    state        text NOT NULL DEFAULT 'active'   CHECK (state IN ('active','stale','archived')),
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_scope, name)
);

CREATE TABLE skill_use (
    id          uuid PRIMARY KEY,
    review_play text NOT NULL,
    skill_name  text NOT NULL,
    body_sha    text NOT NULL,
    repo_scope  text NOT NULL,
    used_at     timestamptz NOT NULL DEFAULT now()
);

-- auto-capture rung (the proposer): v0.4 multi-signal recurrence + S6 state machine. Mirrors
-- migrations/0007_capture_candidate.sql (keep the two in lockstep). A (repo, allowlisted rule_tag)
-- class that recurs with enough INDEPENDENT signal gets ONE proposed skill PR; the proposer never
-- promotes. NO LLM in the trigger; the glob is SECONDARY locality (-> the proposed path_trigger).
CREATE TABLE capture_candidate (
    fingerprint   text PRIMARY KEY,                 -- sha256(repo_scope \0 rule_tag): the class key
    repo_scope    text NOT NULL,
    rule_tag      text NOT NULL,                     -- allowlisted taxonomy tag (recurrence class)
    path_glob     text,                              -- SECONDARY locality -> proposed path_trigger
    state         text NOT NULL DEFAULT 'new'        -- S6 two-phase idempotent state machine
                  CHECK (state IN ('new','accumulating','ready','proposing',
                                   'proposed','promoted','declined','stale','error')),
    branch        text,                              -- skill/auto/<slug> (pinned at ready->proposing)
    draft_sha     text,                              -- sha256(rendered SKILL.md) (S6 idempotency)
    pr_number     int,
    decline_count int NOT NULL DEFAULT 0 CHECK (decline_count >= 0),  -- repropose floor (S8)
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    proposed_at   timestamptz                        -- when the PR opened (TTL anti-wedge, S8)
);

CREATE TABLE capture_occurrence (
    fingerprint  text NOT NULL REFERENCES capture_candidate(fingerprint) ON DELETE CASCADE,
    commit_sha   text NOT NULL,
    event_id     text NOT NULL,                      -- the review/push event (run id): temporal indep.
    author       text NOT NULL,
    path_glob    text,
    seen_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fingerprint, commit_sha)            -- one occurrence per (class, commit)
);

CREATE INDEX idx_capture_candidate_state ON capture_candidate (state);

-- outcomes-ledger rung (the per-finding OUTCOME LABEL): append-only event log + computed views on
-- the existing spine (v0.3, cross-family reviewed). Mirrors migrations/0008_finding_outcome.sql
-- (keep the two in lockstep). Every delivered push review records what HAPPENED to each finding —
-- applied_fixed (the hashed line is gone at the next review), dismissed_* (a human label), expired
-- (TTL sweep) — so per-class precision becomes measurable. Identity is a PATH-SCOPED line-hash
-- (finding_key = sha256(repo_id \0 rule_tag \0 path \0 line_hash)) so cross-file identical lines
-- never collide. NO LLM anywhere; v1 COLLECTS ONLY (no dial acts on the data). NEVER UPDATE/DELETE —
-- current state is the finding_current view (human-terminal precedence, else latest-by-seq).
CREATE TABLE finding_outcome (
    seq             bigserial,       -- monotonic EVENT ORDER: 'latest row' is by seq, not created_at
    id              uuid PRIMARY KEY,
    finding_key     text NOT NULL,   -- sha256(repo_id \0 rule_tag \0 path \0 line_hash): PATH in key
    repo_id         text NOT NULL,
    ref             text NOT NULL,   -- the reviewed ref the finding was raised on (EXACT close scope)
    rule_tag        text NOT NULL,   -- allowlisted capture taxonomy (shared, single source of truth)
    reviewer_family text NOT NULL CHECK (reviewer_family IN ('kilabz','oracle')),
    path            text NOT NULL,   -- validated ∈ the reviewed diff's changed-file set
    line_hash       text NOT NULL,   -- sha256 of the normalized flagged-line CONTENT at tip_sha
    source_event    text NOT NULL,   -- 'review:<play>' | 'human:<finding_key12>:<kind>' | 'sweep:<utcday>'
    tip_sha         text NOT NULL,   -- the sha the line-hash was computed/checked at (NOT base_sha)
    outcome         text NOT NULL CHECK (outcome IN
                     ('open','applied_fixed','dismissed_false_positive',
                      'dismissed_wontfix','reverted','expired')),  -- 'reverted' reserved, no v1 writer
    outcome_source  text NOT NULL CHECK (outcome_source IN
                     ('review_raised','auto_fix_landed','auto_git_revert','human_dismiss','ttl_sweep')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- idempotency: one row per (finding_key, reviewer_family, outcome, source_event) — a re-run is a no-op.
CREATE UNIQUE INDEX finding_outcome_event_once
    ON finding_outcome (finding_key, reviewer_family, outcome, source_event);
CREATE INDEX finding_outcome_key_idx  ON finding_outcome (finding_key, created_at DESC);
CREATE INDEX finding_outcome_open_idx ON finding_outcome (repo_id, path) WHERE outcome = 'open';

-- current state per (finding_key, reviewer_family): ANY human row (human_dismiss OR human_confirm)
-- is TERMINAL (outranks any later machine row); among human rows the latest wins (a correction);
-- else latest-by-seq. (Both-source precedence = migration 0012.)
CREATE OR REPLACE VIEW finding_current AS
SELECT DISTINCT ON (finding_key, reviewer_family)
       finding_key, reviewer_family, repo_id, ref, rule_tag, path, line_hash,
       outcome, outcome_source, tip_sha, source_event, created_at, seq
  FROM finding_outcome
 ORDER BY finding_key, reviewer_family,
          (outcome_source IN ('human_dismiss', 'human_confirm')) DESC,  -- human-terminal FIRST
          seq DESC;

-- precision per (rule_tag × reviewer_family), over ALL history (no time window in v1). Reads current
-- state, never raw event counts. precision = applied_fixed / (applied_fixed + dismissed_false_positive).
CREATE OR REPLACE VIEW finding_precision AS
SELECT rule_tag,
       reviewer_family,
       count(*) FILTER (WHERE outcome = 'applied_fixed')              AS applied_fixed,
       count(*) FILTER (WHERE outcome = 'dismissed_false_positive')   AS dismissed_false_positive,
       count(*)                                                       AS volume,
       count(*) FILTER (WHERE outcome = 'applied_fixed')::numeric
         / NULLIF(count(*) FILTER (WHERE outcome IN
             ('applied_fixed','dismissed_false_positive')), 0)        AS precision
  FROM finding_current
 GROUP BY rule_tag, reviewer_family;

-- ---- knowledge_doc: the curator rung's derived FTS index over a corpus folder (0009) ----------
-- Append-only events + computed views (finding_outcome's discipline); files are the source of
-- truth, this table is a derived, REBUILDABLE index (rebuild = tombstone sweep + re-ingest, never
-- TRUNCATE). No (scope,path,sha) unique index ON PURPOSE (it would ghost a restore-after-archive
-- of identical content); idempotency = compare-current-before-insert under a per-scope advisory
-- lock. Design: docs/curator-design.md v0.4.
CREATE TABLE knowledge_doc (
    seq         bigserial,        -- monotonic event order ('latest' is by seq, not created_at)
    id          uuid PRIMARY KEY, -- deterministic tie-break for the current view's total order
    scope       text NOT NULL,    -- corpus id; static allowlist in code (unknown = hard error)
    path        text NOT NULL,    -- relative to the corpus root, traversal-checked at ingest
    title       text NOT NULL DEFAULT '',
    tags        text NOT NULL DEFAULT '',
    doc_date    date,             -- filename YYYY-MM-DD prefix wins, frontmatter date: fallback
    body        text NOT NULL,    -- decoded errors='replace', NULs stripped, capped ~900KB
    content_sha text NOT NULL,    -- sha256 of raw bytes; 'absent' marks a tombstone
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    lossy       boolean NOT NULL DEFAULT false,  -- decode/truncation loss — surfaced in recall
    tsv tsvector GENERATED ALWAYS AS (            -- 2-arg to_tsvector: IMMUTABLE (generated col)
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(tags,  '')), 'B') ||
        setweight(to_tsvector('english', coalesce(body,  '')), 'D')) STORED,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX knowledge_doc_tsv_idx ON knowledge_doc USING GIN (tsv);
CREATE INDEX knowledge_doc_cur_idx ON knowledge_doc (scope, path, seq DESC);

CREATE OR REPLACE VIEW knowledge_doc_current AS
SELECT DISTINCT ON (scope, path)
       seq, id, scope, path, title, tags, doc_date, body, content_sha, status, lossy, tsv, created_at
  FROM knowledge_doc
 ORDER BY scope, path, seq DESC, id DESC;

CREATE OR REPLACE VIEW knowledge_doc_active AS
SELECT * FROM knowledge_doc_current WHERE status = 'active';

-- per-scope commit generation (0016): the walk fence is gen + MAX(seq). gen is bumped under the
-- per-scope advisory lock on EVERY accepted sync/rebuild — including true no-ops, which the old
-- MAX(seq) fence missed (a stale walk could pass the check and resurrect a just-deleted doc);
-- the MAX(seq) term keeps row-writing commits from pre-0016 code visible during a mixed-deploy
-- window. Both terms are monotonic, so a stamped fence can never recur.
CREATE TABLE knowledge_scope_gen (
    scope text PRIMARY KEY,
    gen   bigint NOT NULL DEFAULT 0
);
