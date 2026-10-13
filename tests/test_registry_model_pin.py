"""Registry model pin: every `claude` CLI row must PIN its model in argv, and the pinned alias must equal
the row's declared `model`.

Why: a bare `claude -p` runs the HOST's default model, which tracks whatever the operator last picked
interactively and differs per machine. A row that DECLARES a model but does not pass it describes intent
without enforcing it (mack declared "opus" and mini declared "claude" while both ran the host default).

Zero-dep, <1s:   PYTHONPATH=src python3 tests/test_registry_model_pin.py
"""
import sys

from runtime.registry import V1_ROSTER


def unpinned_claude_rows(roster):
    """Return one problem string per claude CLI row whose argv does not carry --model == spec.model."""
    problems = []
    for spec in roster:
        ad = spec.adapter or {}
        argv = ad.get("argv") or []
        if ad.get("kind") != "cli" or not argv or argv[0] != "claude":
            continue
        if "--model" not in argv:
            problems.append(f"{spec.agent_id}: argv has no --model (declared model={spec.model!r})")
            continue
        i = argv.index("--model")
        pinned = argv[i + 1] if i + 1 < len(argv) else None
        if pinned != spec.model:
            problems.append(f"{spec.agent_id}: argv pins {pinned!r} but declares model={spec.model!r}")
    return problems


def test_shipped_roster_is_pinned():
    problems = unpinned_claude_rows(V1_ROSTER)
    assert not problems, "unpinned/mismatched claude rows: " + "; ".join(problems)


def test_mack_and_mini_are_checked():
    # guard the guard: the two rows that motivated this test must actually be claude CLI rows in scope,
    # so a future refactor that changes their argv shape cannot make the check silently skip them.
    ids = {s.agent_id for s in V1_ROSTER
           if (s.adapter or {}).get("kind") == "cli" and ((s.adapter or {}).get("argv") or [""])[0] == "claude"}
    assert {"mack", "mini"} <= ids, f"mack/mini not seen as claude CLI rows: {sorted(ids)}"


def test_detects_unpinned_and_mismatched():
    base = next(s for s in V1_ROSTER if s.agent_id == "mack")
    bare = base.model_copy(update={"adapter": {**base.adapter, "argv": ["claude", "-p"]}})
    assert unpinned_claude_rows([bare]), "bare argv must be flagged"
    mismatch = base.model_copy(update={"model": "opus"})  # argv still pins sonnet
    assert unpinned_claude_rows([mismatch]), "declared/pinned mismatch must be flagged"
    dangling = base.model_copy(update={"adapter": {**base.adapter, "argv": ["claude", "-p", "--model"]}})
    assert unpinned_claude_rows([dangling]), "--model with no value must be flagged"
    assert not unpinned_claude_rows([base]), "a correctly pinned row must pass"


def agy_label_to_model(label):
    """'Gemini 3.8 Flash (High)' -> 'gemini-3.8-flash': the picker label minus its effort suffix, as a model id."""
    base = label.split(" (", 1)[0]
    return base.strip().lower().replace(" ", "-")


def mismatched_agy_rows(roster):
    """Return one problem string per agy CLI row whose argv label does not name the row's declared model.

    Why: the oracle row states its model twice (the declared `model` and the picker label in argv). A re-pin
    that changes one and not the other leaves the roster describing a reviewer it is not running. This check
    can NOT prove the label exists in Google's catalog (agy fetches that per run); only a live call can.
    """
    problems = []
    for spec in roster:
        ad = spec.adapter or {}
        argv = ad.get("argv") or []
        if ad.get("kind") != "cli" or not argv or argv[0] != "agy":
            continue
        if "--model" not in argv:
            problems.append(f"{spec.agent_id}: agy argv has no --model (declared model={spec.model!r})")
            continue
        i = argv.index("--model")
        label = argv[i + 1] if i + 1 < len(argv) else None
        if label is None or agy_label_to_model(label) != spec.model:
            problems.append(f"{spec.agent_id}: agy argv pins {label!r} but declares model={spec.model!r}")
    return problems


def test_oracle_label_matches_declared_model():
    ids = {s.agent_id for s in V1_ROSTER
           if (s.adapter or {}).get("kind") == "cli" and ((s.adapter or {}).get("argv") or [""])[0] == "agy"}
    assert "oracle" in ids, f"oracle not seen as an agy CLI row: {sorted(ids)}"  # guard the guard
    problems = mismatched_agy_rows(V1_ROSTER)
    assert not problems, "agy rows whose label and declared model disagree: " + "; ".join(problems)


def test_detects_half_applied_agy_repin():
    base = next(s for s in V1_ROSTER if s.agent_id == "oracle")
    stale_declared = base.model_copy(update={"model": "gemini-3.1-pro"})  # argv moved, declared model did not
    assert mismatched_agy_rows([stale_declared]), "a declared model the label does not name must be flagged"
    bare = base.model_copy(update={"adapter": {**base.adapter, "argv": ["agy", "--disable-slash-commands", "-p"]}})
    assert mismatched_agy_rows([bare]), "an agy argv with no --model must be flagged"
    dangling = base.model_copy(update={"adapter": {**base.adapter, "argv": ["agy", "-p", "--model"]}})
    assert mismatched_agy_rows([dangling]), "--model with no label must be flagged"
    assert not mismatched_agy_rows([base]), "the shipped oracle row must pass"


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
