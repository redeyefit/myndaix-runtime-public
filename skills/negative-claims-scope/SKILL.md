---
name: negative-claims-scope
description: Negative claims need a stated search scope or exhaustiveness
path_trigger: "docs/*.md docs/reviews/*.md docs/research/*.md"
---

Positive claims verify cheap (one observation proves them). Negative claims verify
expensive (only an exhaustive search proves them) — and docs/briefs/verdicts routinely
launder "not found in the places I looked" into "does not exist", stamped VERIFIED.
The generalization is invention wearing evidence's clothes, and in auto-loaded channels
(design docs, work-order briefs, review verdicts) it becomes ground truth for the next
reader.

Sound forms: scope-stamped ("no X in the four paths checked: A, B, C, D") or
exhaustively derived ("the config enumerates all roots; none contains X"). Unsound:
"no X anywhere/at all/exists" backed by a bounded search; "VERIFIED" blessing the
conclusion rather than naming what was observed.

Flag: absolute negative claims in designs/briefs whose evidence section shows a bounded
search; "VERIFIED"/"confirmed" labels that don't name the observation; inventory tables
claiming completeness without stating the enumeration method.

(Learned 2026-09-03: a cross-lane brief declared "the Mini has NO FieldVision clone
anywhere" after checking four paths — the clone lived in a fifth (~/Developer), the
refuted diagnosis had been right, and the wrong brief auto-loaded into the next session's
context. Caught only because the receiving session verified before obeying.)
