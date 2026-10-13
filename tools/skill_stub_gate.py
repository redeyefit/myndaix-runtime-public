#!/usr/bin/env python3
"""Refuse to merge an un-authored auto-proposed SKILL.md (the CI half of capture.py's K4 gate).

The S7 proposer opens PRs whose SKILL.md body carries STUB_MARKER until a human authors the real
lesson. If such a stub merges unedited, reconcile marks the capture class terminally "promoted"
while the controller's index step rejects the very same blob — the class can never be re-proposed
and no usable guidance ever lands. This gate turns that dead end into a red PR check BEFORE merge.

Judgment is delegated to runtime.capture.is_unauthored_stub — the SAME predicate the controller's
index gate runs — so the CI gate and the promotion gate can never drift. Never replace this with a
hash(final_body) != draft_sha comparison: the design round proved that shape unconditionally
fail-open (representation mismatch between the compared bodies).

Usage:  skill_stub_gate.py <path> ...   (only skills/**/SKILL.md paths are judged; others ignored)
"""
from __future__ import annotations

import sys
from pathlib import Path

# The workflow invokes this with the venv python + PYTHONPATH=src, but the gate must also hold when
# run bare (operator shell, a future workflow edit that drops the env) — resolve src/ from this
# file's own location so the import can't silently fall back to a stale installed runtime package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from runtime.capture import is_unauthored_stub  # noqa: E402


def main(argv: list[str]) -> int:
    # Filter HERE, not via git pathspec globs in the workflow: bash/git `**` wildmatch semantics
    # vary, and a mangled (word-split) path falls through to the unreadable branch below instead
    # of silently widening or narrowing the match.
    paths = [p for p in argv[1:] if p.split("/")[0] == "skills" and p.endswith("/SKILL.md")]
    if not paths:
        print("skill_stub_gate: no skills/**/SKILL.md paths to check — OK")
        return 0
    violations: list[str] = []
    for p in paths:
        try:
            content = Path(p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # The workflow diffs with --diff-filter=AMR (deletions excluded), so every path passed
            # is expected to exist and decode — a missing/binary/mangled-name file is an ERROR,
            # never a pass (fail-closed: "could not judge" must not read as "authored").
            violations.append(f"{p}: unreadable ({e.__class__.__name__}: {e}) — fail-closed")
            continue
        if is_unauthored_stub(content):
            violations.append(f"{p}: un-authored auto-proposed stub (STUB_MARKER present, or "
                              "empty) — author the real lesson before merge")
    if violations:
        sys.stderr.write("skill_stub_gate: REFUSED — an unedited auto-proposed stub must never "
                         "merge (it would terminally consume its capture class):\n")
        for v in violations:
            sys.stderr.write(f"  {v}\n")
        return 1
    print(f"skill_stub_gate: {len(paths)} SKILL.md file(s) authored — OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
