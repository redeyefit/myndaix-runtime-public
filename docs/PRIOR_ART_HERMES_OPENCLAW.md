# Prior-Art Brief — Trusted Brain (OpenClaw + Hermes Agent)

**Date:** 2026-06-28
**Purpose:** Research-first deliverable (per new-systems rule) before standing up the trusted
autonomous brain on the always-on Mini. Decides BUILD vs ADOPT vs BORROW against MyndAIX's
constraints: local-first, durable bash-on-Postgres spine, solo, anti-over-engineering, **trusted =
controlled + comprehended**.

**Method:** Ground truth, not marketing — installed-package inspection (`openclaw` npm pkg on the
Mini) + repo-grounded Q&A (DeepWiki over `openclaw/openclaw` and `NousResearch/hermes-agent`).

---

## OpenClaw — `github.com/openclaw/openclaw` — VERDICT: REJECT (decommissioned 2026-06-28)

*"Your own personal AI assistant. Any OS. Any Platform. The lobster way. 🦞"* — TypeScript, v2026.4.29,
35 deps, built on the `pi` agent framework (`@mariozechner/pi-*`) + ACP + MCP, SQLite state.

**Architecture = the coupling sin.** One always-on **Gateway** process multiplexes WS control/RPC +
HTTP + plugin routes; **cron, agent-execution, messaging, and config all live inside it**, sharing
`state/openclaw.sqlite` (+ per-agent SQLite). Logically separable, runtime-coupled through the single
process.

**Its OWN docs list the failure modes we lived:** 4GB-RSS gateway memory growth → OOM exits; orphaned
processes; file-IPC / session-lock corruption; "gateway silently stops responding" under macOS Power
Nap + launchd respawn-protection; stalled sessions. These are **design consequences, not
misconfiguration** — confirming Jefe's "graveyard of the walking dead" judgment. Decommission was correct.

---

## Hermes Agent — `github.com/NousResearch/hermes-agent` — VERDICT: BORROW THE PATTERN (do not adopt)

Nous Research, Feb 2026, ~110k stars in 10 weeks. **Python**, modular (loose coupling via registry +
`check_fn` gating). Self-hosted, persistent memory, "the agent that grows with you."

**The headline: Hermes independently arrived at the architecture MyndAIX already hand-built.**

| Capability | Hermes ships | MyndAIX already has |
|---|---|---|
| Durable board | Kanban (`~/.hermes/kanban.db`): leasing + `kanban_heartbeat` (stale timeout 4h), dead-PID zombie detection → reclaim, CAS on `status`/`claim_lock` (WAL + `BEGIN IMMEDIATE`), `idempotency_key`, `max_runtime_seconds` hard cap, circuit-breaker (`failure_limit` 2 → auto-block `gave_up`) | ledger: `get_attempt_job` FOR-UPDATE ownership gate, `_requeue_safe` + `non_idempotent` flag, CAS verbs, admission limits |
| Self-learning | Reflective Phase → writes `SKILL.md` (YAML frontmatter); **`skills.write_approval: true` stages for human approve/reject, survives restarts**; trigger heuristic = ≥5 tool calls / fixed tricky error / user corrected approach | auto-capture rung: recurrence ledger + **human-gated** proposer (the `feat/auto-capture` branch in flight) |
| Curation | Curator: idle(2h)+7d cycle, lifecycle active → stale(30d) → archived(90d), optional LLM consolidation (off by default) | the `+learning` rung (planned) |
| Autonomy | dispatcher loop, 60s tick, reclaim/promote/claim/spawn; durable cron w/ `.tick.lock` | controller-loop rung (deferred) |

**The one deliberate divergence is MyndAIX's advantage:** Hermes is **SQLite (single-writer)**; MyndAIX
is **Postgres**. For a *two-machine always-on* brain (MacBook + Mini = concurrent multi-writer), Postgres
is the correct substrate. On the decision that governs whether real multi-host autonomy is possible,
MyndAIX is *ahead* of the 110k-star project.

**Why BORROW, not ADOPT:** "Trusted" requires control + comprehension. Adopting a big external always-on
framework — even an excellent, less-coupled one — repeats the OpenClaw mistake: when it breaks unattended,
you debug *their* 35-dep system, not your lean spine. MyndAIX is already ~80% of this, validated, and
controlled. Re-hosting on Hermes would discard a working controlled implementation (waste) and downgrade
the substrate (SQLite). Adopting is rational *from zero*; MyndAIX is not from zero.

---

## Steal-list (borrow into the spine we control — prioritized to where we are)

1. **Skill-capture (current `feat/auto-capture` branch):** Hermes `write_approval` staging — proposals
   persist across restarts, approve/reject via CLI — onto our recurrence ledger + proposer. Adopt the
   capture trigger heuristic (≥5 tool calls / fixed tricky error / user corrected approach).
2. **Curator lifecycle (`+learning`):** skill states active → stale(30d) → archived(90d) so the library
   doesn't rot.
3. **Spine hardening (small ledger diffs):** `idempotency_key` on task creation; `max_runtime_seconds`
   hard cap (terminate + requeue).

## Deliberately NOT building / adopting

- NOT a single always-on gateway (the OpenClaw sin). Brain = bounded controller JOB on the spine; the
  spine executes; transport is a tap, not a control plane (per [[north-star-autonomous-brain]]).
- NOT SQLite for shared state (single-writer; blocks multi-host).
- NOT Hermes wholesale — read it as the reference manual, copy designs, not code.

## Decision

**BORROW. Build the mxr/Postgres spine on the clean always-on Mini; treat Hermes as the reference
implementation.** Next: provision the Mini (agy + postgresql@16 → spine → smoke-test), then resume the
north-star climb with the steal-list folded in.
