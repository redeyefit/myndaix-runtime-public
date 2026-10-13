"""shadow-dial pure core (design v0.6 §7) — Wilson math, THE BOUND-DIRECTION INVARIANT (the
load-bearing tests), three-way pair handling, classification edges, recency, provenance, the §4
eval/arming gate sub-gate by sub-gate, and fail-safe env parsing. DB-free, like test_outcomes.py.

Run:  PYTHONPATH=src python3 tests/test_dialshadow.py
"""
import datetime as dt

import runtime.dialshadow as D

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


P = D.ShadowParams()          # the design defaults


def row(tag="fail-open", family="kilabz", outcome="confirmed_real",
        source="human_confirm", seq=1, ref="r1", play="p1"):
    return {"rule_tag": tag, "reviewer_family": family, "outcome": outcome,
            "outcome_source": source, "seq": seq, "ref": ref, "play": play}


def real(seq, ref="r1", play="p1", tag="fail-open", family="kilabz"):
    return row(tag, family, "confirmed_real", "human_confirm", seq, ref, play)


def fp(seq, ref="r1", play="p1", tag="fail-open", family="kilabz"):
    return row(tag, family, "dismissed_false_positive", "human_dismiss", seq, ref, play)


def wontfix(seq, ref="r1", play="p1", tag="fail-open", family="kilabz"):
    return row(tag, family, "dismissed_wontfix", "human_dismiss", seq, ref, play)


def spread(rows):
    """Give a row list ≥2 distinct refs + plays so provenance never trips a test that isn't
    about provenance."""
    for i, r in enumerate(rows):
        r["ref"] = f"ref{i % 3}"
        r["play"] = f"play{i % 3}"
    return rows


def one_cell(rows, params=P):
    cells = D.aggregate_cells(rows, params)
    assert len(cells) == 1, f"expected 1 cell, got {len(cells)}"
    return cells[0]


# ---- Wilson bound math (§7: known (k,n) → known [lo,hi] within tolerance) ------------------------
def test_wilson_known_values():
    lo, hi = D.wilson(5, 10)
    ok(abs(lo - 0.2366) < 0.005 and abs(hi - 0.7635) < 0.005,
       f"wilson(5,10) ≈ [0.24,0.76], got [{lo:.4f},{hi:.4f}]")
    lo, hi = D.wilson(0, 12)
    ok(lo == 0.0 and abs(hi - 0.2425) < 0.005, f"wilson(0,12) ≈ [0.00,0.24], got [{lo:.4f},{hi:.4f}]")
    lo, hi = D.wilson(12, 12)
    ok(hi == 1.0 and abs(lo - (1 - 0.2425)) < 0.005, "wilson(12,12) mirrors wilson(0,12)")
    lo, hi = D.wilson(0, 3)
    ok(abs(hi - 0.5615) < 0.005, f"wilson(0,3) hi ≈ 0.56 (the design §3 example), got {hi:.4f}")
    ok(D.wilson(0, 0) is None, "n=0 → None (no divide, no fake certainty)")
    lo, hi = D.wilson(1, 1)
    ok(0.0 <= lo <= hi <= 1.0, "interval clamped to [0,1]")
    try:
        D.wilson(5, 3)
        ok(False, "k>n must raise")
    except ValueError:
        ok(True, "k>n raises ValueError")


# ---- B1 direction (§7: THE LOAD-BEARING TESTS) ---------------------------------------------------
def test_direction_middling_sample_is_hold_not_suppress():
    # k=5,n=10 → p=0.5, interval ≈[0.24,0.76]. lo(0.24) < FLOOR(0.30): a LO-read gate would
    # wrongly suppress. The gate MUST read hi(0.76) — not below the floor → hold.
    rows = spread([real(i) for i in range(1, 6)] + [fp(i) for i in range(6, 11)])
    c = one_cell(rows)
    ok(c["n"] == 10 and c["would_say"] == "hold",
       f"middling (5/10) is HOLD, not would-suppress (gate must read hi) — got {c['would_say']}")


def test_direction_genuinely_low_sample_would_suppress():
    # k=0,n=12 → hi≈0.24 < 0.30, fp=12 ≥ 3 → would-suppress.
    rows = spread([fp(i) for i in range(1, 13)])
    c = one_cell(rows)
    ok(c["would_say"] == "would-suppress",
       f"0/12 (hi≈0.24<0.30) → would-suppress, got {c['would_say']}")
    ok(c["suppressible"] is False, "SUPPRESSIBLE is empty → even a would-suppress is not suppressible")


def test_direction_high_sample_would_trust():
    # k=n=60 → lo≈0.94 ≥ 0.90 → would-trust. (k=n=12 has lo≈0.76 — NOT enough, also asserted.)
    rows = spread([real(i) for i in range(1, 61)])
    c = one_cell(rows)
    ok(c["would_say"] == "would-trust", f"60/60 (lo≥0.90) → would-trust, got {c['would_say']}")
    rows = spread([real(i) for i in range(1, 13)])
    c = one_cell(rows)
    ok(c["would_say"] == "hold",
       f"12/12 (lo≈0.76 < 0.90) is HOLD — perfect-but-thin never trusts on the point rate, got {c['would_say']}")


# ---- classification edges (§7) -------------------------------------------------------------------
def test_insufficient_below_min_n():
    rows = spread([fp(i) for i in range(1, 9)])          # n=8 < 10 — the design §3 example
    c = one_cell(rows)
    ok(c["would_say"] == "insufficient" and "n<10" in c["reason"],
       f"n=8 → insufficient (not hold), got {c['would_say']} ({c['reason']})")


def test_insufficient_thin_provenance_even_at_high_n():
    rows = [fp(i, ref="only-ref", play=f"play{i%3}") for i in range(1, 25)]
    c = one_cell(rows)
    ok(c["would_say"] == "insufficient" and "refs<2" in c["reason"],
       f"24 labels from ONE ref → insufficient, got {c['would_say']} ({c['reason']})")
    rows = [fp(i, ref=f"ref{i%3}", play="only-play") for i in range(1, 25)]
    c = one_cell(rows)
    ok(c["would_say"] == "insufficient" and "plays<2" in c["reason"],
       f"24 labels from ONE play → insufficient, got {c['would_say']} ({c['reason']})")


def test_min_fp_mass_guard():
    # hi<floor with fp≥3 impossible at fp<3 via wilson? craft: fp=2 real=0 gives n=2 <min_n. So
    # test the guard in isolation: n≥10 with hi<floor requires fp≥... use fp=12: passes. To hit
    # the guard, drop min_fp astronomically via params (the guard is separately live).
    p_hard = D.ShadowParams(min_fp=20)
    rows = spread([fp(i) for i in range(1, 13)])
    c = one_cell(rows, p_hard)
    ok(c["would_say"] == "hold",
       f"hi<floor but fp(12)<min_fp(20) → NOT would-suppress, got {c['would_say']}")


def test_missing_provenance_counts_as_none():
    rows = [fp(i, ref=None, play=None) for i in range(1, 13)]
    c = one_cell(rows)
    ok(c["distinct_refs"] == 0 and c["distinct_plays"] == 0 and c["would_say"] == "insufficient",
       "None ref/play → zero provenance (fail-safe: never MORE distinct)")


# ---- three-way pair handling (§7, MAJOR-r3) ------------------------------------------------------
def test_wontfix_leaves_n_provenance_invalid_unchanged():
    base = spread([real(i) for i in range(1, 6)] + [fp(i) for i in range(6, 11)])
    with_wf = base + [wontfix(99, ref="ref-NEW", play="play-NEW")]
    c0, c1 = one_cell(base), one_cell(with_wf)
    ok(c0["n"] == c1["n"] == 10, "wontfix leaves n unchanged")
    ok(c0["distinct_refs"] == c1["distinct_refs"] and c0["distinct_plays"] == c1["distinct_plays"],
       "wontfix leaves provenance unchanged (its NEW ref/play not counted)")
    ok(c1["invalid"] == 0, "wontfix is LEGAL-excluded — NOT the invalid alarm")
    ok(c0["would_say"] == c1["would_say"], "wontfix cannot flip a classification")


def test_forged_offset_pair_increments_only_invalid():
    base = spread([real(i) for i in range(1, 6)] + [fp(i) for i in range(6, 11)])
    forged = base + [
        row(outcome="applied_fixed", source="auto_fix_landed", seq=98, ref="rX", play="pX"),
        row(outcome="confirmed_real", source="human_dismiss", seq=97, ref="rY", play="pY"),
        row(outcome="dismissed_false_positive", source="human_confirm", seq=96),
    ]
    c0, c1 = one_cell(base), one_cell(forged)
    ok(c1["invalid"] == 3, f"3 off-set pairs → invalid=3, got {c1['invalid']}")
    ok(c1["n"] == c0["n"] and c1["confirmed_real"] == c0["confirmed_real"]
       and c1["dismissed_fp"] == c0["dismissed_fp"], "off-set pairs never enter n")
    ok(c1["distinct_refs"] == c0["distinct_refs"], "off-set pairs never enter provenance")
    ok(c1["would_say"] == c0["would_say"], "off-set pairs never flip a classification")


def test_classify_pair_exact():
    ok(D.classify_pair("human_confirm", "confirmed_real") == D.PAIR_REAL, "confirm pair")
    ok(D.classify_pair("human_dismiss", "dismissed_false_positive") == D.PAIR_FP, "fp pair")
    ok(D.classify_pair("human_dismiss", "dismissed_wontfix") == D.PAIR_EXCLUDED, "wontfix = legal excluded")
    for s, o in [("human_confirm", "dismissed_false_positive"), ("human_dismiss", "confirmed_real"),
                 ("auto_fix_landed", "applied_fixed"), ("review_raised", "open"),
                 ("panel_proposed", "confirmed_real"), ("", "")]:
        ok(D.classify_pair(s, o) == D.PAIR_INVALID, f"({s},{o}) is INVALID (exact pairs only)")


# ---- recency window (§7 M3) ----------------------------------------------------------------------
def test_recency_recovering_class_visible():
    # 30 old fp's then 12 recent confirms, recency_n=12 for a clean split: all-time hi below…
    p = D.ShadowParams(recency_n=12)
    rows = spread([fp(i) for i in range(1, 31)] + [real(i) for i in range(31, 43)])
    c = one_cell(rows, p)
    ok(c["n"] == 42 and c["n_recent"] == 12, "recent window = last recency_n by seq")
    ok(c["precision_recent"] == 1.0 and c["precision"] < 0.3,
       "recovering class: recent 1.0 while all-time lags (M3 visible, not hidden)")
    ok(c["wilson_recent_hi"] > c["wilson_hi"],
       "recent hi ABOVE all-time hi → §4.2 both-windows would refuse arming")


def test_recency_order_is_seq_not_input_order():
    p = D.ShadowParams(recency_n=5)
    rows = spread([real(i) for i in range(10, 15)] + [fp(i) for i in range(1, 6)])  # confirms NEWER
    c = one_cell(rows, p)
    ok(c["precision_recent"] == 1.0, "recency sorts by seq (input order must not matter)")


def test_recency_literal_s7_shape_alltime_below_recent_above():
    # the LITERAL §7 bullet: old fp's + recent confirms with all-time hi BELOW the floor while
    # recent hi is ABOVE it — the exact would-suppress-but-recovering shape §4.2 must refuse.
    # (snap/weekly/cohort helpers are defined in the eval section below; tests run at __main__.)
    p = D.ShadowParams(recency_n=5)
    rows = spread([fp(i) for i in range(1, 38)] + [real(i) for i in range(38, 41)])  # 3/40 real
    c = one_cell(rows, p)
    ok(c["wilson_hi"] < p.floor, f"all-time hi {c['wilson_hi']:.3f} < floor (suppress territory)")
    ok(c["wilson_recent_hi"] > p.floor,
       f"recent hi {c['wilson_recent_hi']:.3f} > floor — the recovery is visible, not hidden (M3)")
    ok(c["would_say"] == "would-suppress" and c["dismissed_fp"] >= p.min_fp,
       "the CLASSIFIER may still say would-suppress on all-time…")
    ev = D.eval_arming(
        [snap(t, hi=c["wilson_hi"], hi_r=c["wilson_recent_hi"]) for t in weekly(4)],
        cohort(14, 0))
    ok(ev["gates"]["both_windows"] is False and not ev["eligible"],
       "…but §4.2 both-windows refuses arming while the recent window is above the floor")


# ---- aggregation / display -----------------------------------------------------------------------
def test_cells_keyed_by_tag_and_family():
    rows = (spread([fp(i, tag="fail-open", family="kilabz") for i in range(1, 4)])
            + spread([real(i, tag="fail-open", family="oracle") for i in range(4, 7)])
            + spread([real(i, tag="missing-scoping", family="kilabz") for i in range(7, 10)]))
    cells = D.aggregate_cells(rows, P)
    ok(len(cells) == 3, f"3 (tag×family) cells, got {len(cells)}")
    keys = [(c["rule_tag"], c["reviewer_family"]) for c in cells]
    ok(keys == sorted(keys), "cells sorted deterministically")


def test_display_validation():
    ok(D.display_tag("fail-open") == "fail-open", "allowlisted tag displays verbatim")
    bad = D.display_tag("\x1b]0;evil\x07rm -rf")
    ok(bad.startswith("<off-taxonomy:") and "\x1b" not in bad,
       "off-taxonomy tag → deterministic placeholder, no raw bytes")
    ok(D.display_family("kilabz") == "kilabz" and D.display_family("oracle") == "oracle",
       "known families display verbatim")
    ok(D.display_family("evil\r\n").startswith("<off-family:"), "unknown family → placeholder")


def test_empty_ledger_honest():
    ok(D.aggregate_cells([], P) == [], "no rows → no cells")
    ok("nothing to measure" in D.format_table([], P), "empty table is an honest message")


def test_format_table_smoke():
    rows = spread([fp(i) for i in range(1, 9)])
    out = D.format_table(D.aggregate_cells(rows, P), P)
    ok("insufficient" in out and "no (n<10" in out, "the §3 example row renders (insufficient, no (n<10))")
    ok("would-say" in out.splitlines()[0], "header present")
    forged = rows + [row(outcome="applied_fixed", source="auto_fix_landed", seq=99)]
    out = D.format_table(D.aggregate_cells(forged, P), P)
    ok("fence-integrity alarm" in out, "nonzero invalid → the visible alarm line")


# ---- the §4 eval / arming gate -------------------------------------------------------------------
def snap(captured_at, would_say="would-suppress", cutoff=100, hi=0.20, hi_r=0.20, n_recent=10,
         **over):
    s = {"captured_at": captured_at, "data_cutoff_seq": cutoff, "would_say": would_say,
         "wilson_hi": hi, "wilson_recent_hi": hi_r, "n_recent": n_recent,
         "floor": 0.30, "ceiling": 0.90, "min_n": 10, "min_refs": 2, "min_plays": 2,
         "min_fp": 3, "recency_n": 30, "z": 1.96,
         "stable_snaps": 4, "eval_min_n": 10, "eval_agree": 0.70}
    s.update(over)
    return s


def weekly(n, start=dt.datetime(2026, 7, 6, 9, 0)):
    return [start + dt.timedelta(weeks=i) for i in range(n)]


def cohort(n_fp, n_real, start_seq=101):
    rows = [fp(start_seq + i, ref=f"ref{i%3}", play=f"play{i%3}") for i in range(n_fp)]
    rows += [real(start_seq + n_fp + i, ref=f"ref{i%3}", play=f"play{i%3}") for i in range(n_real)]
    return rows


def test_eval_all_gates_pass():
    snaps = [snap(t) for t in weekly(4)]
    ev = D.eval_arming(snaps, cohort(14, 0))     # 14/14 fp: wilson lo(14,14) ≈ 0.78 ≥ 0.70
    ok(ev["eligible"] is True, f"all five sub-gates pass → eligible, got {ev}")


def test_eval_stability_needs_distinct_weeks():
    day = dt.datetime(2026, 7, 6, 9, 0)
    snaps = [snap(day + dt.timedelta(hours=i)) for i in range(4)]      # 4 SAME-DAY snaps
    ev = D.eval_arming(snaps, cohort(14, 0))
    ok(ev["gates"]["stability"] is False and not ev["eligible"],
       "4 same-day snapshots FAIL stability (needs 4 distinct ISO weeks)")
    snaps = [snap(t) for t in weekly(3)]                               # only 3 snaps
    ev = D.eval_arming(snaps, cohort(14, 0))
    ok(ev["gates"]["stability"] is False, "3 would-suppress snapshots < stable_snaps(4) fails")


def test_eval_both_windows_required():
    snaps = [snap(t) for t in weekly(4)]
    snaps[-1]["wilson_recent_hi"] = 0.55                               # recovering in-window
    ev = D.eval_arming(snaps, cohort(14, 0))
    ok(ev["gates"]["both_windows"] is False and not ev["eligible"],
       "recent window above floor in LATEST snapshot → not eligible (M2)")
    snaps[-1]["wilson_recent_hi"] = None                               # no recent evidence at all
    ev = D.eval_arming(snaps, cohort(14, 0))
    ok(ev["gates"]["both_windows"] is False, "missing recent interval FAILS (absence ≠ pass)")


def test_eval_cohort_size_and_provenance():
    snaps = [snap(t) for t in weekly(4)]
    ev = D.eval_arming(snaps, cohort(9, 0))                            # 9 < eval_min_n(10)
    ok(ev["gates"]["cohort"] is False and not ev["eligible"], "cohort n=9 < 10 fails")
    thin = [fp(101 + i, ref="one-ref", play=f"play{i%3}") for i in range(14)]
    ev = D.eval_arming(snaps, thin)
    ok(ev["gates"]["cohort"] is False, "cohort from ONE ref fails (provenance guards apply to the cohort)")


def test_eval_agreement_needs_wilson_lower_bound():
    snaps = [snap(t) for t in weekly(4)]
    ev = D.eval_arming(snaps, cohort(7, 3))                            # 7/10: lo ≈ 0.40 < 0.70
    ok(ev["gates"]["agreement"] is False and not ev["eligible"],
       f"a bare 7/10 (lo≈0.40) does NOT pass agreement — got lo={ev['agree_lo']}")
    ok(ev["agree_lo"] is not None and abs(ev["agree_lo"] - 0.3968) < 0.01,
       f"agreement lo ≈ 0.40 for 7/10, got {ev['agree_lo']}")


def test_eval_empty_cohort_never_passes():
    snaps = [snap(t) for t in weekly(4)]
    ev = D.eval_arming(snaps, [])                                      # zero subsequent labels
    ok(not ev["eligible"] and ev["gates"]["nonempty_cohort"] is False,
       "zero subsequent labels → insufficient, never a pass (§4.5)")
    stale = [fp(50, ref="r0", play="p0"), real(60, ref="r1", play="p1")]  # all seq ≤ cutoff(100)
    ev = D.eval_arming(snaps, stale)
    ok(ev["cohort_n"] == 0 and not ev["eligible"], "pre-cutoff labels are NOT cohort")


def test_eval_cohort_counts_from_latest_ws_snapshot_only():
    # xreview r1 MED: fresh-evidence-only. Snapshot cutoffs 100/110/120/130 — labels landing
    # BETWEEN snapshots (seq 101..130) already fed later snapshots' classifications (stability
    # evidence) and must NOT be re-admitted as agreement evidence; only seq > 130 counts.
    snaps = [snap(t, cutoff=100 + 10 * i) for i, t in enumerate(weekly(4))]
    between = cohort(14, 0, start_seq=101)                             # seqs 101..114 ≤ 130
    ev = D.eval_arming(snaps, between)
    ok(ev["cohort_n"] == 0 and not ev["eligible"],
       "labels between snapshots are NOT cohort (no evidence reuse across sub-gates)")
    fresh = cohort(14, 0, start_seq=131)                               # all > latest cutoff 130
    ev = D.eval_arming(snaps, between + fresh)
    ok(ev["cohort_n"] == 14, "cohort = labels after the LATEST would-suppress cutoff only")
    ok(ev["eligible"] is True, "a fully-fresh 14-fp cohort still arms")


def test_eval_wontfix_and_invalid_not_cohort():
    snaps = [snap(t) for t in weekly(4)]
    rows = cohort(14, 0) + [wontfix(200), row(outcome="applied_fixed", source="auto_fix_landed", seq=201)]
    ev = D.eval_arming(snaps, rows)
    ok(ev["cohort_n"] == 14, "wontfix + off-set rows never enter the eval cohort")


def test_eval_no_would_suppress_snaps():
    snaps = [snap(t, would_say="hold") for t in weekly(4)]
    ev = D.eval_arming(snaps, cohort(14, 0))
    ok(not ev["eligible"] and ev["gates"]["stability"] is False,
       "hold-only snapshots → nothing to arm")
    ev = D.eval_arming([], cohort(14, 0))
    ok(not ev["eligible"], "no snapshots at all → not eligible")


def test_eval_uses_snapshot_params_not_defaults():
    # a snapshot captured with a STRICTER agree bar must gate by ITS OWN bar (M5-r2)
    snaps = [snap(t, eval_agree=0.95) for t in weekly(4)]
    ev = D.eval_arming(snaps, cohort(14, 0))                           # lo≈0.78 < 0.95
    ok(ev["gates"]["agreement"] is False, "eval params come FROM THE SNAPSHOT, not live config")


# ---- env knob parsing (fail-safe) ----------------------------------------------------------------
def test_params_from_env():
    p = D.ShadowParams.from_env({})
    ok(p == D.ShadowParams(), "empty env → all defaults")
    p = D.ShadowParams.from_env({"SHADOW_FLOOR": "0.25", "SHADOW_MIN_N": "5"})
    ok(p.floor == 0.25 and p.min_n == 5 and p.ceiling == 0.90, "valid overrides apply, rest default")
    p = D.ShadowParams.from_env({"SHADOW_FLOOR": "banana", "SHADOW_MIN_N": "-3",
                                 "SHADOW_CEILING": "7", "SHADOW_Z": "0"})
    ok(p == D.ShadowParams(),
       "malformed/out-of-range values fall back to defaults (a typo can't zero a threshold)")


def test_versions_deterministic():
    ok(D.taxonomy_version() == D.taxonomy_version() and len(D.taxonomy_version()) == 12,
       "taxonomy_version is a stable 12-hex stamp")
    ok(D.SUPPRESSIBLE == frozenset() and D.SUPPRESSIBLE_SET_VERSION == "v1-empty",
       "the SUPPRESSIBLE set ships EMPTY (fail-closed suppressibility)")


def test_format_eval_smoke():
    ok("nothing to evaluate" in D.format_eval([]), "no would-suppress snapshots → honest message")
    snaps = [snap(t) for t in weekly(4)]
    ev = D.eval_arming(snaps, cohort(14, 0))
    out = D.format_eval([("fail-open", "kilabz", ev)])
    ok("ARMING-ELIGIBLE" in out and "not built" in out,
       "an eligible verdict is loudly informational (acting is PR-B)")


if __name__ == "__main__":
    g = sorted(k for k in globals() if k.startswith("test_"))
    for name in g:
        globals()[name]()
    print(f"{PASS[0]} passed, {FAIL[0]} failed ({len(g)} test fns)")
    raise SystemExit(1 if FAIL[0] else 0)
