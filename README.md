# MyndAIX Team Runtime

> **This is a dated public snapshot of a private working repo.** It is the tree at commit `e7f6585`
> (2026-10-13), published as one commit. The working repo has 513 commits from 2026-06-22 to
> 2026-10-13; that history is not published. A short list of internal files and two runbook sections
> are left out (the CI workflow files, a few ops and budget notes), and local usernames, hostnames and
> one private repo name are replaced with placeholders. A pointer to a left-out file is marked
> `[not in the public snapshot]`. Apart from that and this README's own snapshot notes, the code and
> docs are as they stand in the working repo.

A deterministic, Postgres-backed spine for multi-agent work. One Command API is the only thing that
writes the ledger, so agents lease, run, and reply through database row locks instead of files — and the
file-IPC bug classes (double-lease, lost update, duplicate delivery, a crashed worker's job stuck forever)
are excluded by construction. I built it clean-room after a real multi-agent system went down for ~3 hours, when comms,
execution, and config were coupled into one event loop and any single slow part took the whole thing down.

```
   client / transport           ┌──────────────────────────────────┐
   (HTTP API · terminal) ─────▶ │  Command API   (the SOLE writer) │
         ▲                      └─────────────────┬────────────────┘
         │ reply                                  │ ingest · submit
         │ (outbox, decoupled)                    ▼
   ┌─────┴────────┐         ┌────────────────────────────────────────────┐
   │  transport   │ ◀────── │              Postgres ledger               │
   │ (dumb pipe)  │ deliver │  inbound_event · job · attempt · outbound  │
   └──────────────┘         │  status-guarded state machine, one lock    │
                            │  order (attempt→job), FOR UPDATE SKIP      │
                            │  LOCKED, transactional outbox              │
                            └──────────────────┬─────────────────────────┘
                              lease (SKIP LOCKED) │ complete / reclaim
                                                  ▼
                            ┌────────────────────────────────────────────┐
                            │      worker pool  (N workers + janitor)    │
                            │  heartbeats keep long jobs; crashed and    │
                            │  poison jobs are recovered, not lost       │
                            └──────────────────┬─────────────────────────┘
                                               ▼
                            runner → agent (CLI/API), killed as a process
                            group on timeout, file edits in an isolated
                            git worktree (the live repo is never touched)
```

The agents were never the problem — Codex, Gemini, and Claude all answer direct local shell calls. Only
the wrapper around them was. This keeps the direct calls and replaces the wrapper with a durable state machine.

Every slice shipped through adversarial review across model families, and every review caught a real bug
the green tests had missed — the table in [How I built it](#how-i-built-it) is the proof. If you read only
one section, read that one.

## Run it

**Prerequisites:** Python 3.11+ and git — plus one `pip install`, that's everything the no-infra demos
need. The Postgres demos add a local Postgres; the optional real-agent demos add an agent CLI (Node 18+).
All three are below.

> **Full install guide** — every agent CLI (install / auth / verify), the always-on service, and
> one-machine *and* two-machine deployment — is in **[SETUP.md](SETUP.md)**. What follows is the quick tour.
> **Have Docker?** `docker compose up --build` brings up Postgres + the spine in one command (see [SETUP.md](SETUP.md#run-it-with-docker-postgres--service-one-command)).

### 1. Clone and run (no infra — just pydantic; no Postgres, no API keys, no LLM)

```bash
git clone https://github.com/redeyefit/myndaix-runtime-public && cd myndaix-runtime-public
python3 -m venv .venv && source .venv/bin/activate   # avoids the PEP 668 error on modern macOS/Debian
pip install pydantic                                  # the only dependency these demos need
PYTHONPATH=src python3 demo.py            # route a message through the spine and back
PYTHONPATH=src python3 demo.py --isolate  # an agent fixes a bug in a throwaway git worktree
```

The `--isolate` run has a workspace-actor edit buggy code inside an ephemeral git worktree. The live repo
is never touched; the change comes back as a reviewable diff, never auto-merged:

```
  app.py before : 'def add(a, b):\n    return a - b  # bug'
  job ee152ab6 -> done (ran in an isolated git worktree)
  app.py AFTER  : 'def add(a, b):\n    return a - b  # bug'   <- LIVE REPO UNTOUCHED
  the agent's change, captured as a reviewable diff (not merged):
    -    return a - b  # bug
    +    return a + b
```

### 2. The Postgres demos (where the real concurrency lives)

```bash
pip install asyncpg fastapi uvicorn httpx

# macOS:           brew install postgresql@16 && brew services start postgresql@16
# Debian/Ubuntu:   sudo apt-get install -y postgresql && sudo service postgresql start

# these demos DROP and recreate the schema — use a THROWAWAY db, never your ops 'runtime'
createdb runtime_test
export LEDGER_TEST_DSN=postgresql://localhost/runtime_test

PYTHONPATH=src python3 demo.py --pool      # N workers drain a queue + recover a crashed worker
PYTHONPATH=src python3 demo.py --postgres  # the SAME worker core, now backed by Postgres
PYTHONPATH=src python3 demo.py --terminal  # a dumb-pipe transport: a slow agent never blocks it
PYTHONPATH=src python3 demo.py --api        # the HTTP service: POST a job, GET its status + reply
```

`--isolate` (SQLite) and `--postgres` call the *same* `worker.drain()` — only the ledger differs. Swapping
persistence behind the contract is the central thesis, and it's structural: one core runs both stores.

### 3. (Optional) Route to a real agent

The roster in `src/runtime/registry.py` maps each agent to a local CLI, so a real model is just a CLI
install + login away (needs Node 18+ and your own provider account). This needs no Postgres —
`demo.py <agent>` uses an in-memory store; only the agent's CLI must be installed and authenticated.
For example, `demo.py kilabz` runs a code-review agent through OpenAI's Codex CLI:

```bash
npm install -g @openai/codex          # the codex CLI
codex login                           # OAuth — or: export OPENAI_API_KEY=sk-...
PYTHONPATH=src python3 demo.py kilabz  # routes a real Codex review through the spine
```

Every other agent works the same way — install its CLI, authenticate, and the adapter is already wired
(e.g. Claude Code: `npm install -g @anthropic-ai/claude-code`; the Gemini-backed `oracle` agent uses the
`agy` CLI; `recon` is an API agent that just needs `PERPLEXITY_API_KEY` in the environment). The full
per-agent install / auth / verify steps are in **[SETUP.md](SETUP.md#4-install-the-agent-clis)**.

## The design

The rule the whole thing follows: contracts are deep and rigid; the roster, models, and transport are
flexible data behind them. Every change is additive (a registry row), never structural (a patch around the
spine). The test for any decision is *can you add an agent, swap a model, or change transport without
editing the spine?*

- **C0 — capability model.** Each agent has a `reach` (cli|api) and an `authority`
  (responder / workspace-actor / controller / composite). Authority, not reach, drives retry-safety,
  isolation, and dispatch rights.
- **C1 — invocation.** One `invoke(agent, job) -> Result` for any agent. CLI agents run in their own
  process group with a hard timeout, so a stuck agent is killed cleanly with no orphaned children.
- **C2 — ledger.** A state machine (inbound_event / job / attempt / outbound / dead_letter) with leases
  for crash recovery and dedupe for exactly-once-ish ingest — not files. The same Command-API verbs are
  implemented over SQLite (the zero-dep demo) and asyncpg Postgres (production), behind one worker.
- **C3 — comms boundary.** A transport is a dumb pipe over the ledger and can never block on agent work,
  which was the original outage's root cause. Transport details stay in the envelope and never leak into
  agent fields (a chat platform's "group" flag silently dropping replies is what sank the prior system).
- **C4 — failure semantics.** Authority-gated retry (mutating agents never auto-retry), process-group
  kill, and admission limits so a job tree can't run away.
- **C5 — workspace isolation.** Every file-mutating job runs in an ephemeral git worktree, so concurrent
  agents can't corrupt a shared repo.

The Postgres ledger is where the real work is: one transaction per verb, a status-guarded compare-and-swap
on every transition, a single canonical lock order (attempt then job) so there's no ABBA deadlock,
`FOR UPDATE SKIP LOCKED` to hand distinct rows to racing workers, and a transactional outbox so a finished
job can't lose its reply. Full spec and decision log in [`DESIGN.md`](DESIGN.md).

## Extending it: a video agent in one roster row

The "additive, never structural" rule isn't aspirational — here's the receipt. Adding **Higgsfield**
(image/text → video) was one `AgentSpec` row, one adapter function, and a two-line dispatch branch.
No spine edit — even though its wire protocol looks nothing like the other agents'. CLI agents are
subprocesses; `recon` is OpenAI-style chat; Higgsfield is an **async media queue**: submit returns a
`request_id`, then you poll until the render lands. Three different shapes, one `invoke(agent, job) -> Result`:

```
higgsfield "gentle cinematic push-in"  +  --image <url>
   └─ leased → submitted → polled (~65s) → Result.artifact_ref = https://cloud-cdn.higgsfield.ai/….mp4
```

The load-bearing problem here is **money**: a render is billed the moment it's submitted, so the worker
must never auto-retry one. The runner is fail-closed by construction — once a job is submitted, every later
failure (poll error, timeout, a malformed response, *anything*) returns a terminal result, never a
retryable one, so a job can't be charged twice. Untrusted image URLs are SSRF-screened (no private /
loopback / cloud-metadata targets), and it's **live-verified** end-to-end: a real call produced a 31 MB
mp4 in 65 seconds.

Built the same way as the rest — design-first, then pressure-tested across multiple model families. That
review caught a success-path crash (a non-numeric `cost` field discarding an already-paid result) and
several charge-correctness holes the green tests missed; each is now a regression test.

## How I built it

Six core slices — ledger, worker, concurrent pool, terminal transport, HTTP API, auth — plus the
Higgsfield video agent later, each pressure-tested across model families (Codex, Gemini, Claude) before
moving on. Different families catch different things, and every review surfaced a real bug my passing
tests had missed:

| Slice | The bug the green tests missed | Now caught by |
|---|---|---|
| ledger | `cancel()` locked rows in the opposite order from every other verb — an ABBA deadlock | a 150-trial cancel-vs-finish race |
| worker pool | one poison job (bad binary, or an api agent) silently killed *every* worker; `gather(return_exceptions=True)` ate the tracebacks | a poison-job survival test |
| terminal transport | a per-process counter reused as a dedupe key misrouted a reply to the wrong sender after a restart | a restart / dedup test |
| HTTP auth | `created_by` doubled as the owner *and* a provenance tag, so a client keyed `id=human` could read every internal job | a provenance-collision test |
| Higgsfield agent | a non-numeric `cost` field crashed the *success* path — discarding an already-paid render, then re-charging it | a malformed-cost test + a post-charge fail-closed backstop |

I confirmed each fix by reproducing the bug, not just trusting the green bar. For the deadlock I reverted
`cancel()` to the old lock order, watched the regression test fail on the first trial with a
`DeadlockDetectedError`, then re-applied the fix and watched it pass 150/150. Every fix here has a
regression test that fails without it.

## The crew it runs

Every agent is one `AgentSpec` row in `src/runtime/registry.py` — a reach (cli or api; the async-media
queue is an adapter kind under api) plus an authority (responder / workspace-actor / controller /
composite). The call signs below show up all over the docs and code comments, so here are the
introductions. Two more names appear there that are not agents: "Jefe" is me, the author, and "the
Mini" or "the factory" is the always-on second machine.

| Agent | Backed by | Authority | Job |
|---|---|---|---|
| `mack`, `mini` | Claude CLI | workspace-actor | the builders (laptop, and the always-on Mac Mini) |
| `kilabz` | Codex CLI, read-only sandbox | responder | the adversarial code-review gate |
| `kilabz-sr`, `kilabz-deep` | the same Codex reviewer, built from `kilabz`'s adapter | responder | `kilabz-sr` returns a schema-checked verdict for the PR gate; `kilabz-deep` is a higher-effort pass, dispatched by hand |
| `codex` | Codex CLI, workspace-write, net pinned off (best-effort) | workspace-actor | sandboxed autonomous fix attempts |
| `oracle` | Gemini CLI | responder | cross-family second opinion; design review |
| `lobster` | Claude CLI, confined | controller | triage; merges the reviewer verdicts |
| `curator` | Claude CLI, read-only tools, staged cwd | workspace-actor | knowledge-corpus curator |
| `librarian` | Claude CLI, zero tools | responder | cited answers over personal corpora |
| `recon` | Perplexity API | composite | research |
| `higgsfield`, `stitcher` | Higgsfield render API | responder / workspace-actor | paid text/image→video (one clip / chained long-form) |
| `mx-engine` | deterministic bash pipeline | workspace-actor | topic → finished narrated reel |

The dispatch is deliberately cross-family — Claude builds, Codex gates, Gemini second-opinions —
because different model families catch different bugs; the "How I built it" table shows exactly that.

## How the work is run today

I set the goals and the tasks. Three Claude Code seats carry them out largely on their own: a chief
engineer turns a goal into a design and rules on exceptions, a manager orders the work and reads the
reviewers' verdicts, and a builder writes the code. They coordinate through Claude Code's own
multi-session features, not through a custom orchestrator. Money, production changes and what gets
built stay my calls.

This runtime sits underneath as the durable spine: the ledger, the worker pool, and the cross-family
review on every push. That review loop is driven by a deterministic controller, not by a model. Codex
and Gemini do the reviewing; Claude's only part in it is to merge their verdicts into one.

## What grew on top

The spine above is the June core. Since then an autonomous loop grew around it — new modules and
scripts behind the existing verbs, plus registry rows; none of it edited the spine's contracts or
the ledger core:

- **Review-on-push.** `orchestrator/play-review.sh` rides the repo's pre-push hook: every push gets an
  injection-fenced diff review from `kilabz` (with `oracle` as second opinion), `lobster` merges the
  verdicts, and the result lands as a file drop + desktop ping. The push itself is never blocked. An
  hourly non-LLM controller tick ([`controller.py`](src/runtime/controller.py)) reconciles anything the
  hook missed against a durable review cursor — the edge trigger has a level-triggered backstop
  ([design](docs/controller-loop-design.md)).
- **Autonomous fix, fail-closed.** A NEEDS-FIX verdict can trigger one sandboxed Codex fix attempt
  (`play-fix.sh`): worktree-isolated, net egress pinned off (a best-effort seatbelt — the real backstop
  is that nothing is ever auto-applied), verified against a clean checkout, delivered as an inert diff.
  Arming it first requires a probe proving the sandbox cannot write the shared `.git`
  ([design](docs/phase2-pr4-fix-stage-design.md)).
- **Docs-only automerge.** The first deliberate removal of the human merge gate: a mechanical
  diff-class gate proves every changed file is inert markdown before an hourly tick merges the PR —
  flag-gated, capped, revertible ([design](docs/automerge-design.md)).
- **A learning stack that measures before it acts.** A feedback loop that scores the reviewers
  themselves: findings are recorded per-finding with an identity that survives diff shifts, humans
  label them real / false-positive, and a shadow dial reports — with Wilson confidence bounds — which
  reviewer×rule pairs could be trusted or suppressed, while acting on nothing. Review skills (lessons
  from past reviews) inject as nonce-fenced untrusted hints
  ([outcomes](docs/outcomes-ledger-design.md), [dial](docs/shadow-dial-design.md),
  [skills](docs/learning-rung-design.md)).
- **A second brain.** A tsvector-indexed knowledge corpus over real folders (`curate`, `mxr ask`) with
  cited answers from the zero-tool `librarian` responder, so a poisoned document can at worst produce a
  wrong answer, never a file read or a dispatch — phone-reachable through an SSH forced-command wrapper
  exposing four fixed verbs ([phone](docs/phone-tailnet-surface-design.md), [curator](docs/curator-design.md),
  [ask](docs/mx-ask-librarian-design.md)).
- **Substrate: GitOps for two machines** — the laptop (the lab) and an always-on Mac Mini (the
  factory). The factory converges on `origin/main` via a 15-minute reconcile — config parsed, never
  sourced; additive-only migration lint; health-gated restart; auto-revert to last-known-good —
  watched by two independent canaries that also watch each other, plus a nightly verified `pg_dump`
  of the ledger. The lab watches the factory back (`tailnet-watch`), so a dark Mini can't go
  unnoticed for weeks again ([substrate/SETUP.md](substrate/SETUP.md)).
- **Paid-media production.** Long-form video via chained renders + ffmpeg (`stitcher`), a deterministic
  topic→narrated-reel pipeline (`mx-engine`), and a brief→motion-plate quality driver — all under the
  same never-auto-retry-a-paid-job rule the Higgsfield section explains
  ([stitcher](docs/stitcher-design.md), [quality driver](docs/MX_QUALITY_ORCHESTRATOR.md)).

Each shipped the same way: design doc → cross-family adversarial review → build → review again. The
raw review transcripts for some of the larger slices (the automerge gate and the controller loop among
them) are in [`docs/reviews/`](docs/reviews/), unedited apart from the local username in file paths;
the other slices record their review rounds and folded findings inside their design docs.

## What works, what doesn't

**Built and tested (39 suites / 738 test functions, run by the working repo's CI on every push —
real Postgres, real subprocesses, and the real FastAPI app driven in-process; the two live gates that
drive a real agent CLI and spend tokens, the curator and reviewer confinement gates, self-skip unless
you opt in; the Higgsfield agent additionally live-verified end to end against the real API):**

- A ledger-agnostic worker: one core drives the SQLite demo store *or* the Postgres production store.
- The asyncpg Postgres ledger: all the Command-API verbs, a 38-test suite against a live Postgres whose
  **concurrency proofs** include 50 workers racing one lease, a 200-trial reclaim-vs-complete
  mutual-exclusion proof, a 150-trial cancel-vs-finish deadlock proof, exactly-once ingest/delivery,
  and admission limits under 50 concurrent submits.
- A concurrent worker pool: no double-processing, crashed jobs recovered by the janitor, long jobs kept
  alive by heartbeats, and a poison job can't take down the fleet.
- A terminal transport that never blocks on an agent, with replies delivered fully decoupled.
- A FastAPI HTTP service with API-key auth: a client submits and reads only its own jobs (ownership is a
  namespaced `api:<id>`; reading someone else's job is a 404, not a 403, so ids never leak); admin reads any.
- An api-reach adapter (OpenAI-compatible chat): `recon` runs as a live Perplexity API agent through the
  *same* `invoke()` path as the CLI agents — the key comes from the environment, never the roster.
- An async-media adapter (`higgsfield`, image/text→video): submits to a render queue and polls for the
  result through that same `invoke()` path; **fail-closed** so a billed render is never auto-retried or
  double-charged; input URLs are SSRF-screened; the mp4 URL returns as `Result.artifact_ref`. Live-verified
  against the real API (a 31 MB video in 65s).

**Not built, or built but deliberately not armed (named on purpose):**

- A redelivering chat transport — Telegram is [designed](docs/telegram-transport-design.md), not built;
  the phone surface shipped as the tailnet SSH wrapper (`orchestrator/phone/`) instead.
- The C4 admission budgets (cost/chain-TTL) and capability-gated routing — specified in `DESIGN.md`,
  not yet exercised in code. Composite authority ships as a value (`recon` is composite, treated
  fail-closed for retries), but the multi-call net-authority semantics are not built.
- The HTTP API runs in CI and the demo; nothing deploys it as a service yet.
- The inbox assistant (Gmail → labels + reply drafts + one morning brief) is merged and CI-tested but
  not armed — its [deploy runbook](docs/inbox-assistant-deploy.md) is explicitly marked NOT EXECUTED.
- The learning stack's acting rungs (auto-suppress, auto-propose): measurement ships, acting is gated on
  accrued human labels — two acting designs were refused in adversarial review as premature.

## Prior art

This is a category others found too — local coding-agent CLI orchestrators with a durable ledger and
git-worktree isolation — not a novel one. Independent projects converged on nearly the same design:
[striatum](https://github.com/halbritt/striatum) (Go; daemon-owned Postgres, leases, append-only
hash-chained provenance), [gobby](https://github.com/GobbyAI/gobby) (Python; PostgreSQL as the source of
truth, a stdio MCP server, a task→PR build loop), [persistent-agent-runtime](https://github.com/shenjianan97/persistent-agent-runtime)
(Postgres, `FOR UPDATE SKIP LOCKED` leases, heartbeats, a reaper, dead-letter), and
[yarli](https://github.com/rahulrajaram/yarli) (Rust; Postgres event store, worktrees). I built this
independently, and the convergence is the point: when several teams reach the same primitives — a
single-writer ledger, `SKIP LOCKED` leases, a transactional outbox, ephemeral worktrees — those are the
load-bearing ones. What's mine to own here isn't the idea but the execution — a deliberately small
single-Command-API-writer core, the Postgres concurrency proofs (50 workers racing one lease; a 200-trial
reclaim-vs-complete mutual-exclusion proof), and a cross-family adversarial-review process that caught the
bugs the green tests missed.

## Tests

39 suites / 738 test functions. The working repo's CI runs all of them on every push; its
workflow files are not part of this snapshot, so there is no badge here. Two live gates drive a real
agent CLI and spend tokens, so they self-skip unless you opt in (`RUN_CURATOR_ENFORCEMENT=1`,
`RUN_KILABZ_ENFORCEMENT=1`). A suite with a `__main__` block runs directly (`python3 tests/test_x.py`),
no pytest config needed; the rest run under pytest. A separate bash layer covers the tracked shell
scripts: shellcheck, repo-specific semgrep rules (this repo's recurring bash bugs, encoded), bats
regression tests, a substrate smoke + security harness, and an additive-only migration lint. The
Postgres-backed suites need the DB from the "Run it" section above:

```bash
# no infra (SQLite)
PYTHONPATH=src python3 tests/test_worker.py

# Postgres-backed (e.g. the 38-test ledger suite with the concurrency proofs) — throwaway db;
# the suite resets the schema
LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_postgres_ledger.py

# the bash layer
./tools/bash-check.sh && bats tests/bash/
```

## Layout

```
DESIGN.md                    the spec + decision log (v0.1 -> v0.4)
demo.py                      the runnable demos above
src/runtime/
  contracts.py               C0-C3 as Pydantic models
  registry.py                the agent roster, as data (14 AgentSpec rows)
  command_api.py             the Command-API verb interface (sole ledger writer)
  api.py                     FastAPI HTTP surface + API-key auth
  runner.py                  C1 runner: CLI (process-group kill + timeout), the API
                             adapter, and the Higgsfield async-media adapter
  cli.py                     mxr — submit a job from the terminal, plus the rung
                             verbs (review, ask, outcome, dial-shadow, ...)
  worker.py                  the lease -> invoke -> result core (drives either store)
  pool.py                    concurrent worker pool (N workers + janitor + heartbeats)
  workspace.py               C5 ephemeral git-worktree isolation
  serve.py                   the always-on service (auto-migrates on boot, fail-closed)
  controller.py              the hourly review reconciler (non-LLM brain)
  automerge.py               the docs-only PR auto-merge gate
  staging.py, review.py      de-linked read-only review snapshots + the mxr review verb
  skillmatch.py, skillselect.py, capture*.py, outcomes*.py, dialshadow*.py
                             the measure-first learning stack
  knowledge*.py, curate.py   the tsvector second brain (curator + mxr ask)
  inbox_assistant.py, gmail_client.py
                             the drafts-only Gmail brief (built, not armed)
  orchestrator.py, runner_stitch.py, ffmpeg_util.py, critic.py
                             paid-media production (quality driver, stitcher)
  ledger/
    schema.sql               the Postgres state machine (DDL)
    postgres_store.py        the asyncpg ledger (production)
    sqlite_store.py          the SQLite store (the no-infra demo)
    migrations/              idempotent ALTERs for an already-deployed db
  transport/
    terminal.py              C3 terminal transport (dumb pipe over the ledger)
orchestrator/                the autonomous review loop (play-review.sh, play-fix.sh,
                             controller-tick.sh) + the phone SSH wrapper
substrate/                   two-machine GitOps: reconcile, drift + liveness canaries,
                             nightly ledger backup, the lab's tailnet-watch
tests/                       39 suites / 738 test functions
docs/                        per-feature design docs; docs/reviews/ holds the committed
                             cross-family review transcripts (the audit trail)
```
