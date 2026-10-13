# DESIGN: Autonomous-fix flip (Phase 2)

_Mack + Jefe, 2026-06-25. Prior-art input: `docs/phase2-autonomous-fix-flip-research.md`._

_Process: synthesized via a judge-panel + 2 adversarial Claude critics (workflow), then
cross-family reviewed by **codex** (read-the-repo) and **Oracle/Gemini** (inlined). Both returned
NEEDS-REVISION with **complementary** blockers; all findings folded below. The headline Oracle
BLOCKER (shared-`.git` write vector) was **empirically tested and CLOSED for the production
config** — see Pre-ship gates §1 and `orchestrator/probe-git-write-vector.sh`._

## What — one paragraph
Flip the existing **manual** `play-fix.sh` invocation into an **opt-in auto-trigger** by adding a
small, fail-soft block to `play-review.sh`'s existing NEEDS-FIX else-branch (`play-review.sh:202-208`).
When `PLAY_AUTOFIX=1`, after the NEEDS-FIX review is durably delivered and the push is confirmed
landed, the branch writes the lobster fix-list (`$triage`) to the worker's own run dir and
`nohup`-**detaches** `play-fix.sh` with three args `(repo_id=$repo_id, base_sha=$tip,
fixlist=$run/fixlist.txt)` — **no 4th-arg selector in v1**. Zero new scripts, **zero changes to
`play-fix.sh`**, zero changes to the lobster prompt. The fix runs under play-fix's own lock/cap,
caps at **UNVERIFIED** (`play-fix.sh:318`), and lands as a sanitized, policy-screened, human-apply
diff in the inbox — byte-identical in actionability to a human typing the three args today. **The
flip changes only WHO invokes play-fix, never how much the fix is trusted.** It fires only on repos
configured `fail_to_pass:null`, only through a **trusted installed** play-fix copy, and only with a
runtime-asserted `base_sha==$tip` — all enforced fail-closed. The selector path (which unlocks
`REGRESSION_CHECK_ONLY`) is a documented, separately-flagged follow-on — not built here.

## Why — problem it solves, what fails without it
Today a NEEDS-FIX verdict requires the operator to hand-type
`play-fix.sh <repo_id> <40-hex-sha> <fixlist-file>`: locate the reviewed SHA, dump triage to a file,
run the tool. Every fix is a manual round-trip, so fixes get deferred and the review→fix loop never
closes autonomously. The flip removes that keystroke for the common case while preserving every
safety property of the manual path. It deliberately does **not** make fixes more trustworthy —
**UNVERIFIED in, UNVERIFIED out** — because the honest value is "pre-draft the fix the operator
would have requested anyway," not "trust the machine."

## Data Flow — input → process → output
1. **Trigger source (existing):** push → pre-push hook → front detaches the worker (`:63`) → canary →
   kilabz review (`$review`, untrusted LLM) → lobster triage (`$triage`, untrusted LLM = the ordered
   fix-list) → PASS gate (`:196`). NEEDS-FIX is the **else** branch (`:202-208`). In scope: `$repo_id`
   (`:72`), `$tip` (reviewed 40-hex SHA, `:71`), `$base` (range lower bound / EMPTY_TREE sentinel —
   **never** the fix base), `$ref`, `$run` (`:75`), `$triage`, `$review`, the global review lock
   `$STATE/lock` (held `:143`→EXIT trap `:153`).
2. **Capture `confirm_pushed` ONCE, `set -e`-safe (codex MAJOR):**
   `if confirm_pushed; then pushed=1; else pushed=0; fi` BEFORE `mark_done`; reuse `$pushed` for both
   the done-marker and the fire gate. A bare `confirm_pushed; rc=$?` would abort under `set -e` on the
   expected-false path. **Never call `git ls-remote` twice** — a second call inside the held review lock
   would risk wedging ALL reviews until the 1800s STALE reap. `mark_done` is refactored to consume
   `$pushed`. The single `ls-remote` (`:127`) is additionally wrapped in a **portable timeout**
   (`gtimeout` if present, else a bg-sleep-kill mirror of play-fix's pattern) so even the one call can't
   hang the lock (Oracle MAJOR — pre-existing risk, hardened here).
3. **Deliver the review first, `set -e`-safe:**
   `if deliver "review NEEDS-FIX — $ref" "<body>"; then delivered=1; else delivered=0; fi` (`:85-102`).
   The fire path runs only when `delivered=1`.
4. **Body always carries a manual hint; the auto note is NEUTRAL (codex MAJOR ordering fix):** the
   NEEDS-FIX body always ends with a copy-paste `play-fix.sh "$repo_id" "$tip" "$run/fixlist.txt"` hint
   (valid for `PRUNE_DAYS=14`; re-push to regenerate). When `PLAY_AUTOFIX=1`, the body adds a **neutral
   conditional** note — *"if armed and eligible, an auto-fix attempt will follow as a SEPARATE inbox
   file (no extra ping)."* — NOT "is running" (we deliver before the gates resolve, so we cannot claim it
   launched). This closes the armed-but-suppressed gap (every suppression path still leaves a working
   manual fallback) and sets honest expectations for the silent second file (play-fix has **no** notifier).
5. **Write the fix-list file (always, in NEEDS-FIX):**
   `printf '%s' "$triage" > "$run/fixlist.txt" 2>/dev/null || true` (NOT `&&` — under `set -e` a
   redirection failure must not abort the already-delivered worker).
6. **Gate the fire — ALL conditions required (fail-closed):** fire iff —
   `PLAY_AUTOFIX=1` **AND** `delivered=1` **AND** `pushed=1`
   **AND** the `fail_to_pass:null` check passes (step 7)
   **AND** the trusted fixer resolves + is executable (step 8)
   **AND** the runtime base assertion passes (step 9)
   **AND** `fixlist.txt` is non-empty. Any false condition → no fire; the manual hint is the fallback.
7. **`fail_to_pass:null` enforcement — `jq -e`, genuinely fail-closed (codex BLOCKER):**
   `jq -e --arg r "$repo_id" 'has($r) and (.[$r] | has("fail_to_pass")) and (.[$r].fail_to_pass == null)' "$REPOS_JSON" >/dev/null 2>&1`
   — the earlier `.[$r].fail_to_pass // "null"` was **fail-OPEN** (missing key, missing field, JSON
   `false`, and string `"null"` all passed). This form fires only when the repo key exists, has the
   field, and it is explicit JSON `null`. Rationale: `play-fix.sh:158` reads `.fail_to_pass`
   **independently** of the 4th-arg selector, so a static array would let a 3-arg auto-fire sail past the
   UNVERIFIED ceiling (`:318`) into precheck/verify and reach `REGRESSION_CHECK_ONLY`/`TAMPERED` — an
   autonomous high-trust outcome no human chose. Enforcing null makes "UNVERIFIED in/out" true by
   construction. (Decision: keep this gate; do NOT add a `--max-verdict` flag to the frozen play-fix.sh
   for v1 — see Product decision.)
8. **Resolve fixer — TRUSTED INSTALL ONLY on the auto path (codex MAJOR + Oracle):**
   production auto path uses **`$ORCH/play-fix.sh` only**; if not executable → suppress fire. **No
   fallback to `$repo/orchestrator/play-fix.sh`** — an attacker-controlled push modifies exactly that
   worktree copy, and `play-fix.sh` is the thing that *builds* the sandbox (`:93-125`), so running an
   attacker copy is unsandboxed RCE. `PLAY_FIX_SELF` is honored **only under a test-mode flag**
   (`PLAY_AUTOFIX_TEST_MODE=1`), mirroring play-fix's own `MYNDAIX_FIX_TEST_MODE` double-gate, and is
   rejected if it canonicalizes under `$repo`. Deploy gate (pre-ship §2) shasum-pins `$ORCH/play-fix.sh`
   to the reviewed `orchestrator/play-fix.sh`.
9. **Runtime base assertion, then detach with a whitelisted env (codex MAJOR + Oracle MINOR):**
   `fix_base="$tip"; [[ "$fix_base" == "$tip" && "$fix_base" != "$base" ]] || <suppress fire>` — for an
   **incremental push `$base` is also a real commit**, so play-fix's existence gates (`:142-143`) do NOT
   distinguish it from `$tip`; a mis-wire would silently fix the wrong base. The assertion + the
   inline comment + the test (arg2==$tip) together guard this load-bearing wire. Then:
   `nohup env -i PATH="$PATH" HOME="$HOME" "$fixer" "$repo_id" "$fix_base" "$run/fixlist.txt" </dev/null >/dev/null 2>&1 &`.
   The **`env -i` whitelist** (Oracle MINOR) is strictly safer than a 2-var blacklist — it strips
   `LD_PRELOAD`, `BASH_ENV`, `GIT_EXTERNAL_DIFF`, the `MYNDAIX_FIX_TEST_MODE`/`_PATCH_OVERRIDE` test-seam,
   and everything else inherited through `nohup` — passing only what play-fix needs (it self-establishes
   the rest at `:20/:251-252`). stdin←`/dev/null` avoids the recorded agy/codex inherited-stdin hang.
10. **Output (unchanged play-fix contract):** detached play-fix takes its own `fix-state/lock`
    (`:188-192`), charges its own `PLAY_FIX_DAILY_CAP` (`:28/:236`), self-establishes PATH + codex-pool
    auth (`:20/:251-252`), runs codex (builder net=false, see §Pre-ship) + sandboxed verify, and writes a
    sanitized diff + one verdict line (`NO_FIX|UNVERIFIED|TAMPERED|REGRESSION_CHECK_ONLY|ABORTED`) to the
    jefe inbox (`:55-64`). With three args + `fail_to_pass:null`, it finishes **UNVERIFIED** before verify
    runs (`:318`). It **never commits, never pushes.**

## Idempotency & loop-immunity
**No new marker.** The earlier draft's `fix-fired-$tip` is **dropped** as redundant with `done-$tip`
(both Claude critics + codex agreed). Mechanism: the worker checks `done-$tip` at `:160` (after
acquiring `state/lock`) and exits early on a re-review; `mark_done` writes `done-$tip` iff `$pushed`;
the fire gate requires the same captured `$pushed`. So the fire and the done-marker share one condition,
and the global review lock (`:143-153`) serializes workers — a re-pushed identical tip hits the `:160`
early-exit (or `contention()`) and cannot double-fire. Latest-SHA-wins falls out for free.

**Loop immunity — proven past play-fix's own lines, then EMPIRICALLY verified.** play-fix's only durable
output is the jefe inbox markdown (`:55-64`; INBOX is human-only — *no agent watcher may ever be added
there*, a load-bearing invariant). play-fix itself never commits/pushes, and play-review fires only on
push. The remaining hazard (Oracle BLOCKER): play-fix delegates code generation to **codex running in a
linked worktree** (`workspace.py:56`) that shares the live repo's `.git` (config + hooks), so an injected
fix-list could try to plant `.git/hooks/pre-push` or set `core.sshCommand` → RCE on the operator's next
git action, bypassing `net=false`. **This was tested** (`orchestrator/probe-git-write-vector.sh`): codex's
`workspace-write` seatbelt confines writes to `{workdir, /tmp, $TMPDIR}` and **denied** all three
shared-`.git` writes (`Operation not permitted`) when the repo lives outside `/tmp` — which is the
production case (`~/code/active/myndaix-runtime/.git`). So the vector is **closed for the production
config**, with one precise condition promoted to a pre-ship gate: **the armed repo must not be under
`/tmp` or `$TMPDIR`**. Any future change that auto-commits/auto-applies, gives the codex builder network,
or relocates a repo under `/tmp`, re-opens the loop and is disqualified.

## Selector contract — v1 derives NO selector
The bridge passes exactly 3 args. The lobster triage prompt (`:190`) emits free-text prose plus the
exact-match `PLAY_PASS` sentinel — no parseable selector token exists, so the minimal change parses
nothing. With no selector + `fail_to_pass:null`, play-fix caps at **UNVERIFIED** (`:317-318`). This
forfeits only `REGRESSION_CHECK_ONLY`, which "found-by-reading" findings cannot reach anyway (play-fix is
forbidden from authoring a red test, TAMPER_RE `:34/:308/:349`).

**Follow-on (NOT built; pointer):** a separately-flagged `PLAY_AUTOFIX_SELECTOR` could, when lobster names
an existing already-red test, emit one `PLAY_F2P: <path>` line, parse it behind a testable boundary
(anchored first-match regex; note play-review's `clean()` strips C0 but **not** CR `\015`, so the
extractor must strip CR itself), strip it from body+fixlist, and pass it as a single positional 4th arg.
play-fix's existing 11-check gate (`:164-185`) is the sole authority; worst case a valid selector names
another existing test → still UNVERIFIED via the fail-on-clean-base precheck (`:324-326`). Ships only
after the safe path is proven, with an offline injection-test harness.

## Product decision — REGRESSION_CHECK_ONLY surfacing + no-selector auto-trigger
**(1) REGRESSION_CHECK_ONLY stays plain human-apply — surface, never auto-action.** Unanimous (3 judges).
It must NOT auto-apply/auto-merge (disqualifying; play-fix has no PASS verdict and labels it "a regression
signal, NOT a guarantee," `:357`). Surfacing is free: play-fix stamps the verdict into the inbox filename +
markdown subject (`:56-58`) — the verdict line IS the sort key. **No ranker/queue/priority layer.**
**Honest caveat:** play-fix has **no** notifier (unlike play-review's `deliver`, `:95-100`) — the fix
verdict lands as a *silent* second inbox file; the NEEDS-FIX review (which *does* ping) pre-announces it
(step 4). In v1 this verdict is unreachable anyway (no selector + enforced null).

**(2) Fire on every NEEDS-FIX, default-OFF (`PLAY_AUTOFIX=0`).** Unanimous. Even at UNVERIFIED, play-fix
pre-drafts a policy-gated, secret-scanned candidate diff — the faithful realization of "change WHO invokes
play-fix." Honest because UNVERIFIED equals the manual 3-arg outcome. Over-firing is resolved by
operator-gating (off-by-default), not by refusing to fire.

**Decision on Oracle's `--max-verdict` proposal — DEFERRED (keep `fail_to_pass:null` gate).** Oracle argued
the null requirement excludes repos that use a static `fail_to_pass` for high-trust *manual* fixes, and
proposed a `--max-verdict=UNVERIFIED` flag on play-fix. Rejected for v1: it adds a new code path through the
most security-critical, most-reviewed file for **zero current benefit** (the only armed repo,
myndaix-runtime, is already `fail_to_pass:null`); excluded repos can still be fixed manually. Recorded as
the deliberate universalizer to build *if/when* a static-`fail_to_pass` repo is ever onboarded to auto-fire.
A future operator-configurable `PLAY_FIX_AUTO=selector|all|off` mode is likewise deferred.

## Edge Cases
- **`$triage` > MAX_FIXLIST (64KB):** play-fix fails closed → ABORTED (`:145`); review delivered; no
  re-fire (done-$tip). Never truncation.
- **Rapid pushes of distinct tips:** each tip fires once; play-fix's `fix-state/lock` serializes the
  fixes, so a 2nd concurrent fire ABORTS "another fix is running" (`:189-191`). A stale earlier tip's fix
  may finish while the latest ABORTs — operator re-runs the latest via the hint. No debouncer.
- **`$pushed=0` (remote rejected / ls-remote timed out):** no fire; `done-$tip` not written; re-push
  re-evaluates. Manual hint still delivered.
- **`delivered=0` (inbox write failed `:91`):** fire suppressed; worker doesn't mark done; retries next push.
- **`fail_to_pass` non-null OR repo not in repos.json:** fire suppressed (step 7, `jq -e` fail-closed);
  review + manual hint delivered. No wrong-repo fire and no silent ceiling-lift.
- **No trusted `$ORCH/play-fix.sh`:** auto fire suppressed (step 8); review + hint delivered. The auto
  path NEVER execs the worktree copy.
- **`base != tip` somehow (mis-wire/refactor):** runtime assertion suppresses fire (step 9).
- **`PLAY_FIX_DAILY_CAP` (20) exhausted:** extra fires → ABORTED (`:236`), fail-safe. Shared with manual.
- **Marker pruned at 14 days then same tip re-reviewed:** one duplicate fire (codex attempt on an old SHA).
  Rare, fail-safe, wasted compute only.
- **Crash mid-fire (after done-marker, before play-fix output):** that tip is "done" until the 14-day
  prune; recover via empty-commit re-push → new tip. Two-phase claim deliberately not built (solo machine).

## Security Surface
- **Untrusted `$triage`:** written verbatim to `$run/fixlist.txt`, passed only as a **file-path arg** —
  never parsed/interpolated by the bridge in v1. play-fix re-reads it as DATA, MAX_FIXLIST fail-closed,
  nonce-fenced to codex (`:91/:257-260`).
- **`jq -e` fail-closed gate** (step 7) closes the fail-open config hole.
- **Trusted-install-only fixer + `env -i` whitelist** (steps 8-9) close the attacker-worktree-copy and
  inherited-env vectors on the unsandboxed auto path.
- **Runtime base assertion** (step 9) closes the wrong-base wire.
- **Marker/dedup filenames** embed `$tip`, re-validated 40-hex by play-fix (`:142`) — no shell metachars.
- **`repo_id`** is a basename lookup key; the worktree path comes from trusted repos.json `.path`; a
  mismatch fails closed (`:138-140`).
- **`PLAY_AUTOFIX` default `0`:** installing the updated play-review.sh spends nothing; arming is explicit;
  rollback = unset one env knob.
- **Bounded cost-DoS (accepted):** forced-NEEDS-FIX pushes can drive ≤`PLAY_FIX_DAILY_CAP=20` codex
  submits/day; bounded, fail-safe, off-by-default. `$STATE` is outside the repo (no marker pre-seeding).
- **Exfil:** verify runs sandboxed (`deny network*`, `:100`); inbox is human-only; patch is `clean()`'d +
  `SECRET_RE`-redacted + withheld on a secret hit (`:62/:76-78`). The codex-submit stage is `net=false`
  (pre-ship §1).
- **Loop immunity** — empirically verified seatbelt confinement (see Idempotency & loop-immunity).

## Pre-ship verification gates (must pass before arming `PLAY_AUTOFIX=1`)
1. **Codex builder confined + net=false (VERIFIED for prod).** `orchestrator/probe-git-write-vector.sh`
   PASSED: `workspace-write` confines writes to `{workdir,/tmp,$TMPDIR}` and denies shared-`.git` writes.
   **Condition:** the armed repo's path must NOT be under `/tmp` or `$TMPDIR`. Re-run the probe if the
   codex CLI or its config changes. (myndaix-runtime: `~/code/active/...` — passes.)
2. **Trusted install present + pinned:** `$ORCH/play-fix.sh` exists, is executable, and `shasum -a 256`
   matches the reviewed `orchestrator/play-fix.sh`. Deploy step: `cp orchestrator/play-fix.sh "$ORCH/play-fix.sh"`.
3. **Armed repos are `fail_to_pass:null`** in the live repos.json (enforced at fire time too).
4. **No agent watcher on `inbox/jefe/`** (loop-immunity invariant).

## Files
**Created:**
- `docs/phase2-autonomous-fix-flip-design.md` — this DESIGN (cross-family reviewed).
- `docs/phase2-autonomous-fix-flip-research.md` — prior-art brief.
- `orchestrator/probe-git-write-vector.sh` — the reproducible pre-arm gate (§1); proves codex's seatbelt
  denies shared-`.git` writes for a non-tmp repo. Not a unit test — an occasional/deploy-time gate.

**Modified:**
- `orchestrator/play-review.sh` — the ONLY runtime code change. Define
  `REPOS_JSON="${MYNDAIX_REPOS_JSON:-$ORCH/repos.json}"`; wrap `confirm_pushed`'s `ls-remote` in a portable
  timeout; capture `confirm_pushed`/`deliver` `set -e`-safe and reuse; restructure the NEEDS-FIX branch
  (`:202-208`): always write `$run/fixlist.txt`, always append the manual hint + neutral auto note, deliver,
  set `done-$tip`; then the fail-closed fire gate (PLAY_AUTOFIX + `$pushed` + `jq -e fail_to_pass:null` +
  trusted-fixer-x + runtime `base==tip` + non-empty fixlist), `env -i`-whitelisted `nohup`. `play-fix.sh`
  and the lobster prompt are **byte-for-byte unchanged**.
- `orchestrator/test.sh` — add hard, blocking smoke cases (stub fixer via `PLAY_FIX_SELF` under
  `PLAY_AUTOFIX_TEST_MODE=1`, no real codex): (a) writes `$run/fixlist.txt`; (b) fixer arg2 == $tip (NOT
  $base) and exactly 3 args; (c) fires ≤once per tip across two same-tip runs; (d) no fire when
  `confirm_pushed` false (force via a bogus non-empty `remote_url` whose `ls-remote` ref ≠ tip); (e) no fire
  on `fail_to_pass` non-null / missing key; (f) no fire with no trusted install (never resolves to
  `$repo/orchestrator/play-fix.sh`); (g) PASS branch never writes fixlist / never fires; (h) armed-but-
  suppressed still emits the manual hint; (i) `PLAY_AUTOFIX` unset/0 → hint present, no fire; (j) `shasum`
  guard that `orchestrator/play-fix.sh` is unchanged by this PR.

## Dependencies
**Depends on:** merged `play-fix.sh` (signature `:130-131`, base gate `:142-143`, `.fail_to_pass` read
`:158`, fixlist cap `:144-145`, UNVERIFIED ceiling `:318`, `fix-state/lock` `:188-192`, daily cap `:28`,
sandbox builder `:93-125`); `play-review.sh` primitives (`confirm_pushed:124-128`, `mark_done:133`, run dir
`:75`, `deliver:85-102`, global lock `:143-153`, `done-$tip:160`, prune `:157`, nohup `:63`); `repos.json`
(key=basename, absolute `.path`, `.fail_to_pass`); `jq`; the runtime codex builder spec
(`registry.py:76` — `workspace-write` + `network_access=false`); `gtimeout` optional.
**Depended on by:** nothing programmatic — output is human-read inbox markdown.

## Deliberately NOT built
- No selector extraction / `PLAY_F2P` clause in v1 (lobster prompt untouched; forfeits REGRESSION_CHECK_ONLY).
- No auto-apply / auto-merge / auto-commit (disqualifying — preserves the contract + loop immunity).
- No new PASS verdict.
- No new script (except the probe gate), daemon, queue, Postgres row, or launchd service.
- No `--max-verdict` flag on play-fix.sh / no other change to the frozen file (deferred — see Product decision).
- No inbox ranker / priority layer.
- No debounce window; no auto-retry; no separate auto-fire daily cap (shares `PLAY_FIX_DAILY_CAP`).
- No `fix-fired-$tip` marker (redundant with `done-$tip`).
- No two-phase provisional claim / EXIT-trap rollback.
- No worktree-copy fixer fallback on the auto path (trusted install only).
- No runner change to detach/clone the codex worktree (the probe shows the seatbelt already confines writes
  for non-tmp repos; revisit only if the probe ever FAILs).

## Open questions for the re-review (on the BUILT code)
1. **Portable `ls-remote` timeout:** is the `gtimeout`-or-bg-kill wrapper correct and does it leave no
   orphan on the timeout path?
2. **`jq -e` semantics:** confirm it returns non-zero (suppresses fire) for missing key, missing field,
   `false`, and string `"null"`, and zero only for explicit JSON `null`.
3. **`env -i` whitelist sufficiency:** does play-fix reach the codex pool + write the inbox with only
   `PATH`/`HOME` passed, or is another var needed (e.g. `LOGNAME`/`USER` for osascript)?
4. **Runtime base assertion:** is `base==tip && base!=tip-of-range` the right invariant, including the
   new/root-branch EMPTY_TREE case?
5. **Test seam:** is `PLAY_AUTOFIX_TEST_MODE`-gating of `PLAY_FIX_SELF` a tight enough production lockout?
