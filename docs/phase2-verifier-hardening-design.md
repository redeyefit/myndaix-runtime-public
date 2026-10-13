# DESIGN: verify-sandbox hardening + reachable REGRESSION_CHECK_ONLY

_v0.1 — 2026-06-25 — Mack. Prereq gate before any autonomous-fix flip._

## What
Make `play-fix.sh`'s deterministic verify stage able to actually run this repo's
test suite under its sandbox, and make the `REGRESSION_CHECK_ONLY` verdict
*reachable* for the regression-fix case — without weakening the sandbox's exfil /
write containment.

## Why
Empirically probed (the live deploy, 2026-06-25): the verify stage returns
**UNVERIFIED on every fix** for THREE independent reasons.

1. **Sandbox can't run anything that touches `/dev/null`.** The sbpl profile is
   `(allow default)(deny network*)(deny file-write*)` + write-allows only for the
   worktree and scratch dirs. `/dev/null` is not in that set, so git and most tools
   fail with `could not open '/dev/null' ... Operation not permitted`. This repo's
   tests `git init` in temp dirs, so the whole suite dies. (Side note: `/usr/bin/git`
   is the Xcode shim and prints an xcrun-cache write error, but that warning is
   NON-fatal once `/dev/null` is writable — git returns rc=0. No PATH change needed.)

1b. **Exec dirs live UNDER the read-denied `$HOME/.myndaix`.** The default
   `$ORCH=$HOME/.myndaix/orchestrator`, and the verify worktree + scratch dirs were
   created under the per-run dir there. But the profile read-denies all of
   `$HOME/.myndaix` (to hide bridge secrets/state). git/python traverse ANCESTOR
   directories (repo discovery, `realpath`/`getcwd`) and die when an ancestor is
   read-denied: `fatal: Invalid path '/Users/.../.myndaix'` / `failed to make path
   absolute`. A leaf-level read carve-out does NOT fix this (the intermediate parent
   is still unreadable). Fix: relocate the sandboxed worktrees + scratch to a fresh
   `mktemp -d` OUTSIDE the deny tree; keep the private patch + logs in `$run` (the
   patch is applied by the trusted PARENT shell, never read inside the sandbox).
   This blocker was masked in test-fix.sh because its `$ORCH` is a temp dir.

2. **`REGRESSION_CHECK_ONLY` is unreachable without a failing-on-base test.** The
   contract (play-fix.sh:243–282): `fail_to_pass` must FAIL on the clean base
   (proves the bug existed), the verify suite must still pass, and the patch may not
   touch any test file (→ TAMPERED). For a healthy repo every committed test passes
   on base, so there is no static `fail_to_pass` that fails on base, and the fix
   can't add one. `fail_to_pass` is currently sourced ONLY from repos.json (static).
   So the verdict can only ever be UNVERIFIED. To reach REGRESSION_CHECK_ONLY the
   caller must supply the SPECIFIC existing test that the regression broke.

## Data flow (after change)
`play-fix.sh <repo_id> <base_sha> <fixlist> [<fail_to_pass_selector>]`
- repos.json gives the TRUSTED `verify` argv (regression suite) and a
  `fail_to_pass_template` argv containing one `{TEST}` placeholder.
- The optional 4th arg is a per-fix test SELECTOR (e.g. `tests/test_worker.py`),
  validated against a strict allow-pattern and confirmed to exist under the repo's
  test dir, then substituted for `{TEST}`. Interpreter, PYTHONPATH, and all other
  argv elements come from trusted config — the selector can only choose WHICH test
  file runs, never inject an arbitrary command.
- No 4th arg → behaves exactly as today (fail_to_pass from static repos.json or
  UNVERIFIED). Fully back-compatible.

## Edge cases
- Selector with shell metachars / path traversal / absolute path / not matching
  `^tests/test_[A-Za-z0-9_]+\.py$` → fail_closed (ABORTED).
- Selector names a file that doesn't exist in the base checkout → fail_closed.
- `fail_to_pass_template` missing `{TEST}` while a selector is supplied → fail_closed.
- Selector test PASSES on clean base → existing UNVERIFIED path (line 250) unchanged.
- `test_terminal.py` cannot run sandboxed (needs a pty) → excluded from the verify
  suite; documented.

## Security surface
- **Untrusted:** the codex patch (already handled). NEW: the `fail_to_pass_selector`
  is operator/orchestrator-supplied, lower-trust than repos.json. Mitigation: it is
  NOT an argv — it is a single token, regex-validated + existence-checked, only
  substituted into a `{TEST}` slot of a trusted template. It can never become argv0
  or add flags. Runs in the same wiped-env sandbox as everything else.
- **`/dev/null` write-allow:** universally safe — `/dev/null` discards writes; it
  cannot exfiltrate or mutate the filesystem. Containment (network deny, write deny
  outside worktree+scratch, secret-read deny) is otherwise unchanged.
- No new network, no new readable secret paths, no new writable real-FS paths.

## Files
- MODIFY `orchestrator/play-fix.sh`: add `(allow file-write* (literal "/dev/null"))`
  to the run_sandboxed profile; RELOCATE sandboxed worktrees + scratch to an `$EXEC`
  `mktemp -d` outside the read-denied tree (with early-abort + full cleanup); add
  optional 4th-arg selector + strict validation + `{TEST}` substitution.
- MODIFY `~/.myndaix/orchestrator/repos.json` (+ committed `repos.json.example`):
  add `verify` (suite) and `fail_to_pass_template` for myndaix-runtime.
- MODIFY `orchestrator/test-fix.sh`: cases for /dev/null-enabled suite, selector
  validation (good + malicious), template substitution, back-compat (no 4th arg).

## Dependencies
- Depends on the merged PR-4 fix stage (live). Nothing depends on this yet; the
  autonomous caller (which would pass the selector) does not exist — this unblocks it.
