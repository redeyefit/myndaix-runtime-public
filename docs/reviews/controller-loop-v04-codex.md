NEEDS-REVISION

**Findings**

MAJOR [src/runtime/controller.py](/Users/you/code/active/myndaix-runtime/src/runtime/controller.py:207): old `controller.lock` directories hard-fail the new flock path. `os.open()` on the legacy mkdir lock raises `IsADirectoryError`, so a stale pre-v0.4 lock dir can permanently crash/stall the controller. Fix: handle legacy directories explicitly, preferably with a migration-safe path or stale-dir compatibility path that does not delete a live old holder.

MAJOR [src/runtime/controller.py](/Users/you/code/active/myndaix-runtime/src/runtime/controller.py:377): pending pin failure is ignored. If `update-ref refs/myndaix/pending/... <head>` fails after the DB claim, the review still dispatches without the GC anchor v0.4 depends on. Fix: check `_pin(...)`; on failure, do not trigger review, log, and release/stale or block the claim so retry/backoff remains bounded.

MAJOR [orchestrator/play-review.sh](/Users/you/code/active/myndaix-runtime/orchestrator/play-review.sh:152): `PLAY_FORCE_DONE` is a public env bypass for push confirmation. Any non-controller invocation inheriting/setting it writes `done-$tip` after delivery even when `ls-remote` says the push was unconfirmed, regressing normal hook dedupe semantics. Fix: remove the generic env override; for controller use pass an empty remote URL, or a controller-specific marker path/mode that normal hook runs cannot accidentally enable.

No issue found in the v0.4 `GIT_ALLOW_PROTOCOL` usage for controller git or controller-launched `ls-remote`: it is inherited and blocks `ext`/`fd` while preserving normal ssh/https auth config. `release_dispatch` also preserves attempts and is correctly guarded on `(repo_id, ref, pending_sha, state='dispatching')`.

Tests not run; sandbox is read-only.
