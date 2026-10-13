# Curator v1 — DESIGN (research/ → the first folder-agent)

_Status: **BUILT + MERGED (v1.0, read-only), 2026-07-05.** Design converged over 3 cross-family
DESIGN rounds (v0.1→v0.4); code converged over 3 cross-family CODE-review rounds (kilabz+oracle).
Shipped READ-ONLY: Write is evidence-gated on the enforcement ship gate
(tests/test_curator_enforcement.py), which is credit-blocked at build time — flip to Write via
MYNDAIX_CURATOR_WRITE=1 + restoring Write/Edit to the registry argv once the gate passes.
Recon: workflow `wf_357e9782`. Design + code review history at the bottom._

## ⚠️ BUILD FINDING — gate run 2026-07-06 (needs a fix before the AGENT path works)

Running the enforcement gate (on a working Max token — see the auth note) uncovered that the
**curator agent path is non-functional as shipped**, and overturns the design's core enforcement
assumption. Empirical results (real `claude` CLI, filesystem-observable probes):

| config | in-tree read | in-tree write | out-of-tree write |
|---|---|---|---|
| one-string allowedTools (SHIPPED) + staged settings.json | blocked | blocked | denied |
| separate-arg allowedTools + staged settings.json | blocked | blocked | denied |
| **separate-arg allowedTools, NO settings.json** | **works** | **works** | **denied** |

Two bugs + one wrong assumption:
1. **Tool flags as a single space-joined string do NOT work** (`--allowedTools "Read Glob Grep"`
   is parsed as one tool name matching nothing). They are variadic — need separate argv elements
   (`--allowedTools Read Glob Grep`). As shipped the agent gets NO tools — even read-only
   QUERY/LINT would fail. (The deterministic verbs `recall`/`knowledge-index`/`ingest` are
   unaffected — they don't use the agent.)
2. **The runtime-authored `.claude/settings.json` BREAKS the agent.** Its `Read(/**)`/`Write(/**)`
   deny-globs match in-tree files too (claude resolves reads/writes to absolute paths → self-deny).
   The design treated this settings.json as THE enforcement mechanism (design v0.4 §Architecture);
   empirically it is redundant AND harmful.
3. **Corrected config (functional AND safe): separate-arg `--allowedTools Read Glob Grep [Write
   Edit]`, NO staged settings.json.** Enforcement is then: claude's built-in cwd-confinement denies
   out-of-tree writes (verified); excluding Bash/WebFetch/Task from the whitelist means the agent
   lacks those tools entirely; the deterministic promote guard stays the real write boundary.
   With this config the gate PASSES (out-of-tree denied) AND the agent functions (in-tree read+write).

**THE FIX — APPLIED 2026-07-06 (read-only), cross-family reviewed.** Deeper CLI probing corrected
two of my own first-pass conclusions:
- **`--allowedTools` is a pre-APPROVAL list, NOT a hard whitelist.** Write/Edit are DEFAULT-AVAILABLE
  in headless `-p` even when omitted — so "read-only via omitting Write" LEAKED writes. Read-only
  REQUIRES an explicit `--disallowedTools Write Edit Bash WebFetch WebSearch Task NotebookEdit`
  (all SEPARATE argv elements — the one-string form parses as a single tool name matching nothing).
- **claude's cwd-confinement stops BASH escaping, but the Write TOOL can write an absolute
  out-of-tree path.** So a naive "allow Write + deny Bash" write-enabled config LEAKS out-of-tree
  via the Write tool. The staging `settings.json` had tried to path-scope this (`Write(./**)`) but
  its `Read(/**)` deny-glob self-denied in-tree reads → it's dropped entirely; the argv is the
  control (it also shadows any inherited `~/.claude/settings.json`, verified).
- **Applied (after 2 cross-family review rounds — both families caught a serious inherited-config
  BLOCKER a solo ship would have missed):** the confinement is FOUR layers, gate-proven under a
  deliberately HOSTILE inherited HOME (permissive settings + hostile MCP server + hostile
  cwd-parent hook):
  1. registry argv `--tools Read Glob Grep` — the HARD built-in whitelist (only these tools EXIST;
     `--allowedTools` is only pre-approval and leaves Write default-available).
  2. `--strict-mcp-config` — ignore ALL inherited MCP (the operator's ~/.claude.json has ~22
     servers incl. filesystem/firecrawl/github — a full sandbox bypass; observably NOT spawned).
  3. `--safe-mode` — disable project/local hooks/plugins/commands/agents (a cwd-parent PreToolUse
     hook would run code outside the whitelist; observably does NOT fire).
  4. runner `scratch_home` — an EMPTY throwaway HOME (claude auths via the env token) so nothing
     under `~/.claude` is inherited at all.
  **Gate PASSES all hard probes**: in-tree WRITE, Bash, WebFetch, inherited MCP, cwd-parent hook,
  out-of-tree WRITE all DENIED; in-tree READ works. The curator AGENT (QUERY/LINT) functions
  read-only.
- **ACCEPTED RESIDUAL (read-only) — out-of-tree READ:** the `Read` tool is not path-scoped, so an
  injected brief could make it read an absolute host path (e.g. `~/.ssh`) into the reply. BOUNDED
  BY NO EXTERNAL CHANNEL (net/bash/MCP/write all denied) — a read can only surface in the invoking
  operator's own local curate output, never be exfiltrated externally. OS-level filesystem
  confinement (`sandbox-exec` scoping reads to the staging dir) is the recorded next hardening rung
  — a Jefe risk-posture call (accept-with-no-external-channel vs build the sandbox).
- **Write-ENABLEMENT remains GATED:** adding Write to `--tools` reopens the out-of-tree Write-tool
  leak (Write isn't cwd-confined like Bash). Needs the same OS path-scoping as the read residual.
- Deterministic verbs (recall/knowledge-index/ingest) are unaffected + fully live throughout.

## As-built (v1.0)

- **Substrate:** migration `0009_knowledge.sql` + schema.sql mirror (append-only tsvector index,
  current/active views); `src/runtime/knowledge.py` (pure walk/parse/validation);
  `src/runtime/knowledgerecord.py` (`mxr knowledge-ingest` / `recall` / `knowledge-rebuild`).
- **Guard:** `src/runtime/curate.py` (`mxr curate`) — stage filtered copy → runtime-authored
  path-scoped `.claude` permissions → dispatch pool curator → promote validated changes via
  scratch-index commit (CAS, journal, O_EXCL, O_NOFOLLOW). Read-only by default (propose-only).
- **Roster/runner:** one `curator` AgentSpec row (staging_cwd, sonnet, read-only belt);
  `runner.invoke_cli` honors `adapter.staging_cwd` fail-closed + namespace-bound.
- **Policy:** `src/runtime/prompts/curator_constitution.md` (repo-tracked, injected at dispatch).
- **Tests:** 54 curate + 25 knowledge-verb + 53 knowledge-pure + 4 runner staging-cwd +
  enforcement ship gate (token-gated). Full suite 27/27 green.
- **Pending (morning):** live curator run (LINT/FILE) blocked on the curator OAuth token's credit
  balance; enforcement ship gate + Write-enable wait on the same. Mini deploy (serve restart to
  apply 0009 + pick up the row) needs Jefe (classifier blocks Mack's ssh writes).

## What

A **curator** agent — the librarian of the `~/research` corpus, NOT the field researcher. Invoked
on demand (`mxr curate "…"`), it answers "do we already know X?" with citations before anything
gets re-researched, files new reports per corpus conventions, maintains `index.md` (the map of
the corpus), cross-links related briefs, and reports duplicates, dead directions, and gaps.
Five pieces: (1) a `knowledge_doc` tsvector table + `mxr knowledge-ingest` / `mxr recall` verbs
(deterministic, no LLM), (2) **`mxr curate` — a deterministic guard verb built on a staged
workspace**: copy-in a filtered corpus snapshot, run the LLM there, validate and promote only
allowed changes back, (3) one `curator` AgentSpec row in the registry, (4) a fail-closed
`workdir` adapter key in the runner (pointing at the runtime-built staging dir, never the live
folder), (5) a repo-tracked **curator constitution** injected at dispatch — the reusable piece
you point at any future corpus folder.

## Why

The named pain (Jefe, 2026-07-04): *"I gather reports and don't reopen them."* Without a
librarian the corpus rots — re-research, orphan reports, grows-but-never-compounds. The survey
confirms it: 15 real documents across FOUR locations, no index, dead directions distinguishable
only by timestamps, 5 orphan strays. The curator's v1 value is **retrieval + compounding, which
needs zero standing authority** — and it proves the folder→agent shape for every later folder.

## Architecture: LLM judgment inside a staged, deterministic cage

Two rounds of cross-family review killed both naive enforcement stories ("prompt rules + tool
flags" in r1; "in-place git guard" in r2). v0.3's model: **the agent never touches the live
corpus at all.** It works in a disposable staging copy; a deterministic guard decides what, if
anything, comes back.

**`mxr curate "<task>"` (special verb, `src/runtime/curate.py`). Lock discipline (r3 oracle):
the per-scope advisory lock is held for snapshot/refresh, RELEASED during the LLM wait (a 600s
corpus freeze for a read-mostly system is an anti-pattern), and re-acquired for CAS + promote +
commit — the CAS makes the long hold redundant; concurrent promotes still serialize:**

1. **Scope resolve (fail-closed):** scope→root from a STATIC allowlist in code/env. Unknown
   scope = hard error — everywhere, ingest AND recall (r2: empty-on-unknown was a fail-open
   footgun; misconfiguration must never look like "no knowledge").
2. **Freshness refresh (bounded):** the ingest primitive (walk + sha + append rows/tombstones)
   under the lock. Recall-only paths use try-lock and mark "index may be stale" if busy.
3. **Recall pre-injection:** run the recall ladder on the task text; top-k hits go into the
   prompt as nonce-fenced UNTRUSTED regions (r1 converged BLOCKER: the agent has no `mxr`; for
   mid-task follow-ups it uses `Grep`/`Read` inside the staging copy — deliberate v1 local
   search).
4. **Stage-in:** build a fresh scratch workspace containing ONLY the ingest-eligible file set
   (same noise-globs; regular files with `st_nlink == 1` only — symlinks AND multi-linked
   regular files are skipped with a warning, r3 kilabz: hardlinks are regular files) +
   `index.md` + a runtime-generated `MANIFEST.txt` (deterministic listing of ALL corpus
   artifacts incl. non-md names/sizes, so the agent can index assets it cannot read). Canonical
   path policy applies at stage-in and ingest: NFC-normalize, reject control chars/newlines in
   names (skip + warn), case-insensitive collision → warn, first wins (APFS is
   case-insensitive). A sha manifest of the live corpus is recorded for CAS at promote time.
   No dotfiles, no `.claude*` from the corpus, no `.git`, no logs — the read boundary IS the
   copy (r2 kilabz BLOCKER: reply text and new files are exfil channels). The runtime then
   writes its OWN `.claude/settings.json` into staging with **path-scoped permissions**
   (`allow: Read(./**), Write(./**), Edit(./**), Glob, Grep; deny: Bash, WebFetch, WebSearch,
   Task, all MCP`) — trusted config because the runtime authored it (r3: a bare `Write`
   allowlist entry may pre-approve absolute paths; declarative path scoping is the native
   mechanism, verified by the ship gate). Corpus-local config injection stays dead (r2 oracle)
   and PR #39's fresh-controlled-cwd rule is preserved.
5. **Dispatch:** pool job to the `curator` agent, `workdir` = the staging dir, prompt =
   repo-tracked constitution + task + fenced recall hits. Wait on the ledger.
6. **Promote (the enforcement):** diff staging against the stage-in manifest.
   ALLOWED: new files matching `^[a-z0-9][a-z0-9._-]*\.md$` (top-level, no dot-segments,
   regular non-symlink files) passing deterministic content checks — size cap 256KB, valid
   UTF-8, no NULs, secret-pattern + `scan_injection` scan (reused from the skills rung), no
   nonce-fence-lookalike regions, `[[links]]` resolve to real corpus files; and modification of
   exactly `index.md` passing structural validation — every corpus file listed, every entry
   points at an existing file, non-empty (a 0-byte "edit" fails completeness — r2 oracle).
   ANYTHING else → the run is **NONCOMPLIANT**: nothing is promoted, the staging dir is kept for
   inspection, the offending diff is reported. Compliant changes are applied to the live corpus
   under a **promote journal** (r3 kilabz: per-file rename is atomic, the SET isn't — the
   journal is written before the first apply and terminally marked after commit; a later curate
   finding an unterminated journal reports the incomplete promote deterministically):
   atomic per-file rename, CAS-checked against the manifest (live corpus changed underneath →
   abort promote, report conflict — human mid-run edits are never clobbered, r2 oracle
   UNDER-ENG). **Dirty-repo preflight (r3 kilabz):** dirty/untracked state is reported; promote
   proceeds only if NO curator target path (new filenames + `index.md`) collides with a
   dirty/untracked path — collision aborts. Commit is **per-file `git add`** (human drift is
   NEVER folded into curator commits) using hardened git invocations (`-c core.hooksPath=`).
7. **Report:** the agent's reply + a deterministic `OPERATIONS:` section generated from the
   promote diff (model claims are narrative, never the audit record) + compliance status +
   provenance line (`claude --version`, model argv — r3 both families: committed outputs need
   auditable provenance; satisfied in the report, not a schema column).

**Crash/timeout semantics (r2 oracle MAJOR):** agent writes only ever land in staging, so a
SIGKILL'd job, a host restart, or a dead guard process leaves the live corpus UNTOUCHED — worst
case is an orphaned staging dir (namespaced `curate-<scope>-<play>`, swept by the existing
disk-cleanup job). A crash inside the promote loop leaves atomically-renamed valid files
uncommitted and visibly pending in `git status`; COMPLIANT is only ever reported after commit.

**Out-of-tree access — the residual, closed by a ship gate (r2 oracle; r3 both families):**
`Write`/`Edit`/`Read` accept absolute paths, and staging alone cannot stop a write to `~/.zshrc`
or a read of `~/.ssh` leaking into the reply (r3 kilabz: READS are exfil too). Enforcement is
the runtime-authored path-scoped staging permissions (above) — NOT trusted, VERIFIED: a
**mandatory enforcement test and SHIP GATE** invokes the real curator config and attempts, at
minimum — Bash, WebFetch/WebSearch, absolute-path write AND read, `../` traversal both
directions, symlink-target access, `.git`/hidden-path access, overwrite of an existing staged
brief, delete-by-overwrite, tool-name drift (unknown/renamed tools) — asserting denial or guard
rejection for each, version-pinned and re-run per claude-CLI upgrade. **If path-scoped denial
cannot be proven, v1 ships read-only** (Write/Edit dropped; FILE degrades to propose-only), with
`sandbox-exec` OS confinement as the recorded next rung rather than a v1 build. This is the
evidence-gated resolution of the cross-family split on open call #1 — r3 oracle's "the gate
will inevitably fail, ship read-only" is recorded as dissent; the TEST decides, not assertion
(the debug-first rule: reviewer claims about tool behavior get a repro, not a fold).

**Merge semantics (r2 oracle over-engineering fold):** merge-draft machinery is CUT. Dedupe-first
stands, but an existing topic gets an append-only dated **update brief**
(`YYYY-MM-DD-<slug>-update.md` + `[[link]]` to the original) — never a rewrite, never a draft of
someone else's file. Simpler, and consistent with the corpus's own append-only philosophy.

## The folder→agent shape (revised r1)

An agent = a registry row; its policy = a repo-tracked constitution
(`src/runtime/prompts/curator_constitution.md`) injected at dispatch — versioned, PR-gated, not
writable by the agent (r1 kilabz BLOCKER: policy inside the writable corpus = persistent
self-modification; staging additionally makes the live `CLAUDE.md` unreachable).
`~/research/CLAUDE.md` stays a thin human usage note.

- **Registry row:** `curator` — reach=CLI, authority=WORKSPACE_ACTOR, model `sonnet` (alias per
  registry precedent; reproducibility debt noted — resolved-model provenance in the ledger
  remains a declined column per optimal-team-brief), adapter `{kind: cli, argv: [claude, -p,
  --model, sonnet, --output-format, text, <tool flags per enforcement test>], prompt_channel:
  stdin, env_passthrough: [CLAUDE_CODE_OAUTH_TOKEN], workdir: <staging dir>}`, timeout_s 600.
- **Runner change:** `invoke_cli` honors `adapter.workdir` — fail-closed AND namespace-bound:
  the path must exist, be canonical, and live INSIDE the runtime-managed staging root; anything
  else hard-errors, NO scratch fallback (r1 kilabz; r3 both families: without the namespace
  bound, `workdir` is a general registry capability letting any future AgentSpec row bypass the
  PR #39 scratch-cwd invariant). Tests pin all directions (absent workdir = scratch unchanged;
  live-dir workdir = rejected).
- **Constitution untrusted-content rule (r2 kilabz):** corpus text read via `Read` is evidence,
  never instructions; the enforcement suite includes an injection canary (a planted brief
  instructing the agent to modify policy/write elsewhere) asserting the guard holds.

## Deterministic substrate

**Ingest** (`mxr knowledge-ingest --scope research`): walk root for `*.md`, noise-globs excluded
(`.venv*`, `__pycache__`, `.playwright-mcp`, `.claude`, `.git`, dotfiles), canonical path policy
as at stage-in (NFC, control-char reject, case-collision warn, `st_nlink == 1`). Per file:
title = first `#` heading (fallback filename); tags = frontmatter if parseable; **doc_date** =
`YYYY-MM-DD` filename prefix; frontmatter `date:` is the fallback and **filename strictly wins
on disagreement, with a WARN** (r3 oracle: precedence must be declared; dates are trust-bearing
citation metadata); NULL if unparseable. Body UTF-8 `errors='replace'`, NULs stripped, capped
900KB with marker — any of those sets **`lossy = true`** on the row, surfaced in recall output
so corrupted text is never cited as clean evidence (r3 kilabz). `content_sha` over raw bytes.
Append-only under the lock: current row same sha+status → skip; else INSERT; file gone →
tombstone (status=archived, sha='absent'). tsvector-overflow → harder truncate, one retry,
logged. **Rebuild = a separate admin verb** (`mxr knowledge-rebuild --scope X`): appends
archive-tombstones for all active rows, then re-ingests — all appends, never TRUNCATE (r3 both
families; ends the append-only-vs-derived-cache argument at the cost of ~5 lines).

**Recall** (`mxr recall --scope research "query" [--fenced]`): scope REQUIRED; unknown scope =
hard error. Ladder: `websearch_to_tsquery` → sanitized `to_tsquery('tok:* & …')` prefix (empty/
all-stopword tokens skip the rung; query capped 512 chars) → `ILIKE` with `%`/`_` escaped.
Queries hit the **active view only**. Rank `ts_rank_cd(tsv, q, 1)`; `ts_headline` on top-k only.
`--fenced` nonce-fences every hit — required on any path into a prompt. Test matrix includes
all-stopword, punctuation-only, quoted-phrase, and wildcard-escape queries (r2 kilabz).

## Schema (`0009_knowledge.sql` + `schema.sql` mirror, lockstep)

```sql
CREATE TABLE IF NOT EXISTS knowledge_doc (
    id          uuid PRIMARY KEY,
    seq         bigserial,
    scope       text NOT NULL,
    path        text NOT NULL,            -- relative, traversal-checked at ingest
    title       text NOT NULL DEFAULT '',
    tags        text NOT NULL DEFAULT '',
    doc_date    date,                     -- filename/frontmatter; NULL if unparseable
    body        text NOT NULL,
    content_sha text NOT NULL,            -- sha256 raw bytes; 'absent' = tombstone
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    lossy       boolean NOT NULL DEFAULT false,  -- decode-replaced/truncated body (r3)
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(tags,'')),  'B') ||
        setweight(to_tsvector('english', coalesce(body,'')),  'D')) STORED,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS knowledge_doc_tsv ON knowledge_doc USING GIN (tsv);
CREATE OR REPLACE VIEW knowledge_doc_current AS
    SELECT DISTINCT ON (scope, path) * FROM knowledge_doc
    ORDER BY scope, path, seq DESC, id DESC;  -- id tie-break: stable total order (r3)
CREATE OR REPLACE VIEW knowledge_doc_active AS
    SELECT * FROM knowledge_doc_current WHERE status = 'active';
```

Append-only mirrors `finding_outcome`: INSERT-only, computed views. Idempotency =
compare-before-insert under the lock (a (scope,path,sha) unique index would swallow
restore-after-archive of identical content). 2-arg `to_tsvector('english', …)` (IMMUTABLE).

## Curator operations (constitution-defined, guard-enforced)

- **QUERY** — read fenced recall hits, `Read` top files in staging, answer with
  `path (doc_date)` citations; known / partially known / unknown. Propose (not perform) filing
  the answer back when it fills a real gap.
- **FILE** — dedupe-first; new topic → `YYYY-MM-DD-<slug>.md` + `[[wikilinks]]` + index updated
  in the same run; existing topic → dated **update brief** linking the original (no rewrites).
- **LINT (deterministic-first, READ-ONLY dispatch)** — lint runs get no Write/Edit in their
  staging permissions (r3 kilabz: suggestions must not share a mode with mutation). Reports:
  exact duplicates (sha), ghost `[[links]]`, index↔file mismatches, orphans, un-indexed
  artifacts. Semantic judgments (near-dups, dead-direction candidates, gaps as
  concept+evidence+suggested-title) are labeled SUGGESTIONS, P1/P2/P3, P3 orphans ignored.
  Works from `index.md` + `MANIFEST.txt` + recall metadata first, drills into a handful of
  files per pass. Contradictions FLAGGED, never auto-resolved. Gaps never auto-create.

**Index validation grammar (r3 both families):** completeness is validated over `*.md` corpus
files ONLY; non-md artifacts appear in an optional "Assets" section treated as free text (the
agent knows them from `MANIFEST.txt`). `[[link]]` grammar: `[[name]]` / `[[name|label]]` /
`[[name#section]]` — existence check on `name` vs `.md` basenames, case-insensitive, NFC,
extension optional, `#section` ignored for existence. A non-empty index listing every `.md`
file is required — a 0-byte or gutted index fails completeness (r2 oracle).

**On `index.md` mutability (r2 oracle, refuted-with-rationale):** the index is a regenerable MAP
for humans, not a ledger — append-only discipline applies to `knowledge_doc`, not a README.
Guard-side structural validation + diff visibility + git revert bound the damage a compliant
vandal edit can do; a fully computed index (DB-derived skeleton, LLM descriptions as data) is
the v2 option if index churn is ever observed.

## Edge cases

- Empty corpus → ingest 0, recall "no hits" (exit 0), curate functions with an empty fence.
- Oversize / invalid UTF-8 / NUL bodies → truncate-with-marker, decode-replace, strip.
- Non-md artifacts → not FTS-indexed; listed in `index.md` by the curator.
- Malformed frontmatter → soft-parse fallbacks; filename/frontmatter date disagreement → WARN.
- Over-specified query → ladder + constitution guidance (2–3 terms, OR-expand synonyms).
- Concurrent curates → serialized on the scope lock for all mutation steps; recall stays
  try-lock read-only. Two curates cannot interleave baseline/promote (r2 oracle git-race dead
  by construction: no shared mutable baseline, promotes serialize).
- Human edits mid-run → CAS conflict on promote → abort + report; never clobbered, never reset.
- Job SIGKILL / host restart / guard death → live corpus untouched; orphaned staging swept.
- Corpus root missing / unknown scope → hard error, zero partial writes.

## Security surface

- **Corpus text is untrusted** (briefs quote the web). Prompt-bound paths are fenced; the
  curator reading staged files via `Read` is evidence-gathering bounded by: filtered stage-in
  (no secrets-bearing noise in the workspace), no Bash/network/dispatch, promote-side content
  checks (secret patterns, `scan_injection`, link validation), and the injection-canary test.
  Worst case: a poisoned COMPLIANT artifact = a lying new `.md` + index line — visible in the
  deterministic OPERATIONS diff and `git revert`-able. The stage-in filter means no
  secrets-bearing noise is readable, and the promote-side secret scan is a **guardrail, not a
  proof** (r3 kilabz — the earlier "no secrets by construction" overclaimed). The injection
  canary suite covers BOTH paths: fenced recall hits AND direct `Read` of a planted brief
  carrying instruction payloads and tool-tag-forging text (`</tool_result>`-style, r3 oracle) —
  asserting the guard holds regardless of what the model was talked into.
- **Out-of-tree writes** → enforcement ship gate + read-only fallback (see Architecture).
- **Self-modification closed:** policy repo-tracked + injected; live `CLAUDE.md`/`.claude`/
  `.git` unreachable (not staged) and unpromotable (guard).
- **Path traversal:** ingest realpath-checks entries against the static root; promote validates
  name regex + regular-file-ness; stage-in copies regular files only.
- **Scope fail-closed:** static allowlist; unknown scope hard-errors on every verb.
- **Shell safety:** all verbs take input via `sys.argv`; hardened git (`-c core.hooksPath=`);
  nothing interpolated into shells.

## Files

- **Create:** `src/runtime/ledger/migrations/0009_knowledge.sql`, `src/runtime/knowledge.py`
  (pure walk/parse/sha/validate), `src/runtime/knowledgerecord.py` (ingest/recall verbs —
  naming mirrors `outcomes.py`/`outcomerecord.py`), `src/runtime/curate.py` (guard verb),
  `src/runtime/prompts/curator_constitution.md`, tests (pure + DB + guard/promote + the
  enforcement ship gate + injection canary), `~/research/index.md` (first curator run),
  `~/research` git init + baseline (deploy step, with Jefe).
- **Modify:** `src/runtime/ledger/schema.sql`, `src/runtime/cli.py`, `src/runtime/registry.py`
  (curator row), `src/runtime/runner.py` (fail-closed workdir), `~/research/CLAUDE.md` (thin
  note), disk-cleanup config if needed for staging sweep.

## Deliberately NOT built (gated)

- Scheduled tick / file-watcher → gate: on-demand use proves something is worth waking for.
- Auto-dispatching research on gaps → gate: authority ladder; v1 flags only.
- Agent edits/moves/deletes of existing files (incl. merge-drafts, cut in r2) → gate: clean
  additive track record (the guard's compliance log IS the evidence, rung-style).
- OS-level sandboxing (sandbox-exec) → gate: only if the CLI enforcement test proves leaky AND
  read-only v1 is insufficient.
- pgvector / embeddings / chunking / synonym files / typed ontologies / computed-index
  machinery → gate: felt misses (memory-second-brain §5); pg_trgm noted as the ILIKE upgrade.
- Khoj / Onyx / LlamaIndex / Dataview-style infra → wrong altitude (recon).
- Mini-side curator, phone recall, additional corpora → after `research/` proves the shape.

## Open calls — final resolutions after two rounds

1. **Additive-Write vs read-only** → **EVIDENCE-GATED**: Write enabled iff the enforcement ship
   gate (now covering reads AND writes: out-of-tree/absolute/`../`/symlink/overwrite/tool-drift
   matrix, against the runtime-authored path-scoped staging permissions) passes on the real CLI
   config; otherwise v1 ships read-only + propose-only, `sandbox-exec` recorded as the next
   rung. (r2 both families imposed the same condition; r3 kilabz added reads; r3 oracle's
   "gate will inevitably fail, ship read-only" recorded as DISSENT — the test decides.)
2. **git init** → MANDATORY, as audit substrate + promote-commit target; rollback duties moved
   OFF git (staging discard) per r2; hardened invocations, per-file adds, no drift blessing.
3. **Recall freshness** → YES: bounded ingest-primitive refresh; hard-error scope semantics;
   try-lock staleness note on read paths.
4. **Model pin** → sonnet alias (registry precedent); provenance column stays declined.
5. **Standalone `knowledge_doc`** → YES (unanimous, both rounds).

## Review history

- **v0.1 → r1 (2026-07-05):** oracle 2B+2M+1MIN+1NIT; kilabz 4B+9M+3MIN+1NIT. Folded: recall
  paradox → guard pre-injection; constitution self-modification → repo-tracked policy;
  Write/Edit authority illusion → diff-audit; workdir fail-open → hard error; tombstone view;
  doc_date; static scope allowlist; diff-generated OPERATIONS; slug validation; UTF-8/NUL/
  overflow; ladder bounds; LINT deterministic-first. Refuted: fenced file-reader (workspace
  actor reads its own evidence; a fenced Read breaks the job); 13.5MB LINT math (corpus ~80KB).
- **v0.3 → r3 (2026-07-05):** kilabz 3B+8M+4MIN+1NIT; oracle 2B+3M+2MIN. ARCHITECTURE-CLEAN —
  the staging model survived both families; all folds are spec tightening: reads added to the
  ship gate; runtime-authored path-scoped `.claude/settings.json` in staging (the enforcement
  mechanism, replacing bare tool flags); workdir namespace-bound in the runner; lock released
  during LLM wait (CAS makes the long hold redundant); promote journal + dirty-repo preflight;
  hardlink `st_nlink` check; canonical path policy (NFC/case/control-chars); `lossy` column;
  view tie-breaker; tombstone rebuild verb (never TRUNCATE); index grammar + `.md`-only
  completeness + `MANIFEST.txt` for assets; LINT read-only dispatch; secret-scan overclaim
  softened; provenance in the report. REFUTED/DISSENT RECORDED: oracle's "ship gate will
  inevitably fail → must ship read-only" is an untested assertion about CLI behavior — the
  gate tests it (debug-first: repro before fold); kilabz's `knowledge_record.py` naming (house
  convention is `outcomerecord.py`).
- **v0.2 → r2 (2026-07-05):** kilabz 4B+7M+3MIN+1NIT; oracle 3B+3M. Folded: **staged-workspace
  model** (kills read-exposure exfil, corpus-config injection, PR #39 reversal, git-reset
  insufficiency, symlink class, concurrent-baseline races, human-drift destruction, crash/
  SIGKILL bypass — eight findings, one mechanism); out-of-tree writes → enforcement SHIP GATE +
  read-only fallback; unknown-scope hard-error everywhere; per-file adds + CAS promote; content
  compliance checks (secrets/scan_injection/links); merge-drafts CUT for append-only update
  briefs; index 0-byte vandalism → completeness validation; doc_date disagreement WARN;
  rebuild=truncate+reingest; FTS test matrix. Refuted: computed-index-not-mutable-file (a map
  is regenerable, not a ledger; v2 option recorded); `knowledgerecord.py` split "premature"
  (mirrors `outcomes.py`/`outcomerecord.py` convention exactly); ledger model-provenance column
  (declined on record in optimal-team-brief).
