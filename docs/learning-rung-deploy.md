# DEPLOY / ARM RUNBOOK — `+learning` rung v1 ("review skills")

Operator (Jefe) runbook to ship the rung **dark**, then **arm** it atomically. Everything is
**OFF by default**; nothing here changes a review verdict until the final `touch`. Design:
`docs/learning-rung-design.md` (`## v0.3`+). Build: `docs/learning-rung-plan.md`.

The whole rung is **fail-closed + fail-open in the safe direction**: selection fails OPEN to an
empty hint (never forces a verdict), and the provenance arm fails CLOSED (no protection → no
skills). You can ship every code step with selection still OFF and verify at each stage.

---

## PREREQUISITE (do this FIRST) — branch protection on `main`

The arm is unforgeable ONLY because every change on `main` arrived via a human-merged PR.
A `MYNDAIX_CONTROLLER_DRY_RUN=1` tick today reports **SKILLS BLOCKED** for every repo because
`main` is not yet fully protected. Set this on each watched repo (`gh` or the web UI):

- **Require a pull request before merging** — but set **`required_approving_review_count: 0`**.
  A required PR forbids DIRECT pushes (which is what the arm needs), while 0 required approvals
  lets the **automerge** rung keep merging docs PRs on green CI without a human approval. ≥1
  approval would break automerge — `_branch_protection_ok` only checks that
  `required_pull_request_reviews` is present, NOT the approval count.
- **Do not allow bypassing the above settings** → i.e. **enforce admins** (`enforce_admins.enabled = true`).
  Without this a solo admin could push a skill directly, defeating the arm. (Trade-off: you must
  then PR every change to main — but with 0 required approvals you can self-merge after CI is green.)
- **Do not allow force pushes** (`allow_force_pushes.enabled = false`).
- **Keep the existing `test` required status check** (the automerge CI gate) — the protection
  PUT replaces the whole object, so preserve `required_status_checks.contexts = ["test"]`.

**Threat-model note (0 required approvals).** With 0 approvals a holder of a write token (or a
rogue agent) could open + self-merge a PR with no second human, shifting trust onto the
`play-review` LLM gate (injection/hallucination-prone) + CI. This is an accepted solo-founder
trade-off, and the compensating controls are deliberate: (1) for the SKILL corpus specifically,
provenance is unforgeable regardless of approvals — `skills/` is in automerge's `_DENY_DIRS`, so a
skill can only reach main via a PR a human chose to merge; (2) the diff-class gate bounds what
automerge can land to inert docs; (3) `enforce_admins` + no-force-push mean every change is a
revertible, audited merge commit. If you want a hard second-human gate on code, raise the count to
1 — but then disable/relax the automerge rung, which relies on 0.

Verify exactly what the controller checks:

```
gh api repos/<owner>/<repo>/branches/main/protection \
  -q '{pr: (.required_pull_request_reviews!=null), admins: .enforce_admins.enabled, noforce: (.allow_force_pushes.enabled|not)}'
# want: {"pr":true,"admins":true,"noforce":true}
```

Until all three are true the controller writes `$ORCH/state/skills-blocked-<repo_id>` and
`skillselect` no-ops for that repo. This is the intended posture, not a bug.

---

## DEPLOY (dark — selection stays OFF)

1. **Migration** — deploy the code and restart `serve`; `migrate()` applies `0005_skill.sql`
   idempotently under its advisory lock (auto-migrates on boot). Verify the tables exist:
   ```
   psql "$MYNDAIX_DSN" -c '\d skill' -c '\d skill_use'
   ```
   They are inert until a caller runs.

2. **play-review.sh** — install the trusted fixed-path copy (the inject seam is a no-op until
   the flag exists, so this is safe to ship dark):
   ```
   cp orchestrator/play-review.sh "$ORCH/play-review.sh"
   ```

3. **Controller + skillselect** — deploy the runtime package. The next controller tick will,
   for each repo: resolve `nameWithOwner`, verify branch protection, and either index `skills/`
   (if protected) or write the per-repo block flag (if not). Selection is still OFF. Dry-run it
   first to see exactly what it would do, writing nothing:
   ```
   MYNDAIX_CONTROLLER_DRY_RUN=1 PYTHONPATH=src python3 -m runtime.controller tick
   ```

---

## SEED the corpus (the human merge IS the arm)

Open a normal PR adding `skills/<name>/SKILL.md`:

```
---
name: <kebab-slug>                 # MUST equal the directory name
description: <=60 chars, one line   # required; the silent-no-route guard
path_trigger: src/*.swift           # path-SEGMENT glob; never *, **/*, or bare dir/*
---

<plain-text review guidance, <=2048 chars. DESCRIPTIVE only — no executable affordances,
no `allowed-tools:`/`scripts:` keys. A reviewer reads this as reference, not instructions.>
```

play-review reviews the body on push (free); `classify_diff` routes `skills/` to a **human**
(it is denylisted from auto-merge). **Your merge under branch protection is the arm.** The next
controller tick lints it, stamps `provenance='promoted'`, and indexes it `active`. A lint
failure (bad slug / over-cap / banned trigger / injection-framing body / affordance key) is
skipped with a LOUD jefe-inbox alert — fix and re-merge.

Confirm it indexed:
```
psql "$MYNDAIX_DSN" -c "select name, state, path_trigger from skill where repo_scope='<repo_id>';"
```

---

## ARM (atomic, human)

```
touch "$ORCH/SKILLS_ENABLED"
```

Because the controller indexes regardless of this flag, arming hits a warm, already-vetted
table — no cold start. The next matching push gets `<=2` fenced hints stapled under BOTH
reviewer prompts (never the triage prompt, never the merge gate).

**Smoke-verify the inject:** push a change matching a skill's `path_trigger`, then grep the run
dir for the fence:
```
grep -rl "BEGIN UNTRUSTED armed-skill" "$ORCH/runs/" | tail -1
```

---

## ROLLBACK (instant, no code revert)

```
rm "$ORCH/SKILLS_ENABLED"
```

Selection no-ops everywhere immediately. A branch-protection drop auto-rolls-back that one repo
via the block flag with **no human action** (re-checked every tick). To fully retire: leave the
flag off — the tables are inert; archived rows are never deleted (reversible by design).

---

## What v1 deliberately does NOT do

Auto-capture / auto-proposer · executable skill affordances · LLM consolidation · skill DELETE ·
a separate `skill-tick.sh` (prune runs inline in the controller tick) · injection into the
auto-merge gate (accepted gap) · semantic/embedding retrieval. See the design's "OUT of v1".
