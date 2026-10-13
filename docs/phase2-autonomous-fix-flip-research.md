# Prior-art brief: auto-trigger patterns for the autonomous-fix flip

_Mack + Jefe, 2026-06-25. Input to `docs/phase2-autonomous-fix-flip-design.md` (not yet written)._

## Why this brief exists

The autonomous-fix flip is **not a new system** — it bridges two already-merged,
already-reviewed scripts (`play-review.sh` emits a NEEDS-FIX verdict + ordered
fix-list; `play-fix.sh` runs the hardened pool→verify→inbox path). So this is NOT
a full BUILD-vs-ADOPT survey. The one part with real external prior art is the
**auto-trigger mechanism itself**: the moment you let a verdict fire an action with
no human in the loop, you inherit a well-known family of failure modes (self-trigger
loops, duplicate fires, poison retries). This brief catalogs those patterns and gives
a BORROW / REJECT verdict for each under our constraints (local-first, bash-on-Postgres
spine, solo, anti-over-engineering, **NEVER auto-apply/auto-merge**).

## What the field already learned (the failure modes)

### 1. Self-trigger infinite loop — the #1 hazard
**Source:** Renovate #17528 — "Renovate is stuck in an infinite loop overwriting a ci
bot's commits." Bot A's auto-commit triggers bot B, whose commit re-triggers bot A;
neither stops. (https://github.com/renovatebot/renovate/issues/17528)

**Pattern:** an auto-trigger must never fire on state that it (or a peer automation)
just produced. The fix in the wild is "ignore commits authored by self" + content-keyed
idempotency.

**Verdict for us: BORROW the principle, but note we're STRUCTURALLY IMMUNE.**
`play-review.sh` fires on `git push`. `play-fix.sh` writes a **diff to the jefe inbox —
it never commits, never pushes** (that's the honest-minimal contract). So the fix output
*cannot* re-enter the review trigger. The loop the whole industry fights is closed by
construction here. The design doc should state this immunity explicitly so a future
"just have it auto-apply" change is recognized as the thing that would re-open it.

### 2. Idempotency key — fire once per logical event
**Sources:** Hookdeck webhook-idempotency guide
(https://hookdeck.com/webhooks/guides/implement-webhook-idempotency); Stripe-style
idempotency keys (apisyouwonthate.com, zuplo.com). Standard pattern: derive a stable
key from the event, persist a "seen/done" marker, drop duplicates.

**Verdict: BORROW — and we already have the mechanism.** `play-review.sh` writes
`$STATE/done-$tip` keyed on the reviewed SHA; `play-fix.sh` has its own daily counter
and global lock. The bridge needs ONE new marker so the **same NEEDS-FIX verdict can't
fire N fixes** — key it on the reviewed `tip` SHA (e.g. `fix-fired-$tip`), claim-and-lock
via atomic `mkdir`/`: >` exactly like the existing markers. No new infra.

### 3. Debounce / coalesce rapid triggers
**Source:** Trigger.dev debounced task runs — "consolidate multiple triggers into a
single execution with a unique key and delay window."
(https://trigger.dev/changelog/debounced-task-runs)

**Verdict: BORROW the idea, REJECT the machinery.** Rapid pushes to a branch produce
several NEEDS-FIX verdicts. We don't need a delay-window debouncer — **latest-SHA-wins**
falls out of the idempotency marker (each `tip` is distinct; an older verdict whose tip
is no longer branch-head is simply stale). The global single-fix lock in `play-fix.sh`
already serializes execution. A time-window debounce would be over-engineering for a
solo, low-volume repo.

### 4. Poison-retry guard / circuit breaker
**Source:** Mergify merge-queue — "if a batch fails, remove the culprit and continue;"
caps on concurrency. General resilience: bounded retries + backoff so a permanently
failing item can't burn the queue forever.

**Verdict: BORROW — partially present, one gap.** `play-fix.sh` has `DAILY_CAP=20` and a
global lock (one fix at a time) — that bounds blast radius. The gap: a verdict that yields
NO_FIX / UNVERIFIED should **not be retried automatically on the next identical trigger**.
The idempotency marker (#2) closes this too — once a `tip` has fired its one fix attempt,
the outcome (any verdict) marks it done. No auto-retry loop. The human, reading the inbox,
is the retry decision.

### 5. Auto-merge gating (what we deliberately do NOT borrow)
**Sources:** GitHub native auto-merge; Mergify "when the native button is enough"
(https://mergify.com/blog/github-auto-merge-when-native-is-enough); Renovate automerge
docs (https://docs.renovatebot.com/key-concepts/automerge/). All gate auto-merge behind
"required checks pass."

**Verdict: REJECT for v1, by design.** PR-4 has **no PASS verdict** — its strongest signal
is REGRESSION_CHECK_ONLY ("a regression signal, NOT a guarantee"). There is no honest green
to gate an auto-merge on. The flip stays human-apply. This is the load-bearing constraint;
the whole design exists to *trigger* a fix, not to *trust* it.

## The genuinely novel + risky bit (no external prior art)

The one thing the field can't hand us: **deriving the `fail_to_pass` selector from the
review.** The selector is "which existing test proves the bug," and it has to come from
the kilabz/lobster review output — which is **untrusted LLM text**. Two hazards:

- The review may name **no specific failing test** (it found a bug by reading, not by a
  red test). Then there is no honest selector and play-fix caps at UNVERIFIED anyway.
- A selector parsed from LLM text is attacker-influenceable (prompt injection in the diff
  under review). `play-fix.sh` already hard-validates the 4th-arg selector (tracked regular
  file at base, no traversal, no flags, exactly one blob) — so a bad selector fails closed,
  not dangerous. But the **bridge must treat the extracted selector as untrusted** and let
  play-fix's existing gate be the authority, never pre-trust it.

This selector-extraction is the real design question for `phase2-autonomous-fix-flip-design.md`,
and it's where Oracle/codex review will earn their keep — not in the trigger plumbing, which
is solved prior art.

## Borrow / reject summary

| Pattern | Source | Verdict |
|---|---|---|
| Don't fire on self-produced state | Renovate #17528 | BORROW — already structurally immune (fix → inbox, never push) |
| Idempotency key per event | Hookdeck / Stripe | BORROW — new `fix-fired-$tip` marker, reuse existing marker mechanism |
| Debounce rapid triggers | Trigger.dev | BORROW idea / REJECT machinery — latest-SHA-wins + existing lock |
| Poison-retry / circuit breaker | Mergify | BORROW — DAILY_CAP + lock present; marker closes the auto-retry gap |
| Auto-merge on green | GitHub / Mergify / Renovate | REJECT (v1) — no PASS verdict exists; human-apply by design |
| Selector from review text | — (no prior art) | BUILD carefully — treat as untrusted; play-fix's gate is the authority |

## Net for the design doc

Trigger plumbing = solved; borrow the idempotency-marker + ignore-self discipline we
already half-have, add **one** `fix-fired-$tip` marker, and explicitly document the
structural loop-immunity. Spend the design + review budget on the **selector-extraction
contract** (the untrusted-LLM-to-validated-path bridge) and on the **product decision**:
is REGRESSION_CHECK_ONLY a strong enough signal to auto-surface/prioritize, or does every
auto-triggered fix still land as plain human-apply like the manual path? That's the open
question Phase 0 design must answer.
