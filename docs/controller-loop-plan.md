# Controller-Loop — Implementation Plan (for Jefe approval before build)

_Builds DESIGN v0.2 (`docs/controller-loop-design.md`). North-star rung 3. Branch: `northstar/controller-loop` (design docs already committed there). One branch, ordered commits; cross-family review of the BUILT code before merge; atomic dry-run-first deploy._

## Build order (each step: build → test → self-check → commit)

### Step 1 — `review_cursor` table + migration + ledger methods (pure spine, no behavior)
- `src/runtime/ledger/migrations/0003_review_cursor.sql` — idempotent (`CREATE TABLE IF NOT EXISTS`), + mirror into `src/runtime/ledger/schema.sql`. Columns per DESIGN §2 (repo_id, ref, baseline_sha, reviewed_sha, pending_sha, state, attempts, updated_at; PK (repo_id, ref)).
- Ledger methods on `PostgresLedger` (+ the `CommandAPI` Protocol + the sqlite store if it mirrors): `get_cursor(repo_id, ref)`, `upsert_baseline(repo_id, ref, head)`, `claim_dispatch(repo_id, ref, head)` (conditional UPDATE → the dedup gate; returns True iff this caller won the slot), `advance_cursor(repo_id, ref, head)`, `mark_blocked(repo_id, ref)`, `clear_pending(...)`.
- **Tests:** `tests/test_postgres_ledger.py` += cursor lifecycle (baseline → claim → advance → reclaim-stale-pending → blocked); `claim_dispatch` concurrency (two callers, exactly one wins). Run via `LEDGER_TEST_DSN`.
- **Gate:** spine-only, no caller yet → zero behavior change. Safe to commit independently.

### Step 2 — `src/runtime/controller.py` (the tick) + entry
- Config load: read `$ORCH/repos.json`, take `path` + optional `watch_ref` (default `refs/heads/main`); compute `repo_id = basename(path)`; **reject duplicate basenames** (M6); canonicalize paths.
- Single-instance lock: atomic `mkdir` under `$ORCH` + metadata (pid/start/host) + TTL/mtime stale-reap + `trap`/`finally` cleanup; exit 0 if freshly held (M2).
- Per-repo tick (bounded by `MAX_DISPATCH_PER_TICK` + daily dispatch budget, M4):
  1. **advance pass:** if `pending_sha` set and its `$STATE/done-<sha>` marker exists → `advance_cursor`; if pending stale past TTL → clear (retry path, M5).
  2. **fetch** (B1): `git -C <path> fetch --no-tags --quiet origin <watch_ref>` via `subprocess.run` argv, timeout, scrubbed env; skip+log on failure.
  3. **observe:** read fetched ref SHA (`git rev-parse`/`ls-remote --exit-code`, validate `^[0-9a-f]{40}$`, exactly one line).
  4. **decide** (DESIGN §3): no cursor → `upsert_baseline`, no review (B2); `head==reviewed_sha` → noop; `pending==head` → noop; `attempts>=MAX` → `mark_blocked` + surface once; else dispatch.
  5. **dispatch:** assert `cat-file -e head^{commit}` AND `reviewed_sha^{commit}`; `claim_dispatch` (the gate); on win → `subprocess.run(["$ORCH/play-review.sh","origin",remote_url], input=f"{watch_ref} {head} {watch_ref} {reviewed_sha}\n".encode(), cwd=path, env=SCRUBBED)`.
- **Security** (DESIGN §5): no `shell=True`; execute only the trusted fixed `$ORCH/play-review.sh` after owner/mode/symlink checks; exact ref equality; URL scheme allowlist; env scrub strips `PLAY_AUTOFIX`/`GIT_*`/`BASH_ENV`/protocol-helpers; warn if `$ORCH/AUTOFIX_ENABLED` present (B3).
- **Entry:** `python -m runtime.controller tick` (one tick, exit — bounded job, not a daemon).
- **Test seam:** `MYNDAIX_CONTROLLER_TEST_MODE=1` + `MYNDAIX_CONTROLLER_DISPATCH_OVERRIDE=<file>` records the would-be invocation instead of running play-review (fail-closed if override set without test mode — mirrors play-fix's seam). Plus `MYNDAIX_CONTROLLER_DRY_RUN=1` (decide + log, never dispatch) for safe first live run.
- **Tests:** `tests/test_controller.py` over throwaway `git init` repos + a test ledger + the dispatch seam: bootstrap-no-review, advance-on-HEAD-move (correct `reviewed..head` range captured), dedup (pending set → no second dispatch), fetch-failure-skip, budget cap, dup-basename rejection, blocked-after-MAX-attempts, autofix-flag-warns-but-PLAY_AUTOFIX-absent, SHA/ref validation rejects junk, no-shell injection attempt is inert.

### Step 3 — launchd + deploy artifacts + runbook
- `orchestrator/ai.myndaix.controller.plist.example` (hourly `StartInterval`/calendar, RunAtLoad, gui/501, WorkingDirectory = repo, invokes the serve venv `python -m runtime.controller tick`, logs to a file).
- Runbook section in the design doc: install/uninstall (`launchctl load/unload` = rollback), the dry-run-first sequence, where the cursor + logs live.

## Verify / deploy (per new-systems.md: test.sh + atomic deploys)
1. Full suite green: `tests/test_controller.py`, `tests/test_postgres_ledger.py`, no regressions in the rest (standalone runner, `LEDGER_TEST_DSN`).
2. **Cross-family review of the BUILT code** (codex reads repo + Oracle inlined) → fold findings → re-verify. (Commit+push first per commit-before-review.)
3. **Dry-run live** on the MacBook: `MYNDAIX_CONTROLLER_DRY_RUN=1 python -m runtime.controller tick` against real repos.json → confirm it seeds baselines + logs correct decisions, dispatches nothing.
4. **First real tick manually** (no plist yet): let it seed baselines for real; verify cursor rows; confirm no whole-tree review fired.
5. **Install the plist** (atomic: this machine only); observe the next scheduled tick reviews a real delta end-to-end (verdict lands in inbox/jefe).
6. Rollback at any point = `launchctl unload` + (optionally) leave the inert cursor table.

## Decisions baked in (from review + your locks)
- Trigger = synthetic-stdin zero-touch · watch = `refs/heads/main` · cadence = hourly · cursor-advance = play-review's `done-<sha>` marker · `repo_id` = basename · brain never sets `PLAY_AUTOFIX`.

## What this rung does NOT do (deferred to later rungs)
LLM in the decision path · auto-fix · auto-merge · learning/outcomes · webhooks · multi-ref/all-branches.

## Estimated footprint
1 migration + ~6 ledger methods + 1 new module (~250 lines) + 1 plist example + 2 test files touched. Rollback = one `launchctl unload`.
