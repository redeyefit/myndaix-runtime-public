"""outcomes-ledger VERB wiring (PR-B: runtime.outcomerecord) — the mxr entry points that wire PR-A's
pure core + ledger verbs into the review pipeline. Exercises the parse+resolve+record happy path
against a REAL temp git repo (so the hunk computation + git-object reads run for real), the
human_dismiss routing (fail-closed short prefix), outcome-stats, --list-tags, the gate-mode HARD
no-op, and the fail-open contract (a bad/malformed call NEVER raises, records nothing). The pure
identity/parser core is in test_outcomes.py; the DB state machine is in test_outcomes_verbs.py.

Each entry point runs its own asyncio.run internally (like the real mxr call), so this harness stays
SYNCHRONOUS and never nests event loops — DB assertions use a tiny asyncio.run-wrapped query helper.

Run: LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_outcomerecord.py
"""
import asyncio
import io
import os
import subprocess
import tempfile
from contextlib import redirect_stdout, redirect_stderr

# outcomerecord reads MYNDAIX_DSN (not LEDGER_TEST_DSN) — point it at the test DB before import.
os.environ["MYNDAIX_DSN"] = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")

import asyncpg  # noqa: E402
from runtime import outcomerecord, outcomes  # noqa: E402
from runtime.ledger.postgres_store import PostgresLedger  # noqa: E402

DSN = os.environ["MYNDAIX_DSN"]
PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _rev(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _make_repo(tmp):
    """A throwaway git repo: commit a base file, then change line 2 -> the change is one hunk that
    contains line 2, so a finding on line 2 resolves. Returns (repo, base_sha, tip_sha)."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "f.py"), "w") as fh:
        fh.write("a\nb\nc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _rev(repo)
    with open(os.path.join(repo, "f.py"), "w") as fh:
        fh.write("a\nreturn None\nc\nd\n")   # line 2 changed -> inside the hunk
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo, base, _rev(repo)


def _q(sql, *args, one=False, val=False):
    """Run a query against the test DB (its own event loop — no nesting with the verbs)."""
    async def go():
        con = await asyncpg.connect(DSN)
        try:
            if val:
                return await con.fetchval(sql, *args)
            if one:
                return await con.fetchrow(sql, *args)
            return await con.fetch(sql, *args)
        finally:
            await con.close()
    return asyncio.run(go())


def _truncate():
    _q("TRUNCATE finding_outcome RESTART IDENTITY")


def _run(fn, argv):
    """Run a verb entry point capturing (stdout, stderr, rc). The verb runs its own asyncio.run."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(argv)
    return out.getvalue(), err.getvalue(), rc


# ---- --list-tags: prompt source-of-truth --------------------------------------------------
def test_list_tags_prints_taxonomy():
    out, _, rc = _run(outcomerecord.main, ["outcome-record", "--list-tags"])
    lines = [ln for ln in out.strip().split("\n") if ln]
    ok(rc == 0, "--list-tags exits 0")
    ok(set(lines) == set(outcomes.RULE_TAG_TAXONOMY), "--list-tags prints the WHOLE taxonomy")
    ok(lines == sorted(lines), "--list-tags output is sorted (stable prompt)")


# ---- happy path: parse + resolve (against a real git repo) + record + surface keys ----------
def test_record_happy_path_opens_both_families():
    _truncate()
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, tip = _make_repo(tmp)
        out, _, rc = _run(outcomerecord.main, [
            "outcome-record",
            "--kilabz", "some bug\nfinding:fail-open @ f.py:2",
            "--oracle", "also bad\nfinding:fail-open @ f.py:2",
            "--", repo, base, tip, "main", "play1", "f.py"])
        ok(rc == 0, "record exits 0")
        rows = [ln.split("\t") for ln in out.strip().split("\n") if ln]
        ok(len(rows) == 2, "two recorded keys surfaced (one per family)")
        ok({r[1] for r in rows} == {"kilabz", "oracle"}, "both families' keys surfaced")
        ok(all(len(r[0]) == 12 for r in rows), "each surfaced key is a 12-hex short key")
        ok(all(r[2] == "fail-open" and r[3] == "f.py" for r in rows), "key line carries tag + path")
        n = _q("SELECT count(*) FROM finding_current WHERE outcome='open'", val=True)
        ok(n == 2, "two open rows in finding_current")


def test_line_outside_hunk_is_dropped():
    # a finding on a line NOT inside any changed hunk (nor its ±3-line context) resolves to None ->
    # dropped, records nothing. Build a 40-line file, change ONE line near the end -> the hunk (with
    # git's default 3 lines of context) is nowhere near line 1, which stays in the file but off-hunk.
    _truncate()
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        lines = [f"line{i}\n" for i in range(1, 41)]
        with open(os.path.join(repo, "f.py"), "w") as fh:
            fh.writelines(lines)
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
        base = _rev(repo)
        lines[35] = "changed near end\n"          # only line 36 changes -> hunk ~lines 33-39
        with open(os.path.join(repo, "f.py"), "w") as fh:
            fh.writelines(lines)
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "change")
        tip = _rev(repo)
        # line 1 ('line1') is real in the file but far outside the changed hunk -> must drop.
        out, _, rc = _run(outcomerecord.main, [
            "outcome-record", "--kilabz", "finding:fail-open @ f.py:1", "--oracle", "",
            "--", repo, base, tip, "main", "play1", "f.py"])
        ok(rc == 0 and out.strip() == "", "an out-of-hunk finding records nothing (no keys surfaced)")
        n = _q("SELECT count(*) FROM finding_outcome", val=True)
        ok(n == 0, "no rows written for an out-of-hunk finding")


def test_close_phase_on_pass_review():
    # CLOSE runs on a PASS (empty reviews) too: an open finding whose line is GONE at a later tip
    # closes. Open on tip1, then a follow-up where line 2 is deleted -> applied_fixed.
    _truncate()
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, tip = _make_repo(tmp)
        _run(outcomerecord.main, [
            "outcome-record", "--kilabz", "finding:fail-open @ f.py:2", "--oracle", "",
            "--", repo, base, tip, "main", "play1", "f.py"])
        with open(os.path.join(repo, "f.py"), "w") as fh:
            fh.write("a\nc\nd\ne\n")            # 'return None' gone
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "fix")
        tip2 = _rev(repo)
        # a PASS follow-up: empty reviews, but the diff touched f.py and the line is gone -> close.
        _run(outcomerecord.main, [
            "outcome-record", "--kilabz", "", "--oracle", "",
            "--", repo, tip, tip2, "main", "play2", "f.py"])
        row = _q("SELECT outcome FROM finding_current WHERE reviewer_family='kilabz'", one=True)
        ok(row is not None and row["outcome"] == "applied_fixed",
           "the fixed finding closed as applied_fixed on the PASS follow-up (CLOSE-on-PASS)")


# ---- human_dismiss routing (fail-closed prefix) -------------------------------------------
def test_dismiss_routing_and_fail_closed_prefix():
    _truncate()
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, tip = _make_repo(tmp)
        out, _, _ = _run(outcomerecord.main, [
            "outcome-record", "--kilabz", "finding:fail-open @ f.py:2", "--oracle", "",
            "--", repo, base, tip, "main", "play1", "f.py"])
        key12 = out.strip().split("\t")[0]
        o1, _, rc1 = _run(outcomerecord.dismiss_main, ["outcome", key12[:8], "fp"])
        ok(rc1 == 0 and "refused" in o1, "a <12-hex prefix is refused (fail-closed), rc 0")
        o2, _, rc2 = _run(outcomerecord.dismiss_main, ["outcome", key12, "fp"])
        ok(rc2 == 0 and "dismissed 1" in o2, "the 12-hex prefix dismisses the kilabz finding")
        row = _q("SELECT outcome FROM finding_current WHERE finding_key LIKE $1 || '%' "
                 "AND reviewer_family='kilabz'", key12, one=True)
        ok(row["outcome"] == "dismissed_false_positive", "the dismissal landed as fp")


def test_dismiss_bad_kind_argparse_fails_open():
    # a kind not in {real,fp,wontfix} -> usage -> fail-open (rc 0), never raises.
    _, _, rc = _run(outcomerecord.dismiss_main, ["outcome", "0" * 12, "garbage"])
    ok(rc == 0, "a bad label kind fails open (rc 0)")


# ---- label-throughput PR-A: `real` kind + kind-first batch form (CLI arg surface) ----------
def test_label_real_single_and_batch_forms():
    _truncate()
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, tip = _make_repo(tmp)
        out, _, _ = _run(outcomerecord.main, [
            "outcome-record", "--kilabz", "finding:fail-open @ f.py:2", "--oracle", "",
            "--", repo, base, tip, "main", "play1", "f.py"])
        key12 = out.strip().split("\t")[0]
        # single legacy form, new `real` kind -> the gating confirmed_real row
        o1, _, rc1 = _run(outcomerecord.label_main, ["outcome", key12, "real"])
        ok(rc1 == 0 and "confirmed 1" in o1, "single form labels real (confirmed_real minted)")
        row = _q("SELECT outcome, outcome_source FROM finding_current_human "
                 "WHERE finding_key LIKE $1 || '%'", key12, one=True)
        ok(row["outcome"] == "confirmed_real" and row["outcome_source"] == "human_confirm",
           "the real label landed as (human_confirm, confirmed_real)")
        # kind-first batch form: valid key (idempotent re-issue -> 0 rows) + junk key refused,
        # four-count summary printed
        o2, _, rc2 = _run(outcomerecord.label_main, ["outcome", "real", key12, "zz!", key12])
        ok(rc2 == 0 and "labeled 1 (0 rows), refused 1, duplicates 1" in o2,
           "batch form: four-count summary (idempotent re-issue, junk refused, dup deduped)")


# ---- outcome-stats ------------------------------------------------------------------------
def test_stats_prints_open_count():
    _truncate()
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, tip = _make_repo(tmp)
        _run(outcomerecord.main, [
            "outcome-record", "--kilabz", "finding:fail-open @ f.py:2", "--oracle", "",
            "--", repo, base, tip, "main", "play1", "f.py"])
        out, _, rc = _run(outcomerecord.stats_main, ["outcome-stats"])
        ok(rc == 0, "outcome-stats exits 0")
        ok("open findings: 1" in out, "stats reports the open count")
        ok("fail-open" in out, "stats prints the fail-open precision row")


# ---- gate-mode HARD no-op + fail-open on bad input ---------------------------------------
def test_gate_mode_hard_noop():
    _truncate()
    os.environ["PLAY_GATE"] = "1"
    try:
        out, err, rc = _run(outcomerecord.main, [
            "outcome-record", "--kilabz", "finding:fail-open @ f.py:2", "--oracle", "",
            "--", "/tmp", "abc1234", "def5678", "main", "p", "f.py"])
        ok(rc == 0 and out.strip() == "", "gate mode records nothing (HARD no-op)")
        ok("PLAY_GATE" in err, "gate no-op is logged")
        ok(_q("SELECT count(*) FROM finding_outcome", val=True) == 0, "gate mode wrote no rows")
    finally:
        del os.environ["PLAY_GATE"]


def test_malformed_call_fails_open():
    # too few positionals -> argparse SystemExit -> fail-open (rc 0), never raises.
    _, _, rc = _run(outcomerecord.main, ["outcome-record", "onlyone"])
    ok(rc == 0, "a malformed call fails open (rc 0, no raise)")


def test_non_sha_base_tip_noop():
    out, _, rc = _run(outcomerecord.main, [
        "outcome-record", "--kilabz", "finding:fail-open @ f.py:2", "--oracle", "",
        "--", "/tmp", "not-a-sha", "also-bad", "main", "p", "f.py"])
    ok(rc == 0 and out.strip() == "", "a non-sha base/tip is a no-op (fail-closed on git-facing args)")


def test_bad_repo_path_noop():
    out, _, rc = _run(outcomerecord.main, [
        "outcome-record", "--kilabz", "finding:fail-open @ f.py:2", "--oracle", "",
        "--", "/no/such/dir/xyz", "abc1234", "def5678", "main", "p", "f.py"])
    ok(rc == 0 and out.strip() == "", "a non-directory repo_path is a no-op")


def _prepare_schema():
    async def go():
        led = await PostgresLedger.connect(DSN)
        async with led._pool.acquire() as con:
            await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await led.init_schema()
        await led.migrate()
        await led.close()
    asyncio.run(go())


def main():
    _prepare_schema()
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
