"""Pure, DB-free core for the shadow dial (docs/shadow-dial-design.md v0.6) — MEASURE ONLY.

The Wilson-interval math, the three-way (source, outcome) pair handling, the per-(rule_tag ×
reviewer_family) classifier, and the §4 eval/arming gate. Kept separate from the ledger wiring
(runtime.dialshadowrecord) so every correctness-critical decision here is unit-testable without a
DB (mirrors runtime.outcomes / runtime.capture, the sibling rungs' pure layers).

THE BOUND-DIRECTION INVARIANT (design §2, stated once, applied EVERYWHERE a CI gates a decision):
    confident the true rate is BELOW a threshold T  ⟺  wilson HI < T
    confident the true rate is ABOVE a threshold T  ⟺  wilson LO ≥ T
Never the midpoint, never the wrong end. Applied at all three gates:
    would-suppress  = precision confidently BELOW the floor    → hi < FLOOR
    would-trust     = precision confidently ABOVE the ceiling  → lo ≥ CEILING
    eval agreement  = agreement confidently ABOVE the bar      → lo ≥ EVAL_AGREE

Pair handling is THREE-WAY (v0.6 MAJOR-r3 — legal-excluded ≠ invalid): a current human row is
classified by its EXACT (outcome_source, outcome) pair — included precision pair, legal excluded
pair (dismissed_wontfix — a valid human label that is just not a precision signal), or an
impossible off-set pair (the fence pair-CHECK makes it unreachable; a nonzero tally here is a
VISIBLE fence-integrity alarm, never a count).

This rung acts on NOTHING: no function here (or in the wiring) writes to a fence table, weights a
prompt, or gates a review. The classifier output is INFORMATIONAL, and even a would-suppress is
annotated non-suppressible while the code-owned SUPPRESSIBLE set is empty (fail-closed
suppressibility, the v0.2 B1 fold).
"""
from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, fields
from typing import Optional

from runtime.capture import RULE_TAG_TAXONOMY, is_allowed_tag  # single source of truth (S3)

__all__ = [
    "SUPPRESSIBLE", "SUPPRESSIBLE_SET_VERSION", "REVIEWER_FAMILIES",
    "PAIR_REAL", "PAIR_FP", "PAIR_EXCLUDED", "PAIR_INVALID",
    "ShadowParams", "taxonomy_version", "wilson", "classify_pair",
    "aggregate_cells", "eval_arming", "display_tag", "display_family",
    "format_table", "format_eval",
]

# ---- fail-closed suppressibility (v0.2 B1 fold, code-owned) -----------------------------------
# EMPTY in v1: the live taxonomy is entirely correctness/security classes and under fail-closed
# policy none is safe to auto-suppress — a noisy security detector is still a detector. A non-empty
# set requires a benign/"style-nit" taxonomy tier and ITS OWN design pass (design §5).
SUPPRESSIBLE: frozenset = frozenset()
SUPPRESSIBLE_SET_VERSION = "v1-empty"

# Mirrors the DB CHECK on finding_outcome.reviewer_family (migration 0008). Display validation
# only — an off-list family can't exist in the ledger, but the verb must not echo raw bytes.
REVIEWER_FAMILIES = frozenset({"kilabz", "oracle"})

# ---- three-way pair classification (design §2, MAJOR-r3) ---------------------------------------
PAIR_REAL = "real"          # (human_confirm, confirmed_real)            → numerator + n
PAIR_FP = "fp"              # (human_dismiss, dismissed_false_positive)  → fp term + n
PAIR_EXCLUDED = "excluded"  # (human_dismiss, dismissed_wontfix)         → legal, NOT a signal
PAIR_INVALID = "invalid"    # anything else — fence-integrity alarm, never a count

_INCLUDED_PAIRS = {
    ("human_confirm", "confirmed_real"): PAIR_REAL,
    ("human_dismiss", "dismissed_false_positive"): PAIR_FP,
}
_EXCLUDED_PAIRS = {("human_dismiss", "dismissed_wontfix"): PAIR_EXCLUDED}


def classify_pair(outcome_source: str, outcome: str) -> str:
    """EXACT-pair three-way classification. A builder must NOT widen the admitted set without a
    design change (design §2) — any unrecognized pair is PAIR_INVALID, surfaced, never counted."""
    pair = (outcome_source, outcome)
    if pair in _INCLUDED_PAIRS:
        return _INCLUDED_PAIRS[pair]
    if pair in _EXCLUDED_PAIRS:
        return PAIR_EXCLUDED
    return PAIR_INVALID


def taxonomy_version() -> str:
    """Deterministic short version of the allowlisted taxonomy, stamped into every snapshot row
    (M5 reproducibility): a taxonomy edit changes the version, so an eval can tell it is comparing
    across taxonomy generations."""
    joined = "\n".join(sorted(RULE_TAG_TAXONOMY))
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


# ---- policy knobs (env-tunable in the VERB, defaults here — v0.2 D1: SQL stays a pure
# projection; a recalibration is a config change, never code) ------------------------------------
@dataclass(frozen=True)
class ShadowParams:
    floor: float = 0.30        # SHADOW_FLOOR    — would-suppress: hi < floor
    ceiling: float = 0.90      # SHADOW_CEILING  — would-trust:    lo ≥ ceiling
    min_n: int = 10            # SHADOW_MIN_N    — labeled denominator below this → insufficient
    min_refs: int = 2          # SHADOW_MIN_REFS — provenance: distinct source refs
    min_plays: int = 2         # SHADOW_MIN_PLAYS— provenance: distinct source plays
    min_fp: int = 3            # SHADOW_MIN_FP   — would-suppress needs ≥ this many human fp's
    recency_n: int = 30        # SHADOW_RECENCY_N— recent window = last N labeled events by seq
    z: float = 1.96            # 95% Wilson
    stable_snaps: int = 4      # SHADOW_STABLE_SNAPS — §4.1: snapshots AND distinct ISO weeks
    eval_min_n: int = 10       # SHADOW_EVAL_MIN_N   — §4.3: post-cutoff cohort size
    eval_agree: float = 0.70   # SHADOW_EVAL_AGREE   — §4.4: agreement Wilson LO ≥ this

    _ENV = {
        "floor": "SHADOW_FLOOR", "ceiling": "SHADOW_CEILING", "min_n": "SHADOW_MIN_N",
        "min_refs": "SHADOW_MIN_REFS", "min_plays": "SHADOW_MIN_PLAYS", "min_fp": "SHADOW_MIN_FP",
        "recency_n": "SHADOW_RECENCY_N", "z": "SHADOW_Z",
        "stable_snaps": "SHADOW_STABLE_SNAPS", "eval_min_n": "SHADOW_EVAL_MIN_N",
        "eval_agree": "SHADOW_EVAL_AGREE",
    }

    @classmethod
    def from_env(cls, env=None) -> "ShadowParams":
        """Each knob parsed FAIL-SAFE: a malformed or out-of-range value falls back to the default
        (design §6, the CLI env-knob convention) — a typo'd env var must never turn a threshold
        into 0 and flip a classification."""
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            raw = env.get(cls._ENV[f.name], "")
            if not raw:
                continue
            try:
                val = int(raw) if f.default.__class__ is int else float(raw)
            except (TypeError, ValueError):
                continue
            # sanity ranges: rates in [0,1], counts ≥ 1, z > 0 — anything else is a typo, keep default
            if f.default.__class__ is int:
                if val < 1:
                    continue
            elif f.name == "z":
                if not (0 < val < 10):
                    continue
            elif not (0.0 <= val <= 1.0):
                continue
            kw[f.name] = val
        return cls(**kw)


# ---- Wilson score interval ----------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.96) -> Optional[tuple]:
    """95% (z=1.96) Wilson score interval [lo, hi] for k successes in n trials.
    n = 0 → None (no divide, no fake certainty). Clamped to [0, 1]."""
    if n <= 0:
        return None
    if not (0 <= k <= n):
        raise ValueError(f"wilson: k={k} outside [0, n={n}]")
    z2 = z * z
    denom = n + z2
    center = (k + z2 / 2.0) / denom
    half = (z / denom) * math.sqrt(k * (n - k) / n + z2 / 4.0)
    return (max(0.0, center - half), min(1.0, center + half))


# ---- per-(rule_tag × reviewer_family) aggregation + classification ------------------------------
def aggregate_cells(rows: list, params: ShadowParams) -> list:
    """rows = one CURRENT human row per (finding, family) (the ledger's finding_current_human read),
    each: {rule_tag, reviewer_family, outcome, outcome_source, seq, ref, play}. `ref`/`play` come
    from the finding's latest RAISE row (provenance = the labels span independent reviews, not one
    bulk dismissal of one decoy PR — design §2); a missing ref/play counts as NO provenance
    (fail-safe: fewer distinct → insufficient, never more).

    Returns one classified cell dict per (rule_tag, reviewer_family), sorted by (tag, family).
    dismissed_wontfix rows leave n + provenance + invalid ALL unchanged (legal-excluded);
    an off-set pair increments ONLY the cell's `invalid` tally (fence-integrity alarm)."""
    by_cell: dict = {}
    for r in rows:
        by_cell.setdefault((r["rule_tag"], r["reviewer_family"]), []).append(r)

    cells = []
    for (tag, family), items in sorted(by_cell.items()):
        included = []   # (seq, pair_class, ref, play) — the admitted precision pairs only
        invalid = 0
        for r in items:
            pc = classify_pair(r["outcome_source"], r["outcome"])
            if pc in (PAIR_REAL, PAIR_FP):
                included.append((r["seq"], pc, r.get("ref"), r.get("play")))
            elif pc == PAIR_INVALID:
                invalid += 1
            # PAIR_EXCLUDED (dismissed_wontfix): deliberately touches nothing.

        included.sort(key=lambda t: t[0])                      # by seq, oldest first
        k = sum(1 for _, pc, _, _ in included if pc == PAIR_REAL)
        n = len(included)
        fp = n - k
        w = wilson(k, n, params.z)
        recent = included[-params.recency_n:]                  # last N labeled events by seq (M3)
        k_r = sum(1 for _, pc, _, _ in recent if pc == PAIR_REAL)
        n_r = len(recent)
        w_r = wilson(k_r, n_r, params.z)
        refs = {ref for _, _, ref, _ in included if ref}
        plays = {p for _, _, _, p in included if p}

        cell = {
            "rule_tag": tag, "reviewer_family": family,
            "confirmed_real": k, "dismissed_fp": fp, "n": n,
            "precision": (k / n) if n else None,
            "wilson_lo": w[0] if w else None, "wilson_hi": w[1] if w else None,
            "n_recent": n_r,
            "precision_recent": (k_r / n_r) if n_r else None,
            "wilson_recent_lo": w_r[0] if w_r else None,
            "wilson_recent_hi": w_r[1] if w_r else None,
            "distinct_refs": len(refs), "distinct_plays": len(plays),
            "invalid": invalid,
        }
        cell["would_say"], cell["reason"] = _classify(cell, params)
        # fail-closed suppressibility: even a would-suppress is "not suppressible" while the
        # code-owned set is empty — the measurement may say "low precision", the design refuses
        # to imply any of it is auto-suppressible (v0.2 B1).
        cell["suppressible"] = cell["would_say"] == "would-suppress" and tag in SUPPRESSIBLE
        cells.append(cell)
    return cells


def _classify(cell: dict, p: ShadowParams) -> tuple:
    """The §2 classifier. insufficient is checked FIRST (a thin cell must refuse to classify —
    the honest v1 output at today's volumes), then the two CI gates per the invariant."""
    reasons = []
    if cell["n"] < p.min_n:
        reasons.append(f"n<{p.min_n}")
    if cell["distinct_refs"] < p.min_refs:
        reasons.append(f"refs<{p.min_refs}")
    if cell["distinct_plays"] < p.min_plays:
        reasons.append(f"plays<{p.min_plays}")
    if reasons:
        return "insufficient", ",".join(reasons)
    # invariant: confidently BELOW the floor ⟺ hi < FLOOR (plus the min-FP mass guard)
    if cell["wilson_hi"] is not None and cell["wilson_hi"] < p.floor and cell["dismissed_fp"] >= p.min_fp:
        return "would-suppress", f"hi={cell['wilson_hi']:.2f}<{p.floor:.2f},fp={cell['dismissed_fp']}"
    # invariant: confidently ABOVE the ceiling ⟺ lo ≥ CEILING
    if cell["wilson_lo"] is not None and cell["wilson_lo"] >= p.ceiling:
        return "would-trust", f"lo={cell['wilson_lo']:.2f}>={p.ceiling:.2f}"
    return "hold", "-"


# ---- the §4 eval / arming gate --------------------------------------------------------------------
def _iso_week(dt) -> tuple:
    c = dt.isocalendar()
    return (c[0], c[1])


def eval_arming(snaps: list, all_cell_rows: list) -> dict:
    """The §4 arming gate for ONE (tag × family) cell — every sub-gate NUMERIC, none skippable.

    snaps = this cell's dial_shadow_snapshot rows (dicts), each carrying its OWN policy context
    (M5/M5-r2: an eval verdict is reproducible from the snapshots alone, not live config — so the
    gate reads thresholds from the LATEST snapshot, never from env).
    all_cell_rows = ALL current human rows for the cell (aggregate_cells input shape); the cohort
    is the included-pair rows with seq > the LATEST would-suppress snapshot's data_cutoff_seq —
    FRESH evidence only (xreview r1 MED): a label that landed between snapshots already fed the
    later snapshots' classifications (the stability signal), so re-admitting it as agreement
    evidence would count the same label at two sub-gates. Anchoring at the latest cutoff is the
    strict fail-closed reading of §4.3 "post-cutoff" — arming needs eval_min_n labels that arrived
    after the MOST RECENT prediction, evidence no snapshot has seen.

    Returns {"eligible": bool, "gates": {...}, ...} — informational; NOTHING reads this to act."""
    snaps = sorted(snaps, key=lambda s: s["captured_at"])
    ws_snaps = [s for s in snaps if s["would_say"] == "would-suppress"]
    latest = snaps[-1] if snaps else None
    out = {
        "would_suppress_snaps": len(ws_snaps),
        "gates": {}, "eligible": False,
        "cohort_n": 0, "cohort_fp": 0, "agree_rate": None, "agree_lo": None,
    }
    if not ws_snaps or latest is None:
        out["gates"]["stability"] = False
        return out

    # gate params from the LATEST snapshot (reproducibility, M5-r2)
    stable_snaps = int(latest["stable_snaps"])
    eval_min_n = int(latest["eval_min_n"])
    eval_agree = float(latest["eval_agree"])
    min_refs = int(latest["min_refs"])
    min_plays = int(latest["min_plays"])
    min_fp = int(latest["min_fp"])
    floor = float(latest["floor"])
    z = float(latest["z"])

    # §4.1 stability: ≥ stable_snaps would-suppress snapshots spanning ≥ stable_snaps distinct
    # ISO weeks (4 same-day snaps must FAIL).
    weeks = {_iso_week(s["captured_at"]) for s in ws_snaps}
    g_stable = len(ws_snaps) >= stable_snaps and len(weeks) >= stable_snaps
    out["gates"]["stability"] = g_stable

    # §4.2 both windows in the LATEST snapshot: all-time hi < floor AND recent hi < floor
    # (invariant: confidently BELOW). A missing recent interval (n_recent=0) FAILS — absence of
    # evidence is never a pass.
    hi = latest.get("wilson_hi")
    hi_r = latest.get("wilson_recent_hi")
    g_windows = hi is not None and hi_r is not None and float(hi) < floor and float(hi_r) < floor
    out["gates"]["both_windows"] = g_windows

    # §4.3+§4.5 cohort: included-pair labels landed AFTER the latest would-suppress prediction —
    # fresh, unseen-by-any-snapshot evidence only (max, never min: no evidence reuse across the
    # stability and agreement sub-gates).
    cutoff = max(int(s["data_cutoff_seq"]) for s in ws_snaps)
    cohort = []
    for r in all_cell_rows:
        if int(r["seq"]) <= cutoff:
            continue
        pc = classify_pair(r["outcome_source"], r["outcome"])
        if pc in (PAIR_REAL, PAIR_FP):
            cohort.append((pc, r.get("ref"), r.get("play")))
    n = len(cohort)
    fp = sum(1 for pc, _, _ in cohort if pc == PAIR_FP)
    refs = {ref for _, ref, _ in cohort if ref}
    plays = {p for _, _, p in cohort if p}
    out["cohort_n"], out["cohort_fp"] = n, fp
    g_cohort = (n >= eval_min_n and len(refs) >= min_refs and len(plays) >= min_plays
                and fp >= min_fp)
    out["gates"]["cohort"] = g_cohort

    # §4.4 agreement: fraction of subsequent labels that are fp (confirming the would-suppress),
    # armed only when confidently HIGH → Wilson LOWER bound ≥ eval_agree (a bare 7/10, lo≈0.40,
    # does NOT pass). §4.5: zero subsequent labels → insufficient, never a pass.
    w = wilson(fp, n, z) if n else None
    if w:
        out["agree_rate"] = fp / n
        out["agree_lo"] = w[0]
    g_agree = w is not None and w[0] >= eval_agree
    out["gates"]["agreement"] = g_agree
    out["gates"]["nonempty_cohort"] = n > 0

    out["eligible"] = g_stable and g_windows and g_cohort and g_agree and n > 0
    return out


# ---- display (validated — design §6: tags/families only ever reach stdout, but never raw) --------
def display_tag(tag: str) -> str:
    """An off-taxonomy tag is displayed as a deterministic placeholder, never raw bytes (a tag can
    only reach the ledger through the allowlist, so this fires on taxonomy drift — visible, safe)."""
    if is_allowed_tag(tag):
        return tag
    return f"<off-taxonomy:{hashlib.sha256(str(tag).encode()).hexdigest()[:8]}>"


def display_family(family: str) -> str:
    if family in REVIEWER_FAMILIES:
        return family
    return f"<off-family:{hashlib.sha256(str(family).encode()).hexdigest()[:8]}>"


def _fmt_rate(v) -> str:
    return "  n/a" if v is None else f"{v:.2f}"


def format_table(cells: list, params: ShadowParams) -> str:
    """The `mxr dial-shadow` read surface (design §3). Expected today: `insufficient` for
    essentially every cell — the honest v1 output at current label volumes."""
    if not cells:
        return "no human-labeled findings yet — the shadow dial has nothing to measure"
    head = (f"{'rule_tag':<26} {'family':<8} {'n':>3} {'prec':>5} {'wilson[lo,hi]':>14} "
            f"{'recent':>6} {'refs':>4} {'plays':>5} {'would-say':<14} suppressible")
    lines = [head]
    total_invalid = 0
    for c in cells:
        total_invalid += c["invalid"]
        if c["wilson_lo"] is None:
            interval = "           n/a"
        else:
            interval = f"[{c['wilson_lo']:.2f},{c['wilson_hi']:.2f}]".rjust(14)
        if c["would_say"] == "insufficient":
            sup = f"no ({c['reason']})"
        elif c["would_say"] == "would-suppress":
            sup = "YES" if c["suppressible"] else "no (no benign tier)"
        else:
            sup = "-"
        lines.append(
            f"{display_tag(c['rule_tag']):<26} {display_family(c['reviewer_family']):<8} "
            f"{c['n']:>3} {_fmt_rate(c['precision']):>5} {interval} "
            f"{_fmt_rate(c['precision_recent']):>6} {c['distinct_refs']:>4} "
            f"{c['distinct_plays']:>5} {c['would_say']:<14} {sup}")
    if total_invalid:
        lines.append(f"⚠ {total_invalid} INVALID (off-fence) label row(s) — fence-integrity alarm; "
                     f"investigate finding_outcome pairs")
    return "\n".join(lines)


def format_eval(results: list) -> str:
    """The `--eval` agreement report (design §4): per cell with any would-suppress snapshot, the
    five numeric sub-gates and the arming verdict. Informational — arming itself is PR-B, unbuilt."""
    if not results:
        return ("no would-suppress snapshots yet — nothing to evaluate "
                "(run `mxr dial-shadow --snapshot` weekly; the gate needs 4 snapshots over 4 weeks)")
    lines = []
    for tag, family, ev in results:
        g = ev["gates"]
        verdict = "ARMING-ELIGIBLE (informational — acting is PR-B, not built)" if ev["eligible"] \
            else "not eligible"
        agree = ("n/a" if ev["agree_lo"] is None
                 else f"{ev['agree_rate']:.2f} (wilson lo {ev['agree_lo']:.2f})")
        lines.append(f"{display_tag(tag)} × {display_family(family)}: {verdict}")
        lines.append(f"  stability   : {'PASS' if g.get('stability') else 'fail'} "
                     f"({ev['would_suppress_snaps']} would-suppress snapshot(s))")
        lines.append(f"  both-windows: {'PASS' if g.get('both_windows') else 'fail'}")
        lines.append(f"  cohort      : {'PASS' if g.get('cohort') else 'fail'} "
                     f"(n={ev['cohort_n']}, fp={ev['cohort_fp']})")
        lines.append(f"  agreement   : {'PASS' if g.get('agreement') else 'fail'} ({agree})")
    return "\n".join(lines)
