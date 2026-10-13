# Auto-merge (docs-only PR-gate) — DESIGN v0.3

_North-star rung 4. v1 = the brain auto-merges **docs-only** PRs behind a hard gate. First removal of the human merge gate. Prior-art: `docs/automerge-research.md`. Reviewed: 34-agent adversarial workflow (`docs/reviews/automerge-design-v0.1-adversarial-workflow.md`) + cross-family v0.2 (codex NEEDS-REVISION, Oracle APPROVE-WITH-FIXES). Status: **hardened; for Jefe plan approval + deploy-prereq provisioning, then build (built code gets its own review).**_

**Decisions locked (Jefe):** safe class = docs-only · target = green human-authored PRs (brain gates+merges, never authors) · OFF by default · revertible · no *code* auto-merge to the runtime · **branch protection on `main` requires the `test` CHECK only (NOT human reviews — so the brain merges on green CI without needing to *approve*, sidestepping the self-approval/bot-identity problem)** · **author allowlist = `redeyefit` only**.

### Core reframe (codex + Oracle converged): the DIFF-CLASS GATE is the security boundary; the LLM review is a QUALITY gate
A PR diff is attacker-controlled text; an LLM can be prompt-injected into emitting "PASS". That is acceptable **only because** the mechanical diff-class gate (§3) has already proven the change is docs-only and not an instruction file — so the worst a forged PASS can merge is a wrong *sentence*, instantly revertible. Security rests on the un-foolable mechanical gate, never on the LLM.

### v0.3 changelog — folded the cross-family v0.2 review
- **Merge mechanic:** prefer REST `PUT /repos/{o}/{r}/pulls/{n}/merge` with `sha:H` (structured `{merged, sha}`) OR `gh pr merge --match-head-commit H`; **merge-queue preflight** via the rules API (`gh api repos/{o}/{r}/rules/branches/main --jq 'any(.[]; .type=="merge_queue")'`) → fail closed if queued (`--match-head-commit` does NOT bypass a queue); after merge assert `{merged:true, sha set}` else leave for human.
- **Pin BOTH base and head:** `M = baseRefOid` (pinned once), `H = headRefOid`, `B = merge-base(M,H)`, and use `B..H` for diff-class AND review AND merge. Re-query `headRefOid==H` (+ `baseRefOid==M`, `CLEAN`/`BEHIND`, no queue, checks green) immediately before merge.
- **`play-review --gate` real CLI contract** (§4): `--gate <repo> <B> <H> <ref> <run-id> <verdict-path>`, runs **inline**, **Oracle REQUIRED** (not best-effort on this path), every abort/canary/skip/contention/timeout → **nonzero**, never writes `done-*`, never fires autofix, writes to a fresh `0700` run dir owned by the tick, and the verdict file is **structured + validated** `{run_id, base:B, head:H, verdict:PASS|NEEDS-FIX}` (not bare `verdict-<H>` — stale/replay-prone).
- **Diff-class precision:** handle **deletions** (`D`: dest mode `000000` + old `100644` = OK — else docs can never be removed); reject `100755` **outright**; `--no-ext-diff`; scrub the git env (like `controller._git_env`); **denylist checks BOTH rename sides**.
- **CI parse:** `gh api --paginate` (the `test` check may be on page 2); match the **exact check identity** (name + workflow/app), reject duplicate ambiguous providers, reject if zero/any-running.
- **`mergeStateStatus` ∈ {CLEAN, BEHIND}** (strict CLEAN rejects PRs main has advanced past — friction with no safety gain; BEHIND still merges).
- **Don't infinite-retry:** record NEEDS-FIX/abort outcomes in `automerge_seen` keyed to `H`; skip until the head changes (saves API budget, the controller-loop lesson).
- **Author-allowlist boundary documented:** a trusted author CAN push third-party commits to their own PR branch — allowlisting `redeyefit` trusts them not to. Known boundary.
- **Reject cross-repo/fork PRs for v1** (a fork head is more-untrusted + the fetch differs).

## 0. PREREQUISITES (Jefe; deploy-blockers)
1. **Dedicated `gh` token for launchd** — the keyring token is unreadable by launchd. Provision a **fine-grained PAT** (NOT classic `repo`) with exactly: *Contents: read/write, Pull requests: read/write, Checks: read, Metadata: read* on `redeyefit/myndaix-runtime`, exported from a `chmod 600` secret into the agent env (`GH_TOKEN`). A **bot identity** is only needed if reviews are ever required (we require the check only, so the author's-or-a-PAT works).
2. **Branch protection on `main`** — require the `test` status check (NOT reviews). The brain merges within it.
3. **Review pipeline under launchd** — the gate runs `play-review --gate` → `mxr` → kilabz (codex) + **oracle (agy, now REQUIRED)** + lobster. All three must auth under the automerge launchd env (agy was just re-authed; codex/claude via HOME). Same env-passthrough discipline as the controller.
4. **Rate-limit preflight** — check `gh api rate_limit`; fail closed below a threshold.

## 1. What it does
Bounded, non-Claude (launchd, hourly) sibling of the controller. Each tick: list open same-repo PRs against `main`; per PR pin `M`/`H`, fetch `refs/pull/<n>/head` into an owned ref + assert `==H`; run the gate; only if ALL pass, merge atomically at `H`. Off by default, capped, revertible.

## 2. The gate (pinned `M`+`H`, range `B..H`; ALL pass else human)
0. **Scope:** PR open, base `main`, **not a fork** (head repo == base repo); author ∈ allowlist.
1. **State:** `mergeStateStatus ∈ {CLEAN, BEHIND}`; base branch has **no merge queue** (rules API).
2. **Docs-only diff-class** (§3) over `B..H`.
3. **CI green for `H`:** `gh api --paginate` check-runs + statuses for commit `H`; the exact `test` check COMPLETED+SUCCESS; reject zero/running/failing/ambiguous.
4. **Bounds:** `AUTOMERGE_ENABLED` flag · per-tick + per-day + per-author caps · not DRY-RUN. **Runs BEFORE the review** (PR #51): the caps are cheap and head+day-determined, so a cap-blocked PR cannot merge this tick regardless of the review — running the paid review first re-ran the full 3-agent review every hourly tick for any over-cap PR (a `None` defer records nothing → never deduped), a spend leak that also made a 2nd same-author docs PR look "stuck" until the UTC day rolled. The terminal-classification gates (docs-class, CI) stay ABOVE the caps so a non-docs / CI-failed PR is still recorded when capped.
5. **Review PASS:** `play-review --gate` (§4) → validated structured verdict `{run_id,B,H,PASS}` produced by THIS tick. Anything else → no merge + record in `automerge_seen`.
6. **Atomic merge:** REST `PUT pulls/{n}/merge {sha:H, merge_method:merge}` (409 if head moved) [or `gh pr merge --match-head-commit H`]; assert `{merged:true}`; on queue/auto-enabled/OPEN → disable-auto, log, human.

## 3. Docs-only diff-class gate (the security boundary; un-gameable)
- **Sole source:** `git diff --raw -z -M --no-ext-diff B..H` (scrubbed env). `gh --json files` forbidden (no rename old-side / modes).
- **Each entry must satisfy:** path ends `.md` (case + homoglyph guarded) on **every** side; mode is `100644` (reject `100755`, `120000` symlink, `160000` gitlink); a delete (`D`, dest `000000`) is OK only if the old path was a `.md` `100644`. Empty changeset → reject.
- **DENYLIST (overrides `.md`, checked on BOTH rename sides → human):** `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `COPILOT*.md`, `.cursorrules`, `CODEOWNERS`, `SECURITY*.md`, `**/rules/*.md`, `**/skills/**/*.md`, `**/prompts/**/*.md`, `**/.agents/**`, `.claude/**`, `.codex/**`, `.cursor/**`, `.github/**` — anything read as live instruction/config. **v1-conservative add:** design/spec/operational docs the team treats as ground truth (`DESIGN.md`, `docs/*-design.md`, `*-spec.md`, `docs/OPERATING.md`) → human until trust is established (this repo's docs are mostly these; auto-merge is deliberately near-inert here at first — we're proving the *machinery* safely, not chasing volume).

## 4. Review gate — `play-review --gate` (new synchronous mode)
A real CLI mode (parsed BEFORE the FRONT/`--worker` branch): `--gate <repo> <B> <H> <ref> <run_id> <verdict_path>`. Runs the review **inline** over explicit `B..H`; **Oracle required**; writes ONLY a structured JSON verdict `{run_id,base,head,verdict}` to a fresh `0700` dir; exit 0 iff verdict==PASS. Canary-fail / diff-cap / contention / oracle-or-pool-down / timeout / injection-uncertain → `NEEDS-FIX` or nonzero (fail-closed). Never writes `done-*`, never fires autofix. The automerge tick validates run_id+B+H match what it asked for (anti-stale/replay). The hook path is byte-unchanged (additive mode).

## 5. Safety pillars (post-hardening)
1. **Un-gameable verify:** the mechanical diff-class gate (§3) IS the security boundary; CI-per-commit + a fail-closed structured review are quality/defense layers; atomic head+base-pinned merge.
2. **Bounded blast radius:** docs-only minus instruction/ground-truth docs · same-repo only · per-tick/day/author caps · uncertain → human · worst forged-PASS case = a revertible sentence.
3. **Instant rollback:** revertible merge commit · flag-off halts · DRY-RUN first · never arm a deferred/queued merge.
4. **Learning deferred:** hard-coded class; log loudly; widen only with the outcomes ledger.
5. **Self-runtime:** docs exempt EXCEPT the instruction/ground-truth denylist; code never auto-merges to the runtime.

## 6. Security surface
Untrusted = PR diff + author (+ third-party commits a trusted author may push — known boundary). Docs can't execute; instruction-doc denylist blocks the read-as-prompt path. Validate all gh/git output; array subprocess, never shell; scrub git env. The merge is the privileged action — fork-rejected, head+base-pinned, queue-refused, MERGED-asserted, capped, flag-gated, fine-grained-PAT-scoped. Trigger = launchd (classifier-clean).

## 7. Components & footprint
- **New:** `src/runtime/automerge.py` (~300 lines: list → fetch+pin → 7-gate → atomic merge → assert); `python -m runtime.automerge tick`; `play-review.sh --gate` mode (additive); `orchestrator/automerge-tick.sh` + plist; `automerge_seen(repo,pr,head,decision,ts)` + migration `0004`; `tests/test_automerge.py` (diff-class adversarial truth-table: symlink/gitlink/rename-both-sides/denylist/mode/delete/homoglyph; CI pagination+identity; merge-queue refusal; fork reject; head/base-mismatch; fail-closed review) + `test.sh` live checks (gh auth+rate from launchd context; e2e DRY-RUN on a throwaway docs PR).
- **Reused:** play-review (`--gate`), controller lock/cap/flag/DRY-RUN + `_git_env` scrub, gh, ledger.
- **Knobs:** `AUTOMERGE_ENABLED` (OFF) · `MAX_AUTOMERGE_PER_TICK` 1 · `_PER_DAY` 3 · `_PER_AUTHOR_DAY` 1 · author allowlist · DRY-RUN · allow/deny lists.
- **Rollback:** `launchctl unload`; `git revert` any merge.

## 8. Status / next
Design hardened through a 34-agent workflow + 2 cross-family rounds; remaining items are implementation-contract precision (verdict freshness, merge-queue/rules API, base pinning, denylist specifics, env prereqs) best verified against real code. **Recommend: proceed to the implementation plan → build → built-code cross-family review**, with the §0 prereqs provisioned by Jefe before the live deploy. Deferred: code/test/lockfile classes, self-fix auto-merge, evidence-based widening (the learning rung).
