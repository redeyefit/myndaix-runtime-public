# Controller-Loop ("the brain") — Prior-Art Brief (Phase 0, step 1)

_North-star rung 3: the autonomous controller-loop. v1 scope = **proactive review scheduler**, built as a thin general skeleton (decide→emit), no LLM judgment / no auto-merge / no learning yet. See [[north-star-autonomous-brain]]._

**Date:** 2026-06-25 · **Author:** Mack (interactive, with Jefe) · **Status:** brief for Jefe + Oracle review before DESIGN.md.

## What we're building (one line)
A bounded, non-Claude controller JOB that wakes on a timer, reads ledger + repo state, and dispatches a review when a repo's HEAD has advanced past what was last reviewed — turning push-triggered review into **brain-decided** review.

## Our constraints (the lens for every verdict)
Local-first · solo founder · bash-on-Postgres durable spine already built · anti-over-engineering · classifier-walled (the trigger must be **non-Claude** to be genuinely autonomous, per [[autonomous-dispatch-classifier]]) · no inbound HTTP listener bound (the Command API's `POST /jobs` is dormant).

## Capability-by-capability verdict

### 1. Control-loop logic — **BORROW the pattern (K8s level-triggered reconciler); BUILD thin**
The canonical prior art is the Kubernetes controller reconciliation loop. The decisive principle: the loop is **level-triggered, not edge-triggered** — `Reconcile()` derives the desired state from the *current observed world state*, **not** from the event that woke it; the triggering event isn't even passed in, forcing state-based logic. Reconcile must be **idempotent** (it's called repeatedly for the same state — periodic resync, requeues, child events). This makes it robust against missed events and gives eventual consistency.

This maps **exactly** onto our brain: each tick reads ledger state ("last-reviewed SHA per repo") + world state (live `git ls-remote HEAD`), computes the gap, and dispatches the diff. A *missed* push doesn't matter — the next tick sees the SHA mismatch and catches it. So the controller is a **level-triggered backstop** that composes with the existing edge-triggered push-hook (the hook gives low latency; the loop guarantees nothing is permanently missed — e.g. pushes from a machine without the hook installed).

- **BORROW:** the level-triggered + idempotent reconcile principle.
- **REJECT:** adopting a controller framework (controller-runtime / Operator SDK). We have no CRDs, no API server; the framework is mass for a problem a ~150-line Python tick solves on our Postgres spine.

### 2. Periodic trigger — **ADOPT launchd (already in use)**
We already run `ai.myndaix.fix-sweep` as an hourly launchd agent (RunAtLoad, gui/501). Same pattern, new plist: `ai.myndaix.controller`. launchd is the non-Claude originator (classifier never runs — legitimate per the [[autonomous-dispatch-classifier]] litmus: it fires on its own without Claude in the picture).
- **BORROW (cautionary):** the Celery-Beat lesson — *multiple* scheduler instances cause duplicate dispatch. Enforce **single-instance** (one launchd agent + an atomic `mkdir` lock, the pattern our watchers already use).
- **REJECT:** an always-on in-process daemon loop (that's openclaw's coupling sin — a fragile always-on loop). A launchd-fired bounded *job per tick* keeps the brain a bounded controller-JOB, not a daemon.

### 3. Change detection — **BUILD thin: poll `git ls-remote` (NOT webhook, for v1)**
Tradeoff research (webhook vs poll): webhook = lower latency + less waste; poll = simpler, no exposed endpoint, no callback/retry logic, no security surface. Jenkins' git-plugin literally polls with `ls-remote` for single-branch detection and calls it the fast path.
- For a local-first runtime with **no bound HTTP listener** and minute-scale review latency being totally acceptable, **poll wins**: webhooks would force us to bind + secure an inbound endpoint (the exact attack surface we've spent two phases hardening *against*). ~Hourly polling of a handful of repos is trivial cost (the 500k-queries/year waste argument is about per-minute polling at scale — irrelevant here).
- **BUILD thin:** `git ls-remote <repo> <ref>` → compare to ledger. Single ref per repo (no wildcards = the fast path).
- **REJECT for v1:** webhook listener (security + reliability surface, no payoff at our scale). Revisit only if latency ever matters.

### 4. Idempotent dispatch / dedup — **BORROW (deterministic key + conditional enqueue); on Postgres, not Redis**
Industry pattern: compute a **deterministic dedup key** from the job's unique params and check-before-enqueue (Redis `SET NX` / DynamoDB conditional write / upsert), with at-least-once delivery + an idempotency layer in the handler.
- Our key is naturally `(repo_id, head_sha)`. The level-triggered design makes dedup *fall out for free*: only dispatch if there is **no open/recent review job for that (repo, sha)**. The "already-queued?" guard **is** the dedup — a SQL `EXISTS` against the ledger under the same tick.
- **BORROW:** deterministic key + conditional enqueue. **REJECT:** Redis / DynamoDB — we have Postgres; adding a second datastore for a dedup key we can express as one `EXISTS` is bloat.

### 5. Bounded execution — **ADOPT existing `submit_job` admission + BORROW iteration cap**
`submit_job` **already** enforces `max_depth` / `max_children` / `cost_budget(root)` / `chain_ttl`, rejecting over-budget jobs to `dead_letter`. That's the `max_steps/cost/ttl` bound the north star asks for — **already built**. The controller adds only a per-tick cap (max N dispatches/tick) + the single-instance lock.
- Agentic-loop prior art confirms the shape: bounded iteration + a **conservative/human fallback** when the budget is exhausted — for us, "do nothing this tick, surface nothing, wait for next tick" is the conservative fallback (no action is always safe here).
- The deterministic-orchestration literature ("who controls execution") explicitly validates the north-star choice: keep **control deterministic** for reliability, embed the agentic subsystem (the review job) as a *subtask*. We are deliberately NOT building an LLM planner at this rung.
- **ADOPT:** existing admission. **REJECT:** a new budget/cost subsystem.

## Summary verdict
| Capability | Verdict | Note |
|---|---|---|
| Control-loop logic | **BORROW** K8s level-triggered + idempotent reconcile | thin Python tick, no framework |
| Periodic trigger | **ADOPT** launchd (fix-sweep pattern) | + single-instance lock |
| Change detection | **BUILD** thin `git ls-remote` poll | reject webhook for v1 |
| Dedup / idempotency | **BORROW** deterministic key + conditional enqueue | on Postgres (`EXISTS`), reject Redis |
| Bounded execution | **ADOPT** existing `submit_job` admission | + per-tick cap |
| LLM planner | **REJECT** at this rung | deterministic by design (north-star) |

## Deliberately NOT building (anti-bloat)
Temporal / Argo Workflows / controller-runtime · LangGraph / CrewAI as the brain · webhook listener · Redis/DynamoDB dedup store · any LLM in the decision path · learning / outcomes-weighting (that's the **next** rung) · auto-merge (the apex rung). Each is a later rung or a rejected dependency, not v1.

## Key design inputs this hands to DESIGN.md
1. The loop is a **level-triggered reconciler**: decide from observed state (ledger + live HEAD), never from an event. Idempotent by construction.
2. **Single-instance** is a hard safety requirement (atomic-mkdir lock), or we double-dispatch.
3. Dedup = `EXISTS` an open/recent review for `(repo_id, head_sha)` — needs a small ledger read surface (and likely a `last_reviewed_sha` source: derive from existing review jobs vs. a new tracking row — DESIGN decision).
4. Trigger is **launchd** (non-Claude); brain is plain Python (`runtime.controller` tick), not a Claude agent.
5. Bounds: reuse `submit_job` admission; add per-tick dispatch cap. Conservative fallback = no-op tick.

## Sources
- [Reconciliation Loop — kubebuilder (DeepWiki)](https://deepwiki.com/kubernetes-sigs/kubebuilder/5.2-reconciliation-loop)
- [Level Triggering and Reconciliation in Kubernetes (HackerNoon)](https://hackernoon.com/level-triggering-and-reconciliation-in-kubernetes-1f17fe30333d)
- [The Principle of Reconciliation (Chainguard)](https://www.chainguard.dev/unchained/the-principle-of-reconciliation)
- [10 Things to Know Before Writing a Kubernetes Controller (Medium / Galletti)](https://medium.com/@gallettilance/10-things-you-should-know-before-writing-a-kubernetes-controller-83de8f86d659)
- [Redis Job Deduplication (OneUptime)](https://oneuptime.com/blog/post/2026-03-31-redis-job-deduplication/view)
- [Configure ArgoCD to Sync on Push, Not Polling (OneUptime)](https://oneuptime.com/blog/post/2026-02-26-argocd-sync-on-push-not-polling/view)
- [Webhooks vs Polling (Nile Bits / Medium)](https://medium.com/@nile.bits/webhooks-vs-polling-431294f5af8a)
- [jenkinsci/git-plugin (ls-remote polling)](https://github.com/jenkinsci/git-plugin)
- [Deterministic AI Orchestration (Praetorian)](https://www.praetorian.com/blog/deterministic-ai-orchestration-a-platform-architecture-for-autonomous-development/)
- [Dynamic Planning vs Static Workflows (Medium / Tao An)](https://tao-hpu.medium.com/dynamic-planning-vs-static-workflows-what-truly-defines-an-ai-agent-b13ca5a2d110)
