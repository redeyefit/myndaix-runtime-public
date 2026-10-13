-- Migration 0016: knowledge_scope_gen — the per-scope commit-generation counter. The walk fence
-- is now gen + MAX(seq) (see _KNOWLEDGE_GEN_SQL in postgres_store.py).
--
-- WHY (review 20260910200253 P2, kilabz, confirmed): the fence was MAX(seq) over knowledge_doc,
-- which only advances when a row is INSERTED. An accepted TRUE NO-OP sync moves nothing — e.g.
-- a file created then deleted before either walker commits needs no tombstone because it was
-- never indexed — so a stale walker holding the older fence still passed the equality check and
-- committed the deleted file back to active. New code bumps gen under the per-scope advisory
-- lock on EVERY accepted sync/rebuild, including true no-ops; conflict rejects return before
-- the bump so a rejected walk still mutates nothing.
--
-- WHY NO SEED (r1 kilabz P2, the mixed-deploy window): the fence is the SUM gen + MAX(seq), so
-- a pre-0016 writer still running through a rolling cutover — which advances seq but never
-- touches gen — still moves the fence new walkers check. With gen starting at 0 the fence VALUE
-- is numerically continuous with the old MAX(seq) fence across the upgrade: an old-code stamped
-- fence compares equal iff nothing committed (accept), and any accepted commit from either
-- vintage strictly increases the sum (conflict). Both terms are monotonic (knowledge_doc is
-- append-only), so a stamped fence can never recur.
--
-- Idempotent (IF NOT EXISTS) — re-run on every serve() boot under migrate()'s advisory lock.
CREATE TABLE IF NOT EXISTS knowledge_scope_gen (
    scope text PRIMARY KEY,
    gen   bigint NOT NULL DEFAULT 0
);
