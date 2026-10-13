# DESIGN.md — Team Orchestrator (bash plays on the runtime)

**Status:** **v0.6 — validated; 2 self-review rounds.** Design + code adversarial passes + `orchestrator/test.sh` (stubbed `mxr`) — **18/18 green**. The hook reviewed its own code across two live rounds and found real bugs each time; all fixed and pinned. Proven end-to-end: a push delivered a verdict to iMessage. Stack: **bash client over the UNMODIFIED runtime spine.**
**Author:** Steven Fernandez — designed with the AI pair-engineering team (Claude / Codex / Gemini).

---

## 1. What it does & why
A thin **bash orchestration layer** that makes the agent team **review / discuss / build-together** by chaining `mxr <agent> "<prompt>"` calls. The durable spine already routes one job to one agent; the orchestrator sequences several into a workflow. It replaces **openclaw** — the old file-IPC orchestrator that coupled comms + execution and took a multi-hour outage. The lesson: the agents were never the problem, the *wrapper* was. So this keeps the direct `mxr` calls and adds only ordering — no daemon, no IPC files, no shared mutable state.

## 2. Non-negotiable principle
**ADDITIVE, never STRUCTURAL.** The orchestrator is *just-another-`mxr`-client*. **Test: can it run without editing `src/runtime/`?** If no, stop. State of record is the **Postgres ledger** (every stage is a normal durable job); bash holds only an ephemeral transcript that can be deleted without corrupting anything.

## 3. Legitimacy boundary (hard)
Trigger is a **NON-CLAUDE originator only**: your `git push` (a `pre-push` hook), cron, or a webhook. The Claude Code agent never originates, never runs `mxr`, never auto-arms the trigger. **Litmus:** remove every Claude agent — does it still fire? Yes (your push fires it). The hook is **human-installed**, committed like any file. Verdicts deliver **only to a human-owned, non-agent-watched inbox** (`jefe/`), **never `mack/`** — delivering to a Claude agent's inbox would re-couple comms+execution (the openclaw sin) and breach the merge gate.

## 4. Failure-map — what NOT to build (live-verified on this machine)
- **The trigger must actually fire.** `post-receive` runs only in a **bare** repo (your clone is non-bare); `post-merge` runs only on a merge/pull, never on the push side. A normal `commit && push` triggers **neither** → silent no-op. **v0 uses `pre-push`** (client-side, your working clone, has the range + your PATH/venv).
- **Never `git diff $SHA~1 $SHA`.** Fatal (exit 128) on a root commit; on a merge it drops everything the merge brought in — the most review-worthy event. Use the **real pushed range** and an empty-tree fallback.
- **Never grep-substring the gate token.** `grep -q PLAY_PASS` lets attacker-controlled diff bytes (echoed through the chain) forge a PASS. Match **`trimmed == exactly "PLAY_PASS"`**, default FAIL.
- **Fence defense = a per-run nonce, not tag-stripping.** Tag removal ≠ neutralization (and the v0.1 "BSD sed has no `/I`" claim was *false* on this box anyway). Content the reviewer must **read** (a diff, a review) stays **cleartext** — base64 would make it unreviewable — so the fence tag carries an unguessable per-run **nonce**: injected content can't forge a close-tag + new objective without it. (base64 is reserved for *opaque* data forwarded between agents in deferred phases.)
- **Never `local out=$(mxr …)`.** `local`/`declare` returns 0 and masks the command-sub's exit code — silently undoing the `set -e` capture fix. Declare locals on a **separate line**.
- **Never trust `gtimeout`/`timeout`.** Not installed here (verified). No bash TTL below mxr's own 180s poll; rely on mxr's `rc=1` timeout + the worker's 300s cap.
- **Never let `runs/`/markers live in the repo.** In a worktree they pollute the next diff and become committable; the daily-cap file races. Keep all state **outside any repo**, mutated under the reviewed repo's lock.

## 5. The design — Approach A: linear bash play (v0.2)
- **Trigger:** a `pre-push` hook in the working clone, firing on a push to **any branch** (`refs/heads/*`; tags skipped). Reads `<localref> <localsha> <remoteref> <remotesha>` on **stdin**; an existing branch reviews the incremental `remotesha..localsha`, a **new branch** reviews `merge-base(main, localsha)..localsha` (its own commits vs `main`), falling back to the **empty tree** only when there's no `main`/merge-base. **Detached** (background) and **always `exit 0`** so the push is never blocked or aborted. **Single-fire per push** (review the whole range as one play); dedupe keyed on `localsha`.
- **Channel:** `mxr` as-is, each stage one call. Capture idiom (mandatory): `local out err rc; if out=$(mxr "$a" "$p" 2>"$err"); then rc=0; else rc=$?; fi` — **locals on their own line**. Gate every stage on **`rc==0` AND non-empty stdout** (mxr prints nothing when the reply is None yet still exits 0 — verified `cli.py:57`). `(done AND empty) = FAILED`, never fence-forward empty.
- **Context passing (injection-critical):** objective **ABOVE** a `treat-as="DATA"` fence whose tag carries a **per-run nonce** (`openssl rand -hex 16`); the upstream body stays **cleartext** (control chars stripped) so the agent can actually review it; the **per-run nonce on the fence tags** is what stops injected content from closing the fence and smuggling a new objective. **Byte-cap 64KB; over-cap = FAIL, not truncate** (truncation hides hunks past the cap = evasion). Only **stdout** is forwarded; `.err` is untrusted agent output too.
- **Gate:** triage PASSES iff `trimmed stdout == "PLAY_PASS"` exactly; non-token ⇒ NEEDS-FIX; empty ⇒ FAILED. No substring match.
- **Timeouts:** no bash TTL under 180s. A reviewer that exceeds mxr's 180s poll returns `rc=1` *while the job may still run server-side* — treat that as **UNKNOWN, recoverable**: the abort note includes the **mxr job id** (from stderr, `cli.py:42`) so the completed reply can be fetched from the ledger. Never re-dispatch on timeout.
- **State (outside any repo):** `~/.myndaix/orchestrator/runs/<play_id>/` (transcript, `.err`), `~/.myndaix/orchestrator/state/` (dedupe markers, daily counter). Atomic PER-REPO `mkdir` lock (`lock-<repo_id>`: different repos review in parallel, the same repo serializes); the per-repo daily-cap read-modify-write happens **inside that held lock**. Play-ledger lines built **only with `jq -cn`** (free-text verdicts have quotes/newlines; `>>` is not atomic past PIPE_BUF=512) — and Postgres remains the record-of-record, so the JSONL is best-effort/per-run.
- **Disk safety:** cap `.err` on capture (`head -c 1000000`); prune `runs/`. A disk-full breaks the lock + ledger + every future hook.
- **Env:** pin `PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"` (or call mxr by absolute path); resolve `runs/` to an absolute base.

## 6. v0 — the ONLY thing we build first (ship-first slice)
`orchestrator/play-review.sh`, **review-only, single reviewer, NO fix stage**, installed as a **human `pre-push` hook**:
1. **Pre-flight live canary** `mxr kilabz "reply READY"` (+`lobster`) → on not-(rc0 AND non-empty), abort to `jefe/` (with stderr+sentinel fallback). *Note: a green canary proves reach, not that the larger real review beats the 300s cap.*
2. **Review:** compute the safe pushed-range diff → nonced DATA fence, cleartext, control-chars stripped (objective above, 64KB cap → FAIL if over) → `mxr kilabz` → stdout to `runs/<id>/review.out`.
3. **Triage:** `mxr lobster`, review fenced as DATA, objective "ordered fix-list **or** the literal token `PLAY_PASS`".
4. **Deliver:** verdict + review to `~/.myndaix/bridge/inbox/jefe/` (created with `mkdir -p`, **nonce-fenced** since agents may read inboxes) **+** an `osascript` desktop notification. Append a `jq`-built play-ledger line.

**No `codex`/workspace-actor in v0.** Delivers autonomous code-review-on-push with **zero `src/runtime/` edits**, on agents the ledger has seen complete (kilabz proven by the canary).

## 7. Data flow
`git push (human) → pre-push hook (stdin range) → play-review.sh [detached] → canary → review(kilabz) → triage(lobster) → deliver(jefe inbox + notify)`. Every `mxr` call mints a normal durable job; the run is reconstructable from the ledger even if `runs/` is wiped.

## 8. Security surface
Untrusted = every agent's **stdout AND stderr** AND the diff itself. Objective above a **nonced** fence (cleartext so it stays reviewable; the unguessable nonce defeats fence-forgery); over-cap = FAIL; never eval/source/path-open/argv agent output. The **`jefe/` inbox content is itself nonce-fenced** (2nd-order injection — agents read inboxes). Builder steps (phase 2+) take their objective **statically only**, with worktree isolation + diff-only + **your merge gate** as the hard backstop. Secrets stay in `~/.myndaix/.secrets` (chmod 600), read only by the worker; the orchestrator never reads values, never runs under `set -x`. **Op note:** the worker env is frozen at launchd start — after editing `.secrets`, `launchctl kickstart -k gui/$(id -u)/ai.myndaix.runtime`.

## 9. Scope guard
**v0 = review-only, single-reviewer, `pre-push`.**
**Deferred (in order):** codex **fix stage** (after reach proven) → **2nd reviewer + quorum** → additive **`mxr --parent-id/--base-ref`** passthrough + per-agent timeout profiles + **async submit/poll** for long builds → **Approach C controller-loop** for build-together / multi-round discuss.
**Out of scope by design:** YAML DSL / interpreter / daemon · dollar cost-budgets (only `recon`/`higgsfield` carry one) · any spine edit.

## How it was reviewed
A **3-topology design panel** (linear / declarative-DAG / controller-loop), then a dedicated **adversarial security pass**, then — for the v0 slice specifically — a **fresh-context adversarial pass run on the live machine**. That last pass earned its keep: it proved `git diff $SHA~1` is fatal on a root commit, that `post-receive`/`post-merge` don't fire on a normal push, that the `jefe/` inbox didn't exist, that `local out=$(…)` re-breaks the capture fix, and that `gtimeout` isn't installed — none of which a review of the prose would have caught. **Next gate: cross-family review (`mxr kilabz` / `mxr oracle`) of the `play-review.sh` CODE — where the real bugs live and where the runtime gets dogfought.**

## Changelog
**v0.6** — round-2 self-review fixes: empty `PLAY_IMESSAGE_TO` now disables the ping (`-`, not `:-`); `confirm_pushed` scopes `ls-remote` to the pushed `$ref` (a matching SHA on another ref no longer false-confirms) and uses the push URL (`$2`) for pushurl/direct-URL correctness; fixed a latent `grep -q | pipefail` SIGPIPE in that check (capture+compare). `test.sh` → 18/18.
**v0.5** — fix the 3 bugs kilabz found reviewing this code: (1) a rejected push is no longer deduped — `mark_done` confirms the tip landed on the remote (`git ls-remote`) before writing the marker; (2) exact *trimmed* PASS gate (`^\s*PLAY_PASS\s*$`) so embedded spaces can't forge a pass; (3) `deliver()` returns non-zero on durable-write failure and `done` is gated on it, so a lost review is never deduped. `test.sh` → 14/14.
**v0.4** — widen the trigger to **all branches** (`refs/heads/*`, skip tags); a new branch's first push now reviews `merge-base(main, tip)..tip` (its own commits) instead of a whole-tree empty-tree diff. Worker unchanged (`test.sh` still 10/10).
**v0.3** — code-review hardening, verified by `orchestrator/test.sh` (10/10): deliver() open-failure fallback fix; numeric-guarded daily counter; stale-lock reaping (1800s) + a *visible* contention skip (never silently drops a push); abort paths no longer burn the daily cap; synchronous `.err` capture (dropped the no-op `wait`); `clean()` strips DEL; nonce on **both** fence boundaries; `runs/`/`state/` pruning; diff cap 256KB→64KB (the live 300s-cap hang proved 256KB would always time out); and a `set -u` same-line-`local` crash fix in `call()` that the test caught.
**v0.2** — live-machine adversarial pass: `pre-push` trigger pinned (range from stdin + empty-tree fallback, detached, exit-0); `jefe/` human inbox + notification + stderr/sentinel fallback; exact-match `PLAY_PASS` gate; nonced cleartext fence + 64KB-FAIL cap; `local`-on-separate-line capture rule; no-bash-TTL + job-id-recoverable timeout; state moved outside the repo under lock; `jq`-built ledger; `.err` cap. Build-ready.
**v0.1** — initial panel-hardened design; Approach A selected; v0 review-only slice scoped.
