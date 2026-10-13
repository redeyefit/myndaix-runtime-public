"""dialshadowrecord.py — the `mxr dial-shadow` wiring (docs/shadow-dial-design.md v0.6, PR-A).

MEASURE ONLY. Three modes over the SAME read (finding_current_human via led.dial_shadow_labels):
  - `mxr dial-shadow`             print the per-(rule_tag × reviewer_family) shadow table (§3).
  - `mxr dial-shadow --snapshot`  the rung's ONLY write: append the current classification to
                                  dial_shadow_snapshot (its OWN table — never read by review/
                                  prompt/gate code; the only reader is --eval). Weekly or manual.
  - `mxr dial-shadow --eval`      the §4 agreement report: per past would-suppress cell, the five
                                  numeric arming sub-gates against labels that landed SINCE the
                                  prediction. Informational — acting (PR-B) is not built.

OPERATOR command tier, FAIL-CLOSED (exit 2) on an unreachable ledger or any read error — a dial
that can't read must not print an empty "nothing to suppress" (design §3). Rides the morning
brain-check next to `mxr outcome-stats`. Thresholds come from env HERE (v0.2 D1 — the ledger SQL
is a pure projection), parsed fail-safe in the pure core (runtime.dialshadow).

Gate mode: N/A — this is a manual operator read, never on the review/merge path (design §6).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import sys

from runtime import dialshadow as ds
from runtime.ledger.postgres_store import PostgresLedger


def log(msg: str) -> None:
    """Diagnostics ALWAYS to stderr (stdout is reserved for the table / report payload)."""
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [dialshadow] {msg}", file=sys.stderr, flush=True)


def _snapshot_row(cell: dict, cutoff: int, p: ds.ShadowParams) -> dict:
    """A fully-formed dial_shadow_snapshot row (M5: self-contained — the cell's numbers PLUS the
    complete policy context it was classified under, so any future eval reproduces from the row
    alone)."""
    return {
        "data_cutoff_seq": cutoff,
        "rule_tag": cell["rule_tag"], "reviewer_family": cell["reviewer_family"],
        "confirmed_real": cell["confirmed_real"], "dismissed_fp": cell["dismissed_fp"],
        "n": cell["n"], "precision": cell["precision"],
        "wilson_lo": cell["wilson_lo"], "wilson_hi": cell["wilson_hi"],
        "n_recent": cell["n_recent"],
        "wilson_recent_lo": cell["wilson_recent_lo"], "wilson_recent_hi": cell["wilson_recent_hi"],
        "distinct_refs": cell["distinct_refs"], "distinct_plays": cell["distinct_plays"],
        "would_say": cell["would_say"], "suppressible": cell["suppressible"],
        "floor": p.floor, "ceiling": p.ceiling, "min_n": p.min_n, "min_refs": p.min_refs,
        "min_plays": p.min_plays, "min_fp": p.min_fp, "recency_n": p.recency_n, "z": p.z,
        "stable_snaps": p.stable_snaps, "eval_min_n": p.eval_min_n, "eval_agree": p.eval_agree,
        "week_span_rule": f"distinct_iso_weeks>={p.stable_snaps}",
        "suppressible_set_version": ds.SUPPRESSIBLE_SET_VERSION,
        "taxonomy_version": ds.taxonomy_version(),
    }


async def _run(mode: str) -> int:
    dsn = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
    try:
        led = await PostgresLedger.connect(dsn)
    except Exception as e:
        log(f"ledger unreachable ({e}) — fail-closed")
        return 2
    try:
        params = ds.ShadowParams.from_env()

        if mode == "table":
            rows = await led.dial_shadow_labels()
            print(ds.format_table(ds.aggregate_cells(rows, params), params))
            return 0

        if mode == "snapshot":
            # cutoff FIRST, then a cutoff-BOUNDED labels read (xreview r1 HIGH): the snapshot's
            # cells are exactly the labels with seq <= data_cutoff_seq, so a label racing in
            # mid-snapshot is cleanly post-cutoff (a future eval cohort) instead of silently
            # lost from both sides of the boundary.
            cutoff = await led.max_finding_seq()
            cells = ds.aggregate_cells(await led.dial_shadow_labels(cutoff), params)
            if not cells:
                print("no human-labeled findings yet — nothing to snapshot")
                return 0
            n = await led.dial_shadow_snapshot_append(
                [_snapshot_row(c, cutoff, params) for c in cells])
            print(ds.format_table(cells, params))
            print(f"snapshot appended: {n} cell(s) at data_cutoff_seq={cutoff}")
            return 0

        # --eval: group snapshots by cell; report every cell with ≥1 would-suppress snapshot.
        rows = await led.dial_shadow_labels()
        snaps = await led.dial_shadow_snapshots()
        by_cell: dict = {}
        for s in snaps:
            by_cell.setdefault((s["rule_tag"], s["reviewer_family"]), []).append(s)
        results = []
        for (tag, family), cell_snaps in sorted(by_cell.items()):
            if not any(s["would_say"] == "would-suppress" for s in cell_snaps):
                continue
            cell_rows = [r for r in rows
                         if r["rule_tag"] == tag and r["reviewer_family"] == family]
            results.append((tag, family, ds.eval_arming(cell_snaps, cell_rows)))
        print(ds.format_eval(results))
        return 0
    except Exception as e:
        log(f"dial-shadow {mode} failed ({e}) — fail-closed")
        return 2
    finally:
        try:
            await led.close()
        except Exception:
            pass


def main(argv: list) -> int:
    """`mxr dial-shadow [--snapshot|--eval]` — flags are mutually exclusive; anything else is a
    usage error (exit 2, operator tier: misuse must be visible, never silently the default read)."""
    raw = argv[1:]
    if not raw:
        return asyncio.run(_run("table"))
    if raw == ["--snapshot"]:
        return asyncio.run(_run("snapshot"))
    if raw == ["--eval"]:
        return asyncio.run(_run("eval"))
    log("usage: dial-shadow [--snapshot | --eval]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
