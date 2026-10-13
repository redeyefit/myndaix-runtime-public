---
name: fail-closed-filter
description: A guard whose checking tool can fail must deny, not proceed
path_trigger: "tools/*.sh substrate/*.sh substrate/lab/*.sh orchestrator/*.sh orchestrator/phone/*.sh"
---

`if printf '%s' "$input" | grep -q BAD; then deny; fi` is fail-OPEN: when grep itself
fails (missing binary, broken PATH, OOM, rc 2), the condition is false and hostile input
sails through the exact gate built to stop it. The same shape hides in `[[ $x -ge $CAP ]]`
under `set -e` inside an `if` (a bash arithmetic ERROR evaluates as false → cap skipped)
and in any validator invoked with its failure conflated with "no match".

The safe shape demands POSITIVE PROOF OF CLEAN: capture the tool's rc and output
separately, and proceed only on the exact clean signature — e.g. for grep -c, proceed
only when rc==1 AND count=="0"; anything else (rc 0 = match, rc 2 = error, rc 127 =
missing) denies. Validate limits/caps as numeric BEFORE using them in arithmetic.

Flag: deny-branches gated on a tool's success-at-finding; validators whose own failure
path is unreachable or conflated with the pass path; caps/limits used unguarded in test
arithmetic under set -e.

(Learned 2026-09-03: oracle caught the fail-open control-char filter in mxr-phone (H-1);
the same day oracle also FALSE-flagged DAILY_CAP as unguarded — the guard existed at the
top of the file — so check the whole validation chain before flagging, and cite the
missing link precisely.)
