VERDICT: NEEDS-REVISION

**BLOCKER**

1. `orchestrator/play-review.sh:158`, `orchestrator/play-review.sh:299`, `src/runtime/controller.py:329`: brain-triggered reviews can still auto-fix. The controller only strips `PLAY_AUTOFIX`, but `play-review.sh` also arms autofix from `$ORCH/AUTOFIX_ENABLED`. If that file exists and the verdict is NEEDS-FIX, `autofix_fire` runs.
Fix: add an explicit controller disable flag, e.g. `PLAY_DISABLE_AUTOFIX=1`, make it override both env and durable flag in `autofix_armed`, and set it from `_review_env()`. Add a test with `AUTOFIX_ENABLED` present.

2. `src/runtime/controller.py:233`, `orchestrator/play-review.sh:128`, `orchestrator/play-review.sh:148`, `orchestrator/play-review.sh:275`: moving branches can re-review forever. Controller passes a non-empty remote URL, so `mark_done` only writes `done-<tip>` if the watched ref still equals `tip` at review completion. On an active branch, if `main` advances during review, the verdict is delivered but no done marker is written; the cursor never advances and the next tick reviews `old_reviewed..new_head` again.
Fix: give controller dispatches their own delivered signal, or a `PLAY_CONTROLLER_DISPATCH=1` mode where durable delivery marks done without the pre-push “still remote tip” check. Keep `confirm_pushed` for real pre-push hooks only.

**MAJOR**

1. `src/runtime/controller.py:261`, `src/runtime/controller.py:201`: fetch happens before remote URL validation. A malicious/drifted `remote.origin.url` or git config is used by `git fetch origin ...` before `_remote_url()` rejects `ext::` or other unsafe transports.
Fix: read and validate the URL before fetch, then fetch by sanitized URL rather than remote name.

2. `src/runtime/controller.py:81`, `src/runtime/controller.py:261`: git execution is not config-sandboxed. `_git_env()` scrubs env, but Git still reads repo/global config, credential helpers, URL rewrites, protocol settings, and possible submodule recursion config.
Fix: invoke git with config hardening: `--no-recurse-submodules`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `-c credential.helper=`, `-c protocol.ext.allow=never`, `-c fetch.recurseSubmodules=false`, and remove `http://` / `git://` unless intentionally accepted.

3. `src/runtime/controller.py:261`, `src/runtime/controller.py:264`: `FETCH_HEAD` is a shared mutable pseudo-ref. Any concurrent human/tool fetch in the same clone can overwrite it between fetch and `rev-parse`, causing the controller to dispatch a SHA that is not the watched ref.
Fix: fetch into a controller-owned ref, e.g. `refs/myndaix/controller/<repo>/<ref>`, and resolve that ref.

4. `src/runtime/controller.py:290`: base object loss wedges the cursor. The controller only fetches current HEAD; `reviewed_sha` may later be pruned or orphaned after force-push because it is not anchored by a ref. Then `cat-file` fails forever and the repo silently stops progressing.
Fix: maintain controller-owned refs for `reviewed_sha` and pending heads; update them on baseline/advance. If already missing, surface a blocked/manual-rebaseline state.

5. `src/runtime/ledger/postgres_store.py:930`, `src/runtime/controller.py:280`: `mark_blocked` is not a CAS. It unconditionally marks the row blocked after the caller’s stale read. A concurrent new-head claim can be overwritten into `blocked`.
Fix: pass the expected `head` and `MAX_ATTEMPTS`; update only with `WHERE pending_sha=$3 AND attempts >= $4 AND state='dispatching'`.

6. `src/runtime/controller.py:48`, `src/runtime/controller.py:247`, `orchestrator/play-review.sh:23`: `MYNDAIX_ORCH` override wedges done-marker advancement. Controller reads `STATE` under `MYNDAIX_ORCH`; `play-review.sh` ignores that env and writes to `$HOME/.myndaix/orchestrator`.
Fix: make `play-review.sh` honor `MYNDAIX_ORCH`, or reject controller startup when the override is set.

7. `src/runtime/controller.py:93`, `orchestrator/play-review.sh:53`: trusted-script validation does not guarantee the worker script in non-default configs. The front script can re-exec `$ORCH/play-review.sh` or a worktree fallback unless `PLAY_SELF` is set.
Fix: set `PLAY_SELF` in `_review_env()` to the exact validated `PLAY_REVIEW` path, and add a controller mode that refuses worktree fallback.

8. `src/runtime/controller.py:155`, `src/runtime/controller.py:160`: stale lock reaping is not tied to a hard tick bound. Repo count is unbounded, DB connect can block, and per-repo timeouts can exceed `LOCK_TTL`; a later tick can reap a live controller.
Fix: heartbeat/touch the lock while running, or enforce a hard total tick deadline below TTL.

**MINOR**

1. `docs/controller-loop-design.md:37`, `src/runtime/ledger/schema.sql:111`: design lists `running` as a cursor state, but schema/check constraint only allows `baseline|dispatching|delivered|blocked`.
Fix: align the doc or add the state.

2. `src/runtime/controller.py:72`: URL allowlist includes `http://` and `git://`, while the design says ssh/https/file. Those are unauthenticated/plain transports.
Fix: remove them unless explicitly required and documented.

I did not run the DB-backed tests; this was a static adversarial review of the built branch.
