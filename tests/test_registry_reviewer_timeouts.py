"""Reviewer per-attempt caps vs play-review's sync wait.

Why: play-review.sh waits RCT_PUSH (default 1200s) for ONE review call. The pool kills an attempt at the
row's Profile.timeout_s and retries. A reviewer whose cap sits INSIDE its own latency distribution dies
3x in a row and the verdict degrades to single-family; a cap LARGER than the wait strands the finished
reply in the ledger (2026-07-03). Both reviewers the lane calls must therefore share one cap that fits
inside the wait with queue/startup margin. 2026-10-10: oracle had NO profile (300s default) while its
reviews took 180-300s — 5 dead jobs, 5 single-family verdicts in one evening.

Zero-dep, <1s:   PYTHONPATH=src python3 tests/test_registry_reviewer_timeouts.py
"""
import re
import sys
from pathlib import Path

from runtime.registry import V1_ROSTER

REPO = Path(__file__).resolve().parent.parent
PLAY_REVIEW = REPO / "orchestrator" / "play-review.sh"
# play-review's canary + startup slack: the wait must exceed one attempt by at least this much
MARGIN_S = 180


def lane_wait_s() -> int:
    """RCT_PUSH's shipped default, read from the script so this test fails if the two files drift."""
    m = re.search(r'^RCT_PUSH="\$\{PLAY_REVIEW_CALL_TIMEOUT:-(\d+)\}"', PLAY_REVIEW.read_text(), re.M)
    assert m, "RCT_PUSH default not found in play-review.sh (regex drift?)"
    return int(m.group(1))


def spec(agent_id):
    return next(s for s in V1_ROSTER if s.agent_id == agent_id)


def test_oracle_cap_matches_kilabz():
    o, k = spec("oracle").profile.timeout_s, spec("kilabz").profile.timeout_s
    assert o == k, f"oracle cap {o}s != kilabz cap {k}s — the lane wait was sized for one cap"


def test_reviewer_caps_fit_the_lane_wait():
    wait = lane_wait_s()
    for agent_id in ("kilabz", "oracle"):
        cap = spec(agent_id).profile.timeout_s
        assert cap + MARGIN_S <= wait, f"{agent_id} cap {cap}s + {MARGIN_S}s margin exceeds RCT_PUSH {wait}s"


def test_oracle_cap_is_not_the_dead_default():
    from runtime.contracts import Profile
    assert spec("oracle").profile.timeout_s != Profile().timeout_s, "oracle fell back to the default cap"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    print("PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
