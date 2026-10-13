# DESIGN.md — PR-4: autonomous fix stage (v1)

**Status:** DESIGN v0.2 — cross-family reviewed (codex/GPT + Oracle/Gemini), findings folded in, **D5 resolved (honest-minimal v1)**. Drafted while PR #5 (the spine stack) merges. Ready to build on clean `main` once #5 lands; spec→build→re-review pending. Extends `feature/orchestrator-v0` (review-on-push), depends on PR-0b (env-scrub, in #5). Parent design: `docs/phase2-concurrency-design.md` §5/§7. Plan slot: `docs/phase2-plan.md` PR-4.

> **Central correction from review (both families):** "pinned test command + clean checkout + PASS_TO_PASS" is **NOT** a verification — the verifier *executes attacker-influenced repo code* (test files, `conftest.py`, build/install scripts, import hooks). v1 therefore (a) **never emits `PASS`**; the strongest honest verdict is `REGRESSION_CHECK_ONLY`, and (b) the **human diff review + manual apply is the real verification backstop**, not the verify job. The verify job is a *signal*, not a *guarantee*.

---

## 1. What it does and why

After a review returns **NEEDS-FIX**, a **human** triggers a fix run. One `codex` attempt applies a bounded fix in an isolated worktree cut from the **exact reviewed SHA**; the change is captured as an inert, content-hashed `.patch`; a **separate deterministic sandboxed verifier** re-applies it to a clean checkout and runs the repo's tests as a *regression signal*; the verdict + the (sanitized) diff land in the jefe inbox. **Nothing codex writes ever touches a live repo without a human applying the diff.** First safe rung of the north-star self-fixing controller: ONE attempt, human-triggered, human-merged.

## 2. Data flow (input → process → output)

```
NEEDS-FIX verdict (jefe inbox)  ──human trigger──▶  play-fix.sh
   inputs: repo_id, base_ref = FULL reviewed SHA, fix-list (from the review, byte-capped)
        │  repo_id → trusted repo_path resolved from ORCHESTRATOR CONFIG (never a basename guess)
        │  assert base_ref == the reviewed tip recorded in the parent verdict/job
        │
        ├─ canary: codex + verifier reach READY (else abort, no charge)
        │
        ├─ STAGE 1 — FIX  (codex, WORKSPACE_ACTOR)
        │     worktree = git worktree add --detach @ base_ref   (workspace.py:48)
        │     static OBJECTIVE above the fence; fix-list + file-list = nonce-fenced DATA (stdin only,
        │       NEVER reused as path/shell/config/argv)
        │     codex edits ONLY the worktree → worker calls capture_diff() → <wt>.patch
        │     worker records artifact_ref + sha256 + base SHA + producer job id IN THE LEDGER
        │     (empty diff ⇒ fail-closed: "no fix produced")
        │
        ├─ retrieve artifact FROM THE LEDGER by job id (structured), re-validate sha256
        │     (NEVER parse codex stdout for the path — D1)
        │
        ├─ PATCH POLICY GATE (before any execution)
        │     git apply --check --index (no 3-way);  name-status -z path scan
        │     REJECT/DOWNGRADE: symlinks, gitlinks/submodules, exec-bit, binary, control-char names,
        │       .gitmodules/.envrc, AND any edit to tests / test-config / runners / manifests /
        │       lockfiles / build scripts  → verdict TAMPERED or UNVERIFIED (never a clean signal)
        │
        ├─ STAGE 2 — VERIFY  (deterministic SANDBOXED runner, NOT an LLM/responder)
        │     fresh checkout @ base_ref → git clean -fdx → run FAIL_TO_PASS on CLEAN base (must FAIL)
        │     git apply <patch> → [build phase] → run pinned tests
        │     sandbox: scrubbed env, private HOME/XDG/TMPDIR, NO network, resource limits,
        │       writes only to checkout+scratch, process-group cleanup on all exits
        │     gate: FAIL_TO_PASS (by name, must still EXIST) now passes  AND  PASS_TO_PASS holds
        │       AND collected test-id count unchanged
        │
        └─ DELIVER to jefe inbox: verdict ∈ {REGRESSION_CHECK_ONLY | TAMPERED | UNVERIFIED | NO_FIX}
              + sanitized diff + ⚠ flags (test files touched, manifests touched, secrets-scan hits)
              never PASS · never auto-merge · never auto-retry · auto-on-NEEDS-FIX blocked
```

Human reads the diff and, if good, applies it by hand. A human-gated merge *verb* is deferred — v1 = manual `git apply`.

## 3. Components / files

- **`orchestrator/play-fix.sh`** (new) — chain controller, modeled on `play-review.sh` (lock, daily cap, canary, fence, deliver). **Human-triggered, NOT a git hook.** Resolves `repo_id → repo_path` from config; binds `base_ref` to the parent verdict's reviewed tip.
- **deterministic sandboxed verifier** — fixed argv + sandbox policy; on the spine for accounting but **modeled as a verifier/workspace authority, NOT a prompt-interpreting RESPONDER** (D2). Runs the patch-policy gate, the clean-base FAIL_TO_PASS pre-check, build, tests, secrets-scan. If no sandbox is available → `UNVERIFIED` (never silently run unsandboxed).
- **test-command config** — structured **argv with absolute tool paths** (not a shell string), keyed by `repo_id`; default `UNVERIFIED` when absent (D3 — sourcing is the open sub-decision).
- **`src/runtime/cli.py` / ledger** — expose `artifact_ref` (+ sha256, base SHA, producer job id) via a **structured** read (`mxr get <jid>` JSON), never mixed stdout (D1).
- **`src/runtime/registry.py`** — codex already `WORKSPACE_ACTOR` `workspace-write`; add `-c sandbox_workspace_write.network_access=false` (PR-0b follow-up, `registry.py:69`). Per-agent minimal HOME/XDG so neither codex nor verifier can read `~/.myndaix/.secrets` or dotfiles.
- **Tests** — a known-buggy fixture repo (named FAIL_TO_PASS + PASS_TO_PASS); fix yields a clean regression signal from clean base; **test-deletion → TAMPERED** (not green); a patch touching manifests/symlinks/test-config → downgraded; clean-base flake pre-check; artifact hash mismatch → abort; env-scrub + minimal-HOME + file-list-as-DATA-never-argv + resolve-assert-inside-worktree all enforced; fix-list byte-cap fail-closed.

## 4. Edge cases & failure modes

| Case | Behavior |
|---|---|
| codex makes no change (empty diff) | `NO_FIX`, no verify |
| artifact path/hash mismatch (D1 spoof/mutation) | abort — never verify/deliver a diff that doesn't match the recorded hash |
| patch doesn't apply to clean base | `git apply --check` fails → `UNVERIFIED` (stale/wrong base) |
| FAIL_TO_PASS already passes on clean base | flake/no-real-bug → `UNVERIFIED`, rerun/quarantine |
| FAIL_TO_PASS test deleted/renamed by the patch | `TAMPERED` — the named test must still exist |
| patch edits tests/conftest/manifests/lockfiles/build | downgrade to `TAMPERED`/`UNVERIFIED`; ⚠ flag in delivery |
| patch adds symlink/gitlink/exec-bit/binary/.envrc | rejected by patch policy |
| compiled repo, no build phase | build phase explicit; manifest/install change → `UNVERIFIED` |
| no test-command config for repo | `UNVERIFIED` — never a clean signal without a real run |
| no sandbox available | `UNVERIFIED` — never run untrusted code unsandboxed |
| codex/verifier timeout / over-budget | runtime caps; recoverable job id; pgroup cleanup |
| write escapes the worktree | resolve+assert writes inside worktree; sandbox + net=off |
| fix loops / multiple attempts | v1 = ONE attempt; `WORKSPACE_ACTOR` never auto-retried; no auto-on-NEEDS-FIX |
| repo basename collision / wrong repo | `repo_id` (logical) ≠ `repo_path` (trusted, from config) |
| poison parent chain | admission control `MAX_CHILDREN`/`MAX_DEPTH` (postgres_store.py:208) |

## 5. Security surface (this stage lets an AI write code)

- **Untrusted:** the fix-list/file-list (derived from the diff under review — attacker-influenceable), the diff codex emits, **and every file the verifier executes** (tests, conftest, build/install scripts, import hooks, native ext).
- **Injected as DATA, never argv:** static OBJECTIVE above a nonce fence; fix-list/file-list fenced (the `fence()`/nonce pattern, `play-review.sh:106`), **stdin only**, byte-capped, fail-closed over cap, never reused as path/shell/config/argv. *Residual risk (both reviewers):* the fence stops **command** injection, not **semantic** prompt injection — an LLM can still choose to follow embedded instructions. The **human merge gate is the only real mitigation**; the doc does not claim the fence makes generation safe.
- **Artifact integrity (D1, BLOCKER both):** retrieve from the ledger (structured), store + re-validate `sha256` before verify *and* before delivery, keep the patch outside any agent-writable path. Never trust codex stdout for the ref.
- **Verifier containment (BLOCKER both):** the verifier deliberately runs untrusted code, so it needs a hardened runner — scrubbed env, private/empty `HOME`+`XDG`, no network, private `TMPDIR`, resource limits, write only to checkout+scratch, process-group cleanup on all exits. Without it → `UNVERIFIED`.
- **Harness subversion (BLOCKER codex / MAJOR Oracle):** the pinned *entrypoint* doesn't protect the *harness it runs*. Downgrade any patch touching tests/test-config/runners/manifests/lockfiles/build; compare collected test-id counts before/after.
- **Filesystem secrets (MAJOR codex):** env-scrub doesn't stop reading `~/.myndaix/.secrets`. Per-agent minimal HOME/XDG, no general dotfile access; **secret/high-entropy scan** of the patch + agent output before delivery.
- **Never:** auto-merge, auto-trigger shell/merge, auto-on-NEEDS-FIX. `lobster` = dedup/judge gate. Human is the merge authority.

## 6. Open decisions (status after review)

- **D1 — artifact retrieval — RESOLVED.** Read from the ledger (structured `mxr get <jid>` JSON) + sha256 validation; never parse stdout. (Was my wrong call; both families flagged it.)
- **D2 — verify job shape — RESOLVED.** A **deterministic sandboxed verifier with fixed argv**, on the spine for accounting but NOT an LLM/prompt-interpreting RESPONDER.
- **D3 — test-command source — OPEN (sub-decision).** Both agree: structured argv, absolute paths, outside the patched tree, `UNVERIFIED` default. Tension: codex → keep it **outside the repo** (central, drift risk); Oracle → read a **versioned file at the clean `base_ref`** (`.agent/verify.sh`, repo-owned, friction-free) since reading pre-apply means the patch can't alter it. *Lean:* clean-base versioned file **AND** that file is in the protected set (a patch touching it ⇒ downgrade) — gets repo-ownership + integrity. Confirm in spec.
- **D4 — base_ref semantics — RESOLVED+strengthened.** Full-SHA only; bind to parent verdict's reviewed tip; `git apply --check --index`; recorded patch hash; `git clean -fdx` the checkout.
- **D5 — verifier sandbox strength — RESOLVED (Jefe): (B) Honest-minimal v1.** Ship the patch-policy + clean-base regression *signal* under best-effort local containment, but **never claim more than `REGRESSION_CHECK_ONLY`**; the human diff review is the verification. Smallest safe rung, matches "human merge gate is the v1 backstop." Harden to a real local sandbox / Docker (option A) **before** any auto-on-NEEDS-FIX. (Rejected for v1: building the hardened sandbox now; deferring PR-4.)

## 7. Borrows / adopts / does NOT build

- **Borrows:** SWE-bench **FAIL_TO_PASS / PASS_TO_PASS** semantics (run clean base first; named tests; count collected ids) — as a *signal*, honestly labeled.
- **Reuses the spine:** worktree manager + `capture_diff` (diff-back), `parent_id`/`base_ref`/`artifact_ref` chaining, admission control, PR-0b env-scrub, PR-2 per-repo cap, the `play-review.sh` controller skeleton.
- **Does NOT build (deferred):** Docker-per-task, sample-N, architect/editor split, Aider A/B, LISTEN/NOTIFY, full auto loop, auto-on-NEEDS-FIX, any auto-merge verb.

## 8. Build & review plan (follows /feature + new-systems.md)

1. **Spec workflow** (like PR-2): derive the controller + the deterministic-verifier contract + the patch-policy from this design; adversarial attacks (artifact spoof/mutation, harness subversion incl. test-deletion, FS-secret read during verify, escape-the-worktree, patch-policy bypass, empty-diff, flake, UNVERIFIED honesty).
2. **Build:** `play-fix.sh` + sandboxed verifier + patch-policy gate + ledger artifact read (D1) + registry net=false/minimal-HOME + fixture repo + tests. (Sandbox strength per D5.)
3. **Review:** in-session lenses → fire codex + Oracle cross-family again (security-sensitive — PR-2 rigor).
4. **Gate:** fixture-repo test set green incl. the TAMPERED/UNVERIFIED honesty cases; commit → push → human-gated merge. **Never auto-merge.**

## 9. How it was reviewed
v0.1 → cross-family (codex/GPT + Oracle/Gemini) on the design → **converged BLOCKERs** (artifact-from-stdout unsafe, verifier executes untrusted code, harness subversion) + MAJORs (verify-not-a-responder, repo/base binding, patch-policy, build/deps, FAIL_TO_PASS flake, FS secrets) → **v0.2**. Both confirmed the v1 shape (one attempt, clean-checkout, inert diff-back, no auto-retry/merge) is directionally right; the fix is to stop over-claiming `PASS` and harden the verifier path.

## Changelog
**v0.2** — verifier executes untrusted code → never `PASS` (verdict vocab `REGRESSION_CHECK_ONLY`/`TAMPERED`/`UNVERIFIED`/`NO_FIX`); artifact via ledger+sha256 (D1); deterministic sandboxed verifier not a RESPONDER (D2); patch-policy gate (symlink/gitlink/exec/binary/test-config/manifest); clean-base FAIL_TO_PASS pre-check + named-test-exists + test-id-count; build phase; repo_id≠repo_path from config + full-SHA bound to parent verdict (D4); per-agent minimal HOME/XDG + secrets-scan; fix-list byte-cap; semantic-injection residual acknowledged; **D5 scope fork (sandbox-now vs honest-minimal) for Jefe.**
**v0.1** — initial design (flow, spine reuse, security surface, D1–D4).
