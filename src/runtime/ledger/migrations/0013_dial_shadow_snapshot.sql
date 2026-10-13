-- Migration 0013: dial_shadow_snapshot — the shadow dial's ONLY write surface
-- (docs/shadow-dial-design.md v0.6, PR-A). MEASURE ONLY.
--
-- The shadow dial (`mxr dial-shadow`) is a READ verb over finding_current_human; its opt-in
-- `--snapshot` appends the current per-(rule_tag × reviewer_family) classification HERE so the
-- §4 eval loop can test the prediction against labels that land later. THIS TABLE IS NEVER READ
-- BY REVIEW / PROMPT / GATE CODE (design m1) — the only reader is `mxr dial-shadow --eval`.
-- Nothing acts on it; acting (PR-B) is not designed until a class is arming-eligible by §4.
--
-- Self-contained reproducibility (M5 + M5-r2): every row carries the FULL policy context it was
-- classified under — classifier params AND eval params — so a future eval verdict is reproducible
-- from the snapshot alone, never from live config. week_span_rule is stored as text for the same
-- reason (the stability rule is data, not code, at eval time).
--
-- Append-only by convention (like finding_outcome): NEVER UPDATE/DELETE.
-- Idempotent (IF NOT EXISTS); re-run on every serve boot.

CREATE TABLE IF NOT EXISTS dial_shadow_snapshot (
    id                        bigserial PRIMARY KEY,
    captured_at               timestamptz NOT NULL DEFAULT now(),
    data_cutoff_seq           bigint      NOT NULL,  -- max finding_outcome.seq at capture: the "labels since" boundary
    rule_tag                  text        NOT NULL,
    reviewer_family           text        NOT NULL CHECK (reviewer_family IN ('kilabz','oracle')),
    confirmed_real            integer     NOT NULL CHECK (confirmed_real >= 0),
    dismissed_fp              integer     NOT NULL CHECK (dismissed_fp >= 0),
    n                         integer     NOT NULL CHECK (n >= 0),
    precision                 numeric,               -- NULL when n = 0 (never a fake 0.0)
    wilson_lo                 numeric,
    wilson_hi                 numeric,
    n_recent                  integer     NOT NULL CHECK (n_recent >= 0),
    wilson_recent_lo          numeric,
    wilson_recent_hi          numeric,
    distinct_refs             integer     NOT NULL CHECK (distinct_refs >= 0),
    distinct_plays            integer     NOT NULL CHECK (distinct_plays >= 0),
    would_say                 text        NOT NULL CHECK (would_say IN
                                  ('insufficient','would-suppress','would-trust','hold')),
    suppressible              boolean     NOT NULL,  -- always false while the code-owned SUPPRESSIBLE set is empty
    -- classifier params (the thresholds THIS row was classified under)
    floor                     numeric     NOT NULL,
    ceiling                   numeric     NOT NULL,
    min_n                     integer     NOT NULL,
    min_refs                  integer     NOT NULL,
    min_plays                 integer     NOT NULL,
    min_fp                    integer     NOT NULL,
    recency_n                 integer     NOT NULL,
    z                         numeric     NOT NULL,
    -- eval params (M5-r2 — the §4 gate reads THESE, not live config)
    stable_snaps              integer     NOT NULL,
    eval_min_n                integer     NOT NULL,
    eval_agree                numeric     NOT NULL,
    week_span_rule            text        NOT NULL,
    -- versions
    suppressible_set_version  text        NOT NULL,
    taxonomy_version          text        NOT NULL
);

-- --eval groups by cell and orders by capture time; this is the only access path.
CREATE INDEX IF NOT EXISTS dial_shadow_snapshot_cell
    ON dial_shadow_snapshot (rule_tag, reviewer_family, captured_at);
