# DESIGN: Autofix apply rung — verified fixes become branches + PRs

_Mack, 2026-09-11. The follow-on that `docs/phase2-autonomous-fix-flip-design.md` explicitly
deferred ("the selector path … is a documented, separately-flagged follow-on"). Authorized by
Jefe 09-11 ("too safe and slow … I lean on you to decide what's safe to automate — proceed")._

## What — one paragraph

Two changes to `play-fix.sh`, both riding every existing gate. (1) **`SUITE_GREEN` tier:** on
`fail_to_pass:null` repos (exactly the auto-fire class), the sandboxed verify suite now RUNS —
previously this path finished `UNVERIFIED` without executing anything. Green + all
policy/integrity/tamper gates ⇒ `SUITE_GREEN`, a suite-level signal deliberately weaker than
`REGRESSION_CHECK_ONLY` (which stays reserved for a real fail_to_pass proof — codex M1 kept).
(2) **Apply rung:** when `$ORCH/AUTOFIX_APPLY_ENABLED` exists and the verdict is `SUITE_GREEN`
or `REGRESSION_CHECK_ONLY`, the hash-verified immutable patch is applied in a PRISTINE worktree
from `base_sha`, committed to `fix/auto/<runid>`, pushed, and a PR is opened (`gh`; fail-open to
branch-only). The push fires the normal pre-push review loop on the fix branch. **Merge is not
touched** — it stays with the automerge gate / rung-1 human policy.

## Why

The v1 flip proved the loop can draft correct fixes (first live fire, 20260911080134: right
diff, delivered as look-don't-touch and outrun by a human doing the same fix). The bottleneck
moved from "can it fix" to "does anyone promote the fix." This rung closes review→fix→re-review
autonomously while merge stays gated. Blast radius: a branch — revertible in one command, on a
protected-main repo.

## Attack pass (pre-build, on the planned mechanics — findings folded)

1. **Never commit the executed tree.** The verify worktree ran untrusted patched code. The
   commit is built in a fresh worktree from base + the immutable `$patch` (sha re-verified at
   apply time). Post-execution integrity checks remain advisory-only for the commit path.
2. **Recursion.** Loop-immunity previously rested on "play-fix never pushes" — no longer true.
   `autofix_fire` now hard-skips refs matching `fix/auto/*` (a review of an autofix branch can
   never fire another fix). Belt: branch-exists check refuses a duplicate `fix/auto/<runid>`.
3. **Verdict gating.** The apply call sites exist ONLY after the final tamper gate —
   `TAMPERED`/`UNVERIFIED`/`NO_FIX`/`ABORTED` exit via `finish`/`fail_closed` earlier, so no
   flag can make them apply. Tested (case 32).
4. **Detached-context push.** play-fix runs under `env -i` (no `SSH_AUTH_SOCK`). Push failure is
   fail-open: branch committed locally, inbox note carries the one-line manual push. `gh` PR
   creation is best-effort and skipped under test mode.
5. **Lock hygiene.** Push/gh spawn hook workers — both run `9>&-` so no descendant pins the
   fd-held fix lock (the r5-series regression class).
6. **Mini.** Unchanged: controller/automerge set `PLAY_DISABLE_AUTOFIX=1`, so the apply rung is
   MacBook/pre-push only until deliberately widened.

## Arming (deliberate, per machine)

Ships disarmed. After merge + `cp` of BOTH play scripts to `$ORCH` (deploy-sync surface):
`touch ~/.myndaix/orchestrator/AUTOFIX_APPLY_ENABLED`. Disarm = `rm`.

## Tests

`orchestrator/test-fix.sh` cases 28–33: tier behavior (green/fail/tamper in suite-green mode),
disarmed = nothing pushed, armed = branch pushed with exact verified content, TAMPERED never
applies, full-proof tier applies. 38 checks green; bash-check PASS.

## Deliberately NOT built

Auto-merge of fix PRs (needs its own automerge-scope review), Mini arming, per-fix selector
wiring on the auto path, any change to the fixer prompt or trust ceilings.
