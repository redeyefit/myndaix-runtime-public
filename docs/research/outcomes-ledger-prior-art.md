# Outcomes Ledger — Prior-Art Brief (the self-learning rung)

_Produced 2026-06-29 by the deep-research harness (101 agents, 19 sources, 89 claims → 25 verified → 17 confirmed) + Mack synthesis. Implementation-ready input to the eventual `/feature` design doc. NOT yet a design — this is the prior-art homework so the build is right._

## TL;DR
The outcome label the brain is missing = **a per-finding state machine + a constrained dismissal-reason enum, keyed by `rule_tag` + a SonarQube-style line-hash so a finding has a stable identity across reviews.** The cheapest reliable signal for a solo operator is **automatic** (did-fix-land on rescan + git-revert detection), NOT manual labeling. Feed it into **plain SQL precision dials** that suppress a noisy `(class × reviewer-family)` below a precision floor and promote high-precision ones — every threshold feature-flagged + reversible. Build it as **one append-only Postgres table + computed views** on the existing spine. Reject fine-tuning/embeddings/vector-stores/dashboards.

---

## A. Confirmed prior-art patterns (cited, 3-0 adversarially verified)

1. **Per-finding outcome STATE MACHINE is the universal pattern.** GitHub code-scanning, Semgrep, and SonarQube each model every finding through an explicit lifecycle (open → fixed / dismissed / reverted), not a fire-and-forget verdict. _(GitHub code-scanning API; Semgrep triage; SonarQube issues — all primary docs.)_
2. **Stable finding identity ACROSS scans = a line-hash, not a line number.** SonarQube assigns a stable per-finding identity that survives diff shifts (the surrounding code is hashed, not the line number). This is the answer to "how a finding gets a stable key across reviews." _(SonarQube issues docs.)_
3. **Dismissal is a CONSTRAINED reason enum, not free text.** GitHub ("false positive" / "won't fix" / "used in tests") and SonarQube (false-positive vs accepted/won't-fix) both capture a structured dismissal reason. **The load-bearing split: `false_positive` (reviewer was WRONG) vs `wont_fix`/`accepted` (reviewer was RIGHT, human declines).** Only the former should down-weight a finding-class. _(GitHub + SonarQube primary docs.)_
4. **"Fixed" is derived AUTOMATICALLY on rescan.** SonarQube auto-marks a finding Fixed when it's gone on the next scan — the cheap, no-labeling did-fix-land signal. _(SonarQube; mirrored by Semgrep per a borrow-lead below.)_
5. **The precision lever = suppress a class when its confirmed precision drops below a threshold.** Deterministic, per-class, no retraining. _(verified.)_
6. **LLM-judge reliability = treat the judge as a CLASSIFIER and measure precision/recall per judge.** LLM judges have systematic, measurable biases; reliability decomposes into executable dimensions and aggregated outcome labels produce a measurable precision metric. → here: track precision **per reviewer-family × finding-class**. _(arXiv 2412.12509, LangChain, Kinde, ScienceDirect.)_

## B. Borrow-leads (the verifier over-killed these on single-source airtightness — REAL patterns, confirm at build time)
- **CodeRabbit** persists "learnings" as records with a **usage-count** but does NOT track accept/dismiss/revert — i.e. even a leading LLM reviewer leaves the outcome label unfilled (the exact gap we fill). _(CodeRabbit docs.)_
- **Greptile** auto-suppresses a comment class after **N ignores** (e.g. 3) — a deterministic per-class threshold, not model retraining. _(Greptile docs.)_
- **GitHub** dismissal reason **drives future suppression**, not just cosmetic. _(GitHub docs.)_
- **AIMultiple** benchmark: LLM reviewers' dominant failure is **false NEGATIVES (misses)**, not false positives → also track a MISS signal, don't optimize noise alone. _(AIMultiple.)_

---

## C. Recommended `finding_outcome` schema — **BUILD** (one append-only table on the spine)

```
finding_outcome (append-only; current state = latest event per finding_key)
  id            uuid pk
  finding_key   text     -- sha256(repo_id || rule_tag || line_hash)  [SonarQube line-hash pattern]
  rule_tag      text     -- finding-class, from the existing capture taxonomy
  reviewer_family text   -- kilabz | oracle | lobster (which family raised/confirmed it)
  repo_id       text
  review_run_id text     -- the play/controller run that raised it
  base_sha      text
  outcome       text     -- open | applied_fixed | dismissed_false_positive | dismissed_wontfix | reverted | expired
  outcome_source text    -- auto_fix_landed | auto_git_revert | human_dismiss | human_apply | timeout
  created_at    timestamptz default now()
```
- **`finding_key` = the SonarQube line-hash trick**: hash the normalized surrounding code line, NOT the line number → the same finding is tracked even when the diff shifts lines. ~20 lines to implement.
- Append-only (north-star litmus): never UPDATE; the latest row per `finding_key` is the state. A computed view exposes current state.

## D. Outcome-capture mechanisms — ranked by reliability ÷ cost (solo, local-first)
1. **AUTO did-fix-land** — on the next review of the same area, the flagged code changed toward the fix → `applied_fixed`. Cheapest, zero labeling. (SonarQube auto-Fixed.)
2. **AUTO git-revert** — a later commit reverts a merged fix → flip `applied_fixed` → `reverted` (strong negative signal). (git; the brief's premise.)
3. **HUMAN dismissal reason** — when Jefe acts on a verdict in the inbox, ONE constrained enum (`false_positive` vs `wont_fix`). High quality, one click, separates wrong-reviewer from human-declines.
4. **REJECT** — per-finding thumbs-up/down on every finding (too much solo overhead).

## E. Deterministic SQL dials — compute / auto-act / human-gate
- **COMPUTE** (SQL views over `finding_outcome`): per `(rule_tag × reviewer_family)` — `precision = applied_fixed / (applied_fixed + dismissed_false_positive)`; `revert_rate`; `volume`; recency-weighted.
- **AUTO-ACT** (feature-flagged, reversible, volume-floored): precision < floor over ≥N observations → **SUPPRESS** that class for that family (drop/down-rank in triage); precision > ceiling → **PROMOTE** (high-confidence). Mirrors Greptile suppress-after-N + SonarQube precision lever.
- **HUMAN-GATE** (never auto): suppressing a security/correctness class; promoting a class into the auto-merge gate; the threshold values themselves; any action on < N observations.

## F. BUILD / ADOPT / BORROW verdicts
| Capability | Verdict | Why |
|---|---|---|
| `finding_outcome` append-only table | **BUILD** | one table on the existing Postgres spine; trivial, owned |
| stable line-hash finding identity | **BORROW PATTERN** | SonarQube line-hash, ~20 lines |
| dismissal-reason enum (FP vs wont_fix) | **BORROW PATTERN** | GitHub/SonarQube taxonomy; the FP/wont-fix split is load-bearing |
| precision dials (suppress/promote) | **BUILD** | SQL views + a feature-flagged suppressor in the controller; no lib |
| per-family precision calibration | **BORROW PATTERN** | treat reviewer as a classifier; no eval framework |
| any vendor tool wholesale | **ADOPT nothing** | all SaaS/enterprise; none fit local-first Postgres solo |

## G. Do NOT build (enterprise bloat to reject)
- Fine-tuning / RLAIF / embeddings / a vector store for "learnings" — the explicit constraint; deterministic SQL is the whole point.
- A mutable vendor-style "learnings" store (CodeRabbit shape) — append-only ledger + computed views instead.
- A UI / issue-management dashboard / triage workflow app — the solo operator acts via the inbox + SQL.
- Inter-rater-reliability statistics machinery (Cohen's kappa, etc.) — per-family precision counts suffice.
- Per-finding human thumbs-up/down labeling — lean on the auto signals.

---

## Methodology note (be honest about the run)
The harness extracted 89 claims, verified the top 25, **confirmed 17 / killed 8**. The adversarial verification was conservative — several "killed" claims (GitHub dismissal-drives-suppression, Semgrep auto-fix-detection on rescan, Greptile suppress-after-3) are real patterns that failed *single-source* airtightness; they're retained above as **borrow-leads to confirm at build time**, not discarded. The 17 confirmed claims + 19 sources ground the core. Net: the architecture (append-only outcome ledger + line-hash identity + dismissal enum + auto-capture + SQL precision dials) is well-supported; the specific per-tool thresholds are leads, not gospel.

## Sources (primary first)
- SonarQube issues / lifecycle: docs.sonarsource.com/sonarqube/latest/user-guide/issues/ · sonarqube-server/10.6/user-guide/issues/managing · blog/how-sonarqube-minimizes-false-positives
- GitHub code-scanning: docs.github.com/en/rest/code-scanning · .../managing-code-scanning-alerts/resolving-code-scanning-alerts
- Semgrep: semgrep.dev/docs/semgrep-code/triage-remediation · /docs/semgrep-assistant/metrics · /blog/2023/gpt4-and-semgrep-detailed
- CodeRabbit: docs.coderabbit.ai/knowledge-base/learnings · Greptile: greptile.com/docs/how-greptile-works/memory-and-learning
- LLM-as-judge calibration: arxiv.org/html/2412.12509v2 · langchain.com/resources/llm-as-a-judge · kinde.com/learn/.../llm-as-a-judge-done-right · sciencedirect.com/science/article/pii/S2666675825004564
- Benchmark: aimultiple.com/ai-code-review-tools · Graphite: graphite.com/guides/resolving-comments-conversations-github
