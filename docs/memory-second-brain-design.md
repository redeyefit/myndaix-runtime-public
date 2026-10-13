# Memory / "Second Brain" — DESIGN ANALYSIS (v0.1)

_Task: design a pgvector+tsvector memory table in the runtime's Postgres (the "second brain" that
replaces `~/.myndaix/memory.db` sqlite + knowledge files). **Conviction-first outcome: DEFER the
pgvector build — it is a pattern this codebase has already rejected three times, and nothing
observed yet justifies overturning that.** This doc is the reasoned analysis + the concrete
trigger that WOULD justify it. A well-argued "don't build yet" is the honest design deliverable,
not a speculative 300-line build spec.  Status: recommendation, for Jefe's call._

## 1. The ask, and why I'm pushing back before designing it

The Factory-OS map lists "second brain (pgvector + tsvector memory table in same Postgres)" as an
unbuilt gap. But **a gap on a map is not a need.** Before designing an embeddings store, the
first-principles question: what concrete problem does semantic memory solve that the runtime's
EXISTING, deliberately-deterministic learning substrate does not? If I can't name one, building it
is exactly the "build ahead of need on theory" this team has killed repeatedly (keep-warm agent,
worker-watchdog, Gemini API).

## 2. This codebase has ALREADY rejected vector retrieval — three times, on the record

- `docs/learning-rung-design.md:54` — "**No embeddings, no ranking model, no routing engine for
  ~8 rows.** … match `path_trigger` … in Python `fnmatch`, `LIMIT 2`."
- `docs/learning-rung-design.md:111` — "no semantic/embedding retrieval (SQL+`fnmatch`, LIMIT 2)".
- `optimal-team-brief` §3 — self-learning verdict: "an outcomes-ledger feeding DETERMINISTIC
  dials. NOT fine-tuning / RLAIF / DSPy… **Reject fine-tuning/embeddings/vector-store/dashboard**."
- `docs/auto-capture-research.md` — "REJECT LLM-as-enforcement / ML rule-mining."

Overturning a position affirmed in three separate cross-family-reviewed briefs needs new
evidence, not a map entry. There is none yet.

## 3. The learning substrate that ALREADY exists (and what each covers)

The runtime's "memory" is not absent — it is **decomposed into deterministic, purpose-built
rungs**, each cheaper and more auditable than a general embeddings store:

| Need | Served by | Mechanism |
|---|---|---|
| "we keep flagging this class of issue → codify a lesson" | **skills rung** (LIVE) | promoted `SKILL.md` in Postgres, injected as fenced reference into reviewer prompts, human-gated |
| "which issues recur across reviews" | **capture rung** (LIVE, observe-only) | `capture_candidate`/`capture_occurrence`, rule-tag recurrence |
| "what happened to each finding (fixed/dismissed/reverted)" | **outcomes ledger** (designed, PR #47) | `finding_outcome` append-only + SQL precision dials |
| "durable append-only facts/notes" | `knowledge.jsonl` + memory.db | append-only log, grep-able |
| "how the operator wants me to work" | Mack's file memory (`MEMORY.md`) | the human-agent memory, outside the runtime |

The learning LOOP (recur → codify → measure → suppress/promote) is fully covered by
skills+capture+outcomes, all **deterministic SQL + human gate**, no embedding cost, no recall-poison
surface. A pgvector store would sit *beside* these solving a problem none of them has surfaced.

## 4. What a pgvector second-brain would actually cost (the surface we'd be buying)

Not free — and the costs land squarely on things this system optimizes against:
- **A new dependency + extension** (pgvector) on the spine that currently needs only stock Postgres.
- **An embedding-cost + provider surface** — every write and every recall query embeds text
  (metered API or a local model to host), the exact metered-cost coupling the team rejected agy-API
  over.
- **Recall-injection = prompt-injection.** Semantic recall pulls the "most similar" text into a
  prompt; an attacker who can write ONE memory row (via any untrusted-diff-derived path) gets it
  retrieved into future agent contexts by crafting similarity — a poisoning vector with a much
  larger blast radius than the fenced, human-gated skills path. Defending it well is a whole
  security project.
- **Nondeterminism** in a system whose entire safety model is "deterministic mechanical gates, LLM
  never in the loop." Similarity ranking is model-versioned and drifts.

## 5. The concrete TRIGGER to revisit (write it down, don't build to it)

Build a memory/recall store ONLY when a **specific recall-miss** is observed that the deterministic
rungs structurally cannot serve — e.g. **all** of:
1. An agent demonstrably needed a fact/decision from a PAST task that was not a review-lesson
   (skills), not a recurrence (capture), not a finding-outcome, and not findable by a plain
   substring/`tsvector` search of `knowledge.jsonl`; **and**
2. this happened ≥ N times on real work (not hypothesized), logged; **and**
3. the miss is semantic (paraphrase/synonym), so exact/full-text search genuinely can't reach it.

If (1)+(2) hold but (3) does NOT, the answer is the **cheap half only**: move `knowledge.jsonl`
into a Postgres `knowledge` table with a **`tsvector` full-text index** (stock Postgres, no
extension, no embeddings, deterministic) and a `record-knowledge`/`recall-knowledge` verb pair with
the SAME fenced-untrusted-injection + scoping discipline as skills. That is the 80%-for-1% version
and the natural first step — pgvector is only ever the LAST resort, gated on (3).

## 6. Recommended design (IF/WHEN the trigger fires) — tsvector first, pgvector maybe-never

- **`knowledge` table** (append-only, mirrors the ledger discipline): `{id, scope (repo|global),
  source (agent|human|task), kind (fact|decision|note), body, body_tsv tsvector GENERATED, created_by,
  created_at}`. GIN index on `body_tsv`.
- **Write**: a `record-knowledge` command verb; untrusted-derived writes are sanitized (capture's
  `sanitize_field`), scoped, and **never** auto-recalled into a merge/gate prompt (like skills).
- **Recall**: deterministic `ts_rank` full-text, `LIMIT k`, injected as a FENCED UNTRUSTED reference
  block into reviewer/judge prompts — never an instruction, weighable against the task (skills'
  exact model).
- **Scoping fail-closed**: a recall missing a scope returns nothing (never cross-repo leak).
- **pgvector**: only if trigger (3) proves semantic misses that `tsvector` can't serve, and then as
  an ADDITIVE ranking signal behind the same fence, cross-family reviewed as its own rung.

## 7. Verdict

**Do not build the pgvector second-brain now.** The learning loop is already served by the
deterministic skills+capture+outcomes triad; embeddings/vector-store is a thrice-rejected pattern
whose costs (dependency, metered embeddings, recall-injection blast radius, nondeterminism) hit
this system's core principles; and no recall-miss has been observed to justify it. Bank the
`tsvector` `knowledge`-table upgrade as the cheap first step and **wire the trigger** (§5) so the
decision is made on evidence, not on a map. If Jefe wants the phone/knowledge convenience sooner,
the tsvector table (no extension, no embeddings) is a small, safe, in-philosophy build; pgvector is
not — yet.
