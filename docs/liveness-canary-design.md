# DESIGN.md: liveness-canary — declared-vs-runtime execution reconciliation

> **STATUS (2026-07-18):** BUILT + reviewed on branch `feat/liveness-canary` (unmerged; live launchd-bootstrap verify is a deploy step — LAB first). Design APPROVED — Oracle round 1 NEEDS-CHANGES (6 findings), all fixed; Oracle round 2 PASS (job 5b0efacf). Research provenance: ~/research/2026-07-18-autonomous-headless-agents.md + Recon prior-art pass (job 74ec6b9e, verdict BUILD).
>
> **Review trail (all closed):** external KilaBz+Oracle PASS after 4 rounds; then a deep multi-agent audit (82 agents, 6 dimensions, 3-lens adversarial verify) found **10 confirmed findings both diff-reviewers missed** — they model launchd RUNTIME behavior, reconcile's convergence side-effects, and multi-tick alert sequences. Fixed: (P1) global latch → GROW-ONLY per-key latch (an N-signal canary went blind to new failures while any prior stood); (P2) reconcile-grace rolling-window → **content-aware plist install** in reconcile.sh (byte+mode+type `cmp` so a no-op converge preserves plist mtime — the grace keys on it; unconditional rewrite left long-gap jobs perpetually graced/unwatched); (P2) exit-code launchd-latch → evaluated only when `.out` is fresh AND after **two consecutive failed FIRES** (per-label mtime+count state, not per-tick — launchd latches last-exit-code between runs); (P2) per-file ERR no longer flags a still-loaded job ROGUE (rogue sweep skipped on an incomplete declared set); (P2) rogue labels regex-VALIDATED not `tr`-sanitized (a mangled name can't alias a bootout onto a healthy job); (P2) drift-canary's liveness reverse-watch got its OWN streak+latch (a standing config-drift/QUARANTINE latch no longer mutes "the execution watcher is dead"); (P3) POLL/gap coupling documented+tested; SETUP.md inventory. Cross-family re-review: Oracle PASS all; KilaBz found 2 more valid P2s (exit-code persistence, cmp metadata) → fixed → KilaBz PASS.
>
> **Build/runtime notes honored:** (1) `.last_run` touched on EVERY normal run; (2) liveness_targets.py emits a per-file ERR + continues, never sinks the batch; (3) accepted: a job broken at deploy won't alert until its max-gap elapses post-install. **New invariant introduced:** every watched descriptor's program writes an unconditional `printf 'tick fire'` stdout line (`liveness-fire`, test-asserted) — because the freshness check reads `.out` mtime, and fix-sweep/controller had silent early-exit paths that would freeze it. Consequence: a job that fires-then-fails has a fresh `.out`, so the exit-code check is the ONLY guard for that class (hence its persistence fix matters). Substrate suite: 336 ok, 0 fail. Accepted residual: the cmp mode/type test is grep-structural (a real wrong-mode fixture needs a full converge harness); alert filenames are second-resolution (collision impossible at the 900s live cadence).

## What

A ~100-line bash check (`substrate/liveness-canary.sh` + descriptor `substrate/plists/ai.myndaix.liveness.json`, 15-min interval) that verifies every job DECLARED in `substrate/plists/*.json` (for this machine's role) is actually ALIVE at runtime: label loaded in the launchd domain, last exit status healthy, and fresh evidence of recent execution. Alerts to the operator inbox using the drift-canary streak+latch pattern. Also flags loaded `ai.myndaix.*` labels that are NOT declared (hand-added rogues outside reconcile's managed set).

## Why

The 2026-07-18 deep-research report's biggest verified production incident class is **operational omission**: declared state diverging from runtime state — a fully-tested job that simply never ran (silent 13h–60d). Our current coverage has exactly this hole:

- `reconcile.sh` + `drift-canary.sh` verify **config-level** convergence (descriptor → rendered plist → installed → bootstrapped) and alert if reconcile stops converging.
- The pool's lease/heartbeat machinery verifies **intra-job** liveness (attempt-level).
- **Nothing verifies that a scheduled job actually fires.** A plist can be installed AND loaded while the program crashes at spawn every tick, launchd throttles it, the job hangs forever, or the label got booted out by hand. drift-canary's dry-run stays green through all of these. And if drift-canary itself dies, config drift goes unwatched too — silently.

This check is deliberately deterministic — per the research verdict, runtime-state reconciliation is a machine's job, not an LLM's.

## Build vs Adopt (from research brief)

| Candidate | Verdict | Why |
|---|---|---|
| healthchecks.io (self-hosted) | REJECT | Django+Redis webapp stack for one Mac; ping-model can't see launchd state |
| Cronitor | REJECT | Cloud control plane; violates local-first |
| Uptime Kuma | REJECT | Node/Docker dashboard; HTTP/TCP checks only, no launchd/registry awareness |
| launchd monitor GUIs (Raycast ext, LaunchManager) | BORROW-THE-PATTERN | Enumerate labels + last-run/exit-status — same idea, headless |
| ~100-line bash + launchctl | **BUILD** | Only option that is launchd-aware, descriptor-aware, local-first, near-zero maintenance |

Deliberately NOT built: no new registry (descriptors in `substrate/plists/` stay the single source of truth); no Postgres dependency (pool-job liveness is already covered by lease/heartbeat; adding SQL here duplicates it); no auto-fix (reconcile converges; this only shouts); no new alert transport (operator inbox file-drop, same as drift-canary).

## Data Flow

Input → Process → Output:

1. **Declared set**: a SINGLE `python3` invocation parses all of `substrate/plists/*.json` (paths via `sys.argv`), applies `render_plist.py role-check` semantics for `$MACHINE_ROLE`, and emits one bash-friendly line per watched job: `label:max_gap_seconds:requires_sentinel`. **No schedule math in this component**: every descriptor MUST carry an explicit `liveness_max_gap_seconds` field (e.g. hourly job → 7500; :30 calendar job → 4500). A watched-role descriptor missing the field is a build-time failure (`substrate/test.sh` asserts the field on every descriptor) AND a runtime divergence (fail-closed — an unwatchable job is the exact omission class this kills; opt-in would recreate the hole). Plus a small static list of hand-managed long-lived labels: `ai.myndaix.runtime` (daemon — liveness = pid present).
2. **Self-grace (sleep/wake guard)**: before checking anything, compare `now` against the canary's own `.last_run` touch file. If the canary itself hasn't run in > 2× its interval, the machine was asleep/frozen — touch `.last_run`, exit 0, let every job catch up one tick. Prevents a wake-up alert storm for every job at once.
3. **Runtime evidence**, per declared label:
   - **Reconcile-grace**: if the installed plist's mtime is fresher than the job's max gap, PASS (recently (re)installed/updated by reconcile — job hasn't had a full cycle yet). This grace is unconditional — NOT gated on a missing `.out` (an old `.out` + fresh plist must not false-alert).
   - `launchctl print gui/$UID/<label>` (targeted, known label only) → loaded? last exit status? (daemon: pid?)
   - freshness: mtime of the job's stdout log `$MYNDAIX_HOME/orchestrator/<label>.out` (every tick logs ≥1 line per bash rules — asserted in test) within `liveness_max_gap_seconds`.
4. **Reverse sweep**: `launchctl list` (tab-delimited, stable) filtered to `^ai\.myndaix\.` minus declared set minus static list → rogue labels. `launchctl print` output is never parsed for enumeration (brittle across macOS releases).
5. **Divergence** (any check fails) → bump streak file; at threshold (2 consecutive) and not latched → drop `liveness-alert-<ts>.md` to `$OPERATOR_INBOX` listing each divergent label + which check failed + the one-line remedy (`launchctl bootstrap …` / investigate .out). Clean run → reset streak + latch. Exit 0 always (the canary itself must not accumulate launchd failure state).

**Watcher-of-the-watcher**: liveness-canary covers drift-canary's execution recency (it's a declared job like any other). The reverse — drift-canary gains a 3-line check that `liveness-canary.out` mtime is fresh (< 2× its interval), alerting through its existing latch if stale. Mutual coverage, no third component, no cycle risk (each only *reads* the other's log mtime).

**Documented limitation (accepted, per design review)**: both canaries alert into the same `$OPERATOR_INBOX` file-drop. A total control-plane failure (launchd domain wedged, disk full, machine dark) silences both alerts AND both canaries simultaneously — the mutual watch protects against individual unscheduling, not shared fate. Whole-machine liveness is out of scope here; it is covered by the pool's network-visible surface (`mxr mini` canary / `/health`) going dark.

## Edge Cases

- **First run / missing .out**: covered by the unconditional reconcile-grace (fresh plist mtime → PASS); past the grace window with no `.out` → "never ran" divergence.
- **System sleep / clock jump**: covered by the self-grace guard (own `.last_run` stale → skip one tick).
- **Machine role mismatch**: descriptors excluded by role-check are skipped (not "missing").
- **Empty/corrupt descriptor JSON**: python3 parse failure → count as divergence (fail-closed), name the file.
- **OPERATOR_INBOX absent**: log-don't-latch, retry next interval (drift-canary's exact rule — a lost alert must not be suppressed forever).
- **launchctl print permission/SIP quirks**: non-zero exit from launchctl on an expected label = "not loaded" divergence, not a crash.
- **Clock skew/DST**: all comparisons via epoch seconds (`date +%s`, `stat -f %m`); no date parsing.
- **Octal trap**: all numeric env/derived values normalized `$((10#$n))` after regex check.
- **Streak/latch file writes**: explicit fail-closed `printf > tmp && mv` with error handling (the #89 &&-chain class), identical to drift-canary.
- **Sentinel-gated jobs** (`ai.myndaix.reconcile`, requires_sentinel): if the sentinel is absent the job is legitimately unloaded → skip, not divergence. Read the same `requires_sentinel` field reconcile/test.sh use.

## Security Surface

- **Untrusted input**: none from outside the repo/machine. Descriptor JSON and launchctl output are local trusted-ish data; still: no `eval`, all variables quoted, python3 gets file paths via `sys.argv`, labels validated against `^ai\.myndaix\.[A-Za-z0-9._-]+$` before interpolation into launchctl calls or filenames.
- **Injection into alerts**: alert bodies embed label names + file paths only after the regex validation above; free-text from .out files is NOT embedded (only mtimes).
- **Stored**: two state files (`liveness-streak`, `liveness-alerted`) under `$MYNDAIX_HOME/state/`; alert .md files in operator inbox. Nothing secret.
- **Blast radius**: read-only against launchd (print only — never bootstrap/bootout/kickstart); cannot mutate jobs. The only writes are its own state files + alert drops.

## Files

- CREATE `substrate/liveness-canary.sh` (~100–120 lines)
- CREATE `substrate/liveness_targets.py` (single-invocation descriptor parser → `label:max_gap:sentinel` lines)
- CREATE `substrate/plists/ai.myndaix.liveness.json` (15-min StartInterval, non-mutating, all roles that run scheduled jobs)
- MODIFY all existing `substrate/plists/*.json` descriptors (+1 field each: `liveness_max_gap_seconds`)
- MODIFY `substrate/drift-canary.sh` (+~4 lines: liveness-canary.out freshness check through existing alert path)
- MODIFY `substrate/test.sh` (+ tests: fixture descriptors → declared-set extraction; stale-mtime → divergence; sentinel-gated skip; rogue-label detection; streak/latch behavior; every-tick-logs assertion for all descriptors' programs)
- MODIFY `substrate/plists/` docs comment or `docs/` runbook line if one exists for canaries

## Dependencies

- Depends on: `substrate/lib.sh` (config load, log, die), `render_plist.py role-check` (role filtering), descriptor JSON schema (label/schedule/requires_sentinel fields), launchd domain conventions, `$OPERATOR_INBOX`.
- Depended on by: drift-canary's new freshness check (read-only, mtime of this job's .out).
- Deploy: standard path — descriptor lands in `substrate/plists/`, reconcile renders/installs/bootstraps it on its next converge; no serve restart needed.
