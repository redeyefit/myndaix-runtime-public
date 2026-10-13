"""shadow-dial ledger wiring (design v0.6 PR-A) against a real Postgres — migration 0013, the
dial_shadow_labels read (provenance join, latest-human-word semantics, machine rows structurally
absent), the snapshot append (ALL M5 columns populated + reproducible), and the `mxr dial-shadow`
verb end-to-end (fail-closed exit 2, honest empty output, --snapshot, --eval). The pure math/
classification core is in test_dialshadow.py.

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_dialshadow_verbs.py
"""
import asyncio
import contextlib
import datetime as dt
import inspect
import io
import os

from runtime import dialshadow as ds
from runtime import dialshadowrecord as dsr
from runtime import outcomes
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


async def _truncate(led):
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE finding_outcome RESTART IDENTITY")
        await con.execute("TRUNCATE dial_shadow_snapshot RESTART IDENTITY")


def _finding(tag, path, content, family):
    return {"tag": tag, "path": path, "line_hash": outcomes.line_hash(content),
            "reviewer_family": family}


def _key(tag, path, content):
    return outcomes.finding_key("repoA", tag, path, outcomes.line_hash(content))


async def _seed_labeled(led, n_fp, n_real=0, tag="fail-open", family="kilabz"):
    """Raise n findings across 2 refs × 2 plays, then human-label them (fp first, then real).
    Returns the full finding keys in seed order."""
    keys = []
    for i in range(n_fp + n_real):
        ref, play = f"ref{i % 2}", f"play{i % 2}"
        path, content = f"src/f{i}.py", f"line {i}"
        await led.record_findings("repoA", ref, f"t{i}", play, [path],
                                  [_finding(tag, path, content, family)])
        k = _key(tag, path, content)
        kind = "fp" if i < n_fp else "real"
        r = await led.confirm_outcome(k[:12], family, kind, principal_role="human")
        assert "error" not in r, r
        keys.append(k)
    return keys


# ---- migration 0013 ------------------------------------------------------------------------------
async def test_migration_table_and_columns(led):
    async with led._pool.acquire() as con:
        cols = await con.fetch(
            """SELECT column_name FROM information_schema.columns
                WHERE table_name = 'dial_shadow_snapshot'""")
    names = {c["column_name"] for c in cols}
    expected = {"id", "captured_at", "data_cutoff_seq", "rule_tag", "reviewer_family",
                "confirmed_real", "dismissed_fp", "n", "precision", "wilson_lo", "wilson_hi",
                "n_recent", "wilson_recent_lo", "wilson_recent_hi", "distinct_refs",
                "distinct_plays", "would_say", "suppressible", "floor", "ceiling", "min_n",
                "min_refs", "min_plays", "min_fp", "recency_n", "z", "stable_snaps",
                "eval_min_n", "eval_agree", "week_span_rule", "suppressible_set_version",
                "taxonomy_version"}
    ok(expected <= names, f"all M5 schema columns exist (missing: {expected - names})")
    # idempotency: migrate() re-ran on this boot against an existing table (IF NOT EXISTS held)
    applied = await led.migrate()
    ok("0013_dial_shadow_snapshot.sql" in applied, "0013 re-applies idempotently")


# ---- dial_shadow_labels --------------------------------------------------------------------------
async def test_labels_read_shape_and_provenance(led):
    await _truncate(led)
    await _seed_labeled(led, n_fp=2, n_real=1)
    rows = await led.dial_shadow_labels()
    ok(len(rows) == 3, f"one CURRENT human row per finding, got {len(rows)}")
    ok(all(r["ref"] in ("ref0", "ref1") and r["play"] in ("play0", "play1") for r in rows),
       "ref + play come from the finding's RAISE row (review:<play> parsed)")
    pairs = {(r["outcome_source"], r["outcome"]) for r in rows}
    ok(pairs == {("human_dismiss", "dismissed_false_positive"), ("human_confirm", "confirmed_real")},
       f"only human pairs in the read, got {pairs}")


async def test_labels_latest_human_word_wins(led):
    await _truncate(led)
    keys = await _seed_labeled(led, n_fp=1)
    r = await led.confirm_outcome(keys[0][:12], "kilabz", "real", principal_role="human")   # correction
    assert "error" not in r, r
    rows = await led.dial_shadow_labels()
    ok(len(rows) == 1 and rows[0]["outcome"] == "confirmed_real",
       "an fp -> real correction supersedes (DISTINCT ON latest seq)")


async def test_labels_machine_rows_structurally_absent(led):
    await _truncate(led)
    f = _finding("fail-open", "src/m.py", "machine line", "kilabz")
    await led.record_findings("repoA", "refM", "tM", "playM", ["src/m.py"], [f])
    rows = await led.dial_shadow_labels()
    ok(rows == [], "an open (machine-only) finding never enters the labels read")
    # auto-close it (line disappears) — still machine-only, still absent
    await led.record_findings("repoA", "refM", "tM2", "playM", ["src/m.py"], [], {"src/m.py": set()})
    rows = await led.dial_shadow_labels()
    ok(rows == [], "applied_fixed (machine) rows never enter the labels read")


async def test_max_finding_seq(led):
    await _truncate(led)
    ok(await led.max_finding_seq() == 0, "empty ledger -> cutoff 0")
    await _seed_labeled(led, n_fp=1)
    ok(await led.max_finding_seq() >= 2, "cutoff = max seq after raise + label")


async def test_labels_read_cutoff_bounded(led):
    """xreview r1 HIGH: the --snapshot path reads cutoff FIRST then labels bounded to seq<=cutoff,
    so a label racing in mid-snapshot is cleanly post-cutoff instead of silently lost from both
    the cells and the future eval cohort."""
    await _truncate(led)
    await _seed_labeled(led, n_fp=2)
    cutoff = await led.max_finding_seq()
    await _seed_labeled(led, n_fp=0, n_real=1, tag="missing-scoping")   # the "racing" label
    bounded = await led.dial_shadow_labels(cutoff)
    ok(len(bounded) == 2 and all(r["seq"] <= cutoff for r in bounded),
       "bounded read = exactly the labels at seq<=cutoff (the race label excluded)")
    ok(all(r["rule_tag"] == "fail-open" for r in bounded), "the post-cutoff cell is absent")
    unbounded = await led.dial_shadow_labels()
    ok(len(unbounded) == 3, "unbounded read (table/eval) still sees the racing label")
    racer = next(r for r in unbounded if r["rule_tag"] == "missing-scoping")
    ok(racer["seq"] > cutoff, "the race label is post-cutoff -> a FUTURE eval cohort, never lost")


# ---- snapshot append (M5) ------------------------------------------------------------------------
async def test_snapshot_append_all_columns_and_reproducible(led):
    await _truncate(led)
    await _seed_labeled(led, n_fp=12)                       # 0/12 -> hi≈0.24 -> would-suppress
    params = ds.ShadowParams()
    rows = await led.dial_shadow_labels()
    cells = ds.aggregate_cells(rows, params)
    cutoff = await led.max_finding_seq()
    n = await led.dial_shadow_snapshot_append([dsr._snapshot_row(c, cutoff, params) for c in cells])
    ok(n == 1, "one cell appended")
    snaps = await led.dial_shadow_snapshots()
    ok(len(snaps) == 1, "snapshot read back")
    s = snaps[0]
    ok(all(s[c] is not None for c in s if c not in
           ("precision", "wilson_lo", "wilson_hi", "wilson_recent_lo", "wilson_recent_hi")),
       "every non-nullable M5 column populated")
    ok(s["would_say"] == "would-suppress" and s["suppressible"] is False,
       "0/12 snapshot records would-suppress, non-suppressible")
    ok(int(s["data_cutoff_seq"]) == cutoff, "data_cutoff_seq = max seq at capture")
    ok(s["week_span_rule"] == "distinct_iso_weeks>=4" and s["taxonomy_version"] == ds.taxonomy_version()
       and s["suppressible_set_version"] == "v1-empty", "policy/version context stamped")
    # M5 reproducibility: the stored row's own numbers + params yield its would_say
    w = ds.wilson(int(s["confirmed_real"]), int(s["n"]), float(s["z"]))
    ok(abs(w[1] - float(s["wilson_hi"])) < 1e-6, "stored wilson_hi recomputes from stored (k, n, z)")
    ok(w[1] < float(s["floor"]) and int(s["dismissed_fp"]) >= int(s["min_fp"])
       and int(s["n"]) >= int(s["min_n"]),
       "stored would_say reproduces from the stored columns alone (hi<floor, fp>=min_fp, n>=min_n)")
    # append-only: a second snapshot ADDS rows
    await led.dial_shadow_snapshot_append([dsr._snapshot_row(c, cutoff, params) for c in cells])
    ok(len(await led.dial_shadow_snapshots()) == 2, "append-only (second snapshot adds, never mutates)")


# ---- the verb end-to-end (sync — each main() owns its own event loop) ----------------------------
def _run_verb(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = dsr.main(["dial-shadow", *args])
    return rc, buf.getvalue()


def test_verb_end_to_end():
    os.environ["MYNDAIX_DSN"] = DSN

    rc, out = _run_verb([])
    ok(rc == 0 and "would-suppress" in out, f"table mode exits 0 + shows the seeded cell (rc={rc})")

    rc, out = _run_verb(["--snapshot"])
    ok(rc == 0 and "snapshot appended: 1 cell(s)" in out, f"--snapshot appends + reports (rc={rc})")

    rc, out = _run_verb(["--eval"])
    ok(rc == 0, f"--eval exits 0 (rc={rc})")
    ok("fail-open × kilabz" in out and "not eligible" in out,
       "eval reports the would-suppress cell; 3 snapshots (one ISO week) is NOT eligible")
    ok("stability   : fail" in out, "stability sub-gate fails on same-week snapshots")

    rc, out = _run_verb(["--bogus"])
    ok(rc == 2, "unknown flag -> usage error exit 2")

    os.environ["MYNDAIX_DSN"] = "postgresql://localhost:1/does_not_exist"
    rc, out = _run_verb([])
    ok(rc == 2, "unreachable ledger -> FAIL-CLOSED exit 2 (never an empty 'nothing to suppress')")
    os.environ["MYNDAIX_DSN"] = DSN


def test_verb_eval_eligible_path():
    """Seed 4 weekly back-dated would-suppress snapshots with cutoff 0 — every labeled fp lands
    after the prediction — and assert the full ARMING-ELIGIBLE path renders (informational)."""
    async def _seed():
        led = await PostgresLedger.connect(DSN)
        try:
            async with led._pool.acquire() as con:
                await con.execute("TRUNCATE dial_shadow_snapshot RESTART IDENTITY")
                base = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.timezone.utc)
                for i in range(4):
                    await con.execute(
                        """INSERT INTO dial_shadow_snapshot
                               (captured_at, data_cutoff_seq, rule_tag, reviewer_family,
                                confirmed_real, dismissed_fp, n, precision, wilson_lo, wilson_hi,
                                n_recent, wilson_recent_lo, wilson_recent_hi, distinct_refs,
                                distinct_plays, would_say, suppressible, floor, ceiling, min_n,
                                min_refs, min_plays, min_fp, recency_n, z, stable_snaps,
                                eval_min_n, eval_agree, week_span_rule, suppressible_set_version,
                                taxonomy_version)
                           VALUES ($1, 0, 'fail-open', 'kilabz', 0, 12, 12, 0.0, 0.0, 0.24,
                                   12, 0.0, 0.24, 2, 2, 'would-suppress', false, 0.30, 0.90, 10,
                                   2, 2, 3, 30, 1.96, 4, 10, 0.70, 'distinct_iso_weeks>=4',
                                   'v1-empty', 'test')""",
                        base + dt.timedelta(weeks=i))
        finally:
            await led.close()
    asyncio.run(_seed())
    rc, out = _run_verb(["--eval"])
    ok(rc == 0 and "ARMING-ELIGIBLE" in out and "acting is PR-B, not built" in out,
       "4 weekly would-suppress snapshots + a 12-fp post-cutoff cohort -> eligible (informational)")
    ok("agreement   : PASS" in out, "12/12 fp cohort clears the agreement Wilson lower bound")


async def main():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    await led.migrate()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    try:
        for t in tests:
            await t(led)
            print("PASS", t.__name__)
    finally:
        await led.close()


if __name__ == "__main__":
    asyncio.run(main())
    # verb-level tests run OUTSIDE the async block: each main() call owns its own event loop.
    for t in (test_verb_end_to_end, test_verb_eval_eligible_path):
        t()
        print("PASS", t.__name__)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)
