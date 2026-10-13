# DESIGN.md — MyndAIX Team Runtime

**Status:** **v0.4 — build-ready.** Stack: **Python**. The design was hardened through two cross-family adversarial review cycles (see "How it was reviewed") before any code.
**Author:** Steven Fernandez — designed with an AI pair-engineering team (Claude / Codex / Gemini).

---

## 1. What it does & why
A thin, deterministic orchestrator that **routes a unit of work to an agent (CLI or API), runs it in isolation, captures the result, delivers a reply, and logs every state transition to a durable ledger.**

It replaces a prior multi-agent runtime that coupled comms + execution + config into a single event loop — so any one slow or failed part (a hung embedding, a stale token, a lurking-reply policy) could take the whole command center down. The key insight from that failure: the *agents* were never the problem — Codex, Gemini, and Claude all answer direct local shell calls — only the *wrapper* around them was. This keeps the direct calls and replaces the wrapper with a durable state machine.

## 2. Non-negotiable principle
**Contracts are deep + rigid; roster, models, and transport are flexible data behind them.** Every change is ADDITIVE (a registry row), never STRUCTURAL (a patch around the spine).
**The test for every decision:** *can you add an agent / swap a model / change transport without editing the spine?* If no, a contract has a gap — close it first. If you're specifying more than the contracts + a thin roster, stop.

## 3. Failure-map — what NOT to build (lessons from a real multi-agent outage)
Don't couple comms to agent execution in one event loop — one blocking op froze it and took comms down (→C3) · don't let a single slow op block everything; no isolation, no bounded timeout (→C4) · don't use file-IPC for state — a recurring corruption class (→C2) · **don't let concurrent agents mutate the same repo** — the prior single-threaded runtime accidentally prevented this; a worker pool removes that protection (→C5) · don't put behavior in a mutable config schema that fights every patch (→keep config minimal) · don't let transport semantics leak into agent behavior (a chat platform's "group" classification made the bot lurk and silently drop replies) (→C3+C0) · never store secrets in plaintext config (→§7).

---

## 4. The contracts (specified — ~80% of this design)

### C0 — Capability model (the load-bearing axis; `cli|api` alone is insufficient)
Two orthogonal descriptors per agent:
- **reach** (how invoked): `cli` | `api` — drives the adapter + auth/cost.
- **authority** (what it may do): `responder` (prompt→text, no side effects; auto-retry-safe) · `workspace-actor` (reads/writes files; gets an isolated worktree C5; **never auto-retried** C4) · `controller` (may emit new dispatches; writes only via the Command API 4b) · `composite` (multiple internal calls; declares net authority).
Authority — not reach — drives retry-safety, isolation, and dispatch rights.

### C1 — Agent invocation
`invoke(agent_id, job) → result`
- **result**: `{ status: ok|error|timeout|killed|needs_human, text, exit_code?, error_class, artifacts?[], cost?, ms }`
- **error_class**: `retryable` (transient) · `terminal` (bad-auth, validation, non-zero-exit on a mutation) · `needs_human` (interactive/TTY prompt detected — park, never loop on stdin a headless spine can't answer).
- **adapter** per agent (cli: argv + prompt channel + stdout + exit-code map; api: endpoint + secret-ref + shape). Reach/authority from C0; cost/concurrency/timeout in the registry `profile`.
- **Progress visibility (v1):** a running job emits `heartbeat` + stdout/stderr chunks to a side channel (`attempt_log`), so long jobs are never dark and the worker never blocks. (Full structured streaming as a contract is deferred, §8.)

### C2 — The ledger (a state machine, not one table)
Postgres. Explicit concepts:
- **inbound_event** `{ id, transport, envelope(jsonb→C3), body, received_at, dedupe_key UNIQUE }`
- **job** `{ id, parent_id?, root_id, depth, created_by, to(agent_id), body, capability_required, priority, status(queued|leased|running|done|failed|dead), created_at, repo_id?, base_ref?, base_sha?, worktree_path?, artifact_ref? }`
- **attempt** `{ id, job_id, worker_id, lease_expires_at, started_at, ended_at, status, result(jsonb), error_class }`
- **outbound** (outbox) `{ id, job_id, transport, reply_target, body, status(pending|sent|failed), provider_msg_id?, tries }`
- **attempt_log** (append-only side channel) · **dead_letter** (exhausted work for human triage).
**Leases** → a crashed worker's job is reclaimed on expiry. **Dedupe** (`inbound.dedupe_key`, `outbound.provider_msg_id`) → exactly-once-ish.
**Job chaining:** a child job sets `base_ref` = a prior job's `artifact_ref`, so its worktree is created from the previous step's output — dependent multi-agent work passes state without auto-merging to the live tree.

### C3 — Comms ↔ execution boundary + transport envelope
- Transport adapters normalize inbound → **transport_envelope** `{ transport, account, sender_id, channel/thread_id, reply_target, provider_msg_id, dedupe_key, formatting_caps }`. **Transport semantics never leak into job/agent fields.**
- Transport is a **dumb pipe**: it writes inbound, reads pending outbound, delivers. It never invokes an agent and never blocks on agent work — the prior system's root-cause failure.
- A separate **worker pool** leases jobs, (for workspace-actors) assigns a worktree, invokes, and records the result + a reply.

### C4 — Failure semantics (a state machine)
- **Lease + heartbeat**: lease expiry (crash) → job reclaimed.
- **Retry is authority-gated** (the effect boundary): responder/read-only → auto-retry `retryable`. **workspace-actor → NEVER auto-retry** (a half-applied `git`/`sed` isn't idempotent) → on failure `dead_letter`.
- **timeout** → SIGTERM the **process group** → `killed`.
- **Admission limits:** every controller `submit_job` is checked against `max_depth` / `max_children` / `cost_budget(root)` / `chain_ttl` — stops a runaway job tree.
- **circuit-breaker** per agent (simple open/closed). **api agents** carry a cost/rate budget.

### C5 — Workspace isolation + runner enforcement
- A `workspace-actor` job runs in an **ephemeral git worktree** from `base_ref`@`base_sha`. Success → diff captured as `artifact_ref`, surfaced for review — **never auto-merged**. Failure → worktree preserved then GC'd; the live repo is never touched mid-flight. **Merge is a deliberate, serialized step**, never a race.
- **The runner — not the worktree — is the boundary.** It enforces cwd=worktree, a scrubbed env (declared secret-refs only), no writes outside the worktree, and process-group kill.

## 4b. Interfaces, orchestration & the Command API
- **Spine headless; interfaces are clients.** Deterministic dispatch (route, lease, retry, log) = spine. Judgment (decompose a fuzzy goal) = a `controller` agent. The spine does **no** judgment routing.
- **The Command API is the SOLE writer to the ledger** — transports, workers, controllers, interfaces all go through its verbs; nobody writes raw tables. Verbs (one transaction each): `ingest_inbound, submit_job, lease_job, heartbeat_attempt, complete_attempt, fail_attempt, enqueue_outbound, claim_outbound, mark_outbound_sent, mark_outbound_failed, reclaim_expired, dead_letter, cancel, get_status` — each with `allowed caller | pre-state | post-state | idempotency key | retry rule`.
- Interfaces: terminal (primary), chat pipe (mobile), GUI (a later read-view, not v1).

## 5. v1 roster (data — `authority` drives behavior)
| agent | reach | authority | model | role |
|---|---|---|---|---|
| lobster | cli | controller | opus | orchestration / judgment |
| mack | cli | workspace-actor | opus | hands-on builder |
| mini | cli | workspace-actor | claude | pipeline builder |
| kilabz | cli | responder | gpt-5.5 | code reviewer (read-only) |
| codex | cli | workspace-actor | gpt-5.5 | builder / debugger |
| oracle | cli | responder | gemini-3.1-pro | reviewer / vision |
| recon | api+cli | composite (read-only) | hybrid | research |

## 6. Data flow
`inbound → transport → ingest_inbound → submit_job → job[queued] → lease_job → (worktree) → invoke (heartbeat+log) → complete/fail_attempt → enqueue_outbound → deliver → mark_outbound_sent`. Controllers emit child jobs via `submit_job`.

## 7. Security surface
Untrusted = the inbound body; wrap before any prompt; **never auto-exec agent output as shell** (controller output → Command-API verbs only). Secrets live in a secret store / refs, never in plaintext config. Per-agent isolated creds. Runner enforcement (C5) is the workspace security boundary.

## 8. Scope guard
**Deferred to v2** (resisting these is the discipline): full structured streaming · multimodal/large artifacts · half-open circuit-breaker · backpressure/fairness scheduling · auto-merge of worktrees · GUI.
**Out of scope by design:** plugin system · eval harness · bundled-dependency staging · config schema beyond registry+secrets · embedded event loop.

## How it was reviewed
The design was hardened through **two cross-family adversarial review cycles** — Codex (GPT-5.5) and Gemini (3.1 Pro), each prompted to find what breaks — *before a line of code*. They caught real blind spots a single reviewer misses: a filesystem-concurrency corruption risk, an under-modeled ledger, and the capability-vs-`cli|api` distinction. The design evolved v0.1 → v0.4 under that fire.

## Changelog
**v0.4** — Command-API state-transition table + sole-writer · job-chaining + workspace fields · admission limits · progress visibility · runner-enforced isolation · stack = Python. Build-ready.
**v0.3** — capability model · worktree isolation · Command API · multi-table ledger · authority-gated retry · transport envelope.
**v0.2** — swarm-as-adapter · interfaces/orchestration split · no v1 GUI.
**v0.1** — initial four-contract sketch + roster.
