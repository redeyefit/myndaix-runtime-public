"""outcomes-ledger ledger verbs (v0.3) against a real Postgres — the append-only state machine:
record_findings (CLOSE = a FILE-CONTENT hash check via present_hashes, NOT reviewer-re-flag; OPEN
per (finding_key, reviewer_family) with sticky dismissals), human_dismiss (fail-closed prefix,
human-terminal precedence, mislabel CORRECTION fp<->wontfix), expire_open, outcome_stats. Two-family
(kilabz + oracle) independence is exercised throughout. The pure identity/parser core (incl.
file_line_hashes) is in test_outcomes.py.

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_outcomes_verbs.py
"""
import asyncio
import inspect
import os

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


def _finding(tag, path, content, family):
    """A resolved open-finding dict as the wiring (PR-B) would hand to record_findings."""
    return {"tag": tag, "path": path, "line_hash": outcomes.line_hash(content),
            "reviewer_family": family}


def _present(**path_to_lines):
    """Build the CLOSE-phase present_hashes {path: {line_hash,...}} the way the PR-B wiring will —
    from the CONTENT of each file at tip_sha (via outcomes.file_line_hashes). Here we pass the file's
    lines directly: _present(**{"src/a.py": ["return None", "x = 1"]}) -> {path: {hash,hash}}. Given []
    -> an empty set = CONFIRMED-absent file -> its findings close (§6). A path OMITTED entirely, or
    passed as None, is UNDETERMINED (transient git error) -> its findings are NOT closed (fail-closed)."""
    return {path: (None if lines is None else {outcomes.line_hash(line) for line in lines})
            for path, lines in path_to_lines.items()}


async def _current(led, fk, fam):
    return await led._pool.fetchrow(
        "SELECT outcome, outcome_source FROM finding_current WHERE finding_key=$1 AND reviewer_family=$2",
        fk, fam)


# ---- OPEN phase -------------------------------------------------------------------------
async def test_open_phase_inserts_open_rows(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    res = await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    ok(res["opened"] == 1 and res["closed"] == 0, "one open row inserted, nothing closed")
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    row = await _current(led, fk, "kilabz")
    ok(row is not None and row["outcome"] == "open", "the finding is CURRENTLY open")


async def test_idempotent_re_record_is_noop(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    pres = _present(**{"src/a.py": ["return None"]})   # the line is still in the file at tip
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f], pres)
    res2 = await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f], pres)
    ok(res2["opened"] == 0, "re-recording the SAME review (same source_event) opens nothing (idempotent)")
    ok(res2["closed"] == 0, "the line is still present -> not closed on the re-record")
    n = await led._pool.fetchval("SELECT count(*) FROM finding_outcome WHERE outcome='open'")
    ok(n == 1, "still exactly one open event (unique index dedup)")


# ---- CLOSE phase — FILE-CONTENT hash check (design §2), NOT reviewer-re-flag ---------------
async def test_close_phase_inserts_applied_fixed_when_line_gone(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    # a LATER review whose diff touched src/a.py, and the stored line is GONE from the file at tip
    # (present_hashes for src/a.py no longer contains it) -> applied_fixed.
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"],
                                    [], _present(**{"src/a.py": ["something else entirely"]}))
    ok(res["closed"] == 1, "the vanished line is closed as applied_fixed")
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "applied_fixed" and row["outcome_source"] == "auto_fix_landed",
       "current state is applied_fixed via auto_fix_landed")


async def test_close_skips_still_present_line(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    # the stored line is STILL in the file at tip -> NOT closed, even though this review re-raised it.
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"],
                                    [f], _present(**{"src/a.py": ["return None"]}))
    ok(res["closed"] == 0, "a line still present in the file is NOT closed")


async def test_close_undetermined_presence_does_not_close(led):
    # core-audit HIGH: present[path]=None (a TRANSIENT git error — presence couldn't be determined) must
    # NOT close the finding (no fabricated applied_fixed); a CONFIRMED delete (empty set) still does.
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    # transient: present[src/a.py] = None -> the open finding must STAY open (not fabricated fixed).
    # Routed through _present (kilabz: injecting the raw dict bypassed the helper's documented
    # None-pass-through, leaving that contract unexercised).
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"],
                                    [], _present(**{"src/a.py": None}))
    ok(res["closed"] == 0, "undetermined presence (None) does NOT close (no fabricated applied_fixed)")
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "open", "the finding is still OPEN after a transient git error")
    # a genuine delete (CONFIRMED empty set) DOES still close (§6)
    res2 = await led.record_findings("repoA", "main", "tip3", "play3", ["src/a.py"],
                                     [], {"src/a.py": set()})
    ok(res2["closed"] == 1, "a CONFIRMED-absent file (empty set) still closes as applied_fixed (§6)")


async def test_pass_review_does_not_false_close(led):
    # THE FIX-1 CRITICAL: a PASS (or an unrelated-line) review of the same file raises NO finding for
    # the still-real issue, yet the issue's line is STILL in the file. The old reviewer-re-flag logic
    # would false-close it (empty open_findings -> "present" empty -> everything closes). The
    # file-content check must keep it OPEN because the line is present.
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    # a PLAY_PASS review: empty open_findings, but the file still contains the flagged line + an
    # unrelated line the review actually looked at.
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"],
                                    [], _present(**{"src/a.py": ["return None", "unrelated edit"]}))
    ok(res["closed"] == 0, "a PASS review does NOT false-close a finding whose line is still present")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "open", "the real finding stays OPEN")


async def test_close_whole_file_deleted_closes(led):
    # the design-accepted whole-file-delete/rename case (§6): file_line_hashes CONFIRMED the file is
    # absent at tip (a successful ls-tree) and returned the EMPTY set -> the finding closes.
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"], [], {"src/a.py": set()})  # confirmed deleted
    ok(res["closed"] == 1, "a CONFIRMED-deleted file (empty set) closes its findings")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "applied_fixed", "current state applied_fixed")


async def test_close_requires_changed_path(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    # a review that did NOT touch the finding's path must not close it (path not in changed set), even
    # though present_hashes for src/a.py wouldn't contain the line.
    r_path = await led.record_findings("repoA", "main", "tip3", "playY", ["src/other.py"], [], {})
    ok(r_path["closed"] == 0, "a review that didn't change the path does NOT close it")


async def test_close_scopes_on_origin_ref_not_current(led):
    # FIX 5: a finding opened on ref A must NOT be closed by a review on ref B even if the line is gone
    # at B's tip. Close scopes on the finding's ORIGIN ref (its earliest open row), not a drifting
    # current-row ref.
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    # a review on a DIFFERENT ref, same path, line GONE at that ref's tip -> must NOT close the
    # main-origin finding.
    r_ref = await led.record_findings("repoA", "feature/x", "tip2", "playX", ["src/a.py"], [], {"src/a.py": set()})
    ok(r_ref["closed"] == 0, "a different-ref review does NOT close (origin-ref scoping)")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "open", "the main finding is still OPEN")
    # ...but a review on the finding's OWN ref (main), line gone, DOES close it.
    r_same = await led.record_findings("repoA", "main", "tip3", "playZ", ["src/a.py"], [], {"src/a.py": set()})
    ok(r_same["closed"] == 1, "a same-ref review with the line gone DOES close it")


# ---- sticky dismissal + human-terminal precedence ---------------------------------------
async def test_human_dismiss_and_sticky_reopen(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    pres = _present(**{"src/a.py": ["return None"]})
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f], pres)
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    res = await led.human_dismiss(fk[:12], "kilabz", "fp", principal_role="human")
    ok(res.get("dismissed") == 1 and res["finding_key"] == fk, "human_dismiss wrote one fp row")
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "dismissed_false_positive", "current state is the human dismissal")
    # a LATER review re-flagging the SAME line must NOT re-open (sticky dismissal).
    res2 = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"], [f], pres)
    ok(res2["opened"] == 0 and res2["skipped_dismissed"] == 1, "a dismissed key does NOT re-open (sticky)")
    row2 = await _current(led, fk, "kilabz")
    ok(row2["outcome"] == "dismissed_false_positive", "still dismissed after the re-record")


async def test_human_dismiss_correct_mislabel(led):
    # FIX 3: a human can CORRECT a mislabel (fp -> wontfix). The correcting row has a distinct
    # kind-qualified source_event so it INSERTS, and its higher seq wins in finding_current; the fp
    # count in finding_precision drops back.
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    r1 = await led.human_dismiss(fk[:12], "kilabz", "fp", principal_role="human")
    ok(r1["dismissed"] == 1, "first dismissal (fp) wrote a row")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "dismissed_false_positive", "labelled fp")
    prec_fp = await led._pool.fetchval(
        "SELECT dismissed_false_positive FROM finding_precision_raw WHERE rule_tag='fail-open' AND reviewer_family='kilabz'")
    ok(prec_fp == 1, "precision view shows 1 fp before the correction")
    # correct it to wontfix
    r2 = await led.human_dismiss(fk[:12], "kilabz", "wontfix", principal_role="human")
    ok(r2["dismissed"] == 1, "the correction (wontfix) wrote a NEW row (distinct kind source_event)")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "dismissed_wontfix",
       "current state is the CORRECTED label (higher-seq human row wins)")
    prec_fp2 = await led._pool.fetchval(
        "SELECT dismissed_false_positive FROM finding_precision_raw WHERE rule_tag='fail-open' AND reviewer_family='kilabz'")
    ok((prec_fp2 or 0) == 0, "the correction drops the fp count in finding_precision (current-state read)")
    # re-issuing the SAME kind is an idempotent no-op
    r3 = await led.human_dismiss(fk[:12], "kilabz", "wontfix", principal_role="human")
    ok(r3["dismissed"] == 0, "re-issuing the same kind is an idempotent no-op (0 written)")


async def test_human_terminal_precedence_over_later_machine_row(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [f], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    await led.human_dismiss(fk[:12], "kilabz", "wontfix", principal_role="human")
    # force a LATER machine row (higher seq) for the same key directly — a close event that races in.
    await led._pool.execute(
        """INSERT INTO finding_outcome (id, finding_key, repo_id, ref, rule_tag, reviewer_family,
               path, line_hash, source_event, tip_sha, outcome, outcome_source)
           VALUES (gen_random_uuid(), $1, 'repoA', 'main', 'fail-open', 'kilabz', 'src/a.py', $2,
                   'review:late', 'tipLate', 'applied_fixed', 'auto_fix_landed')""",
        fk, f["line_hash"])
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "dismissed_wontfix",
       "the human dismissed_* row WINS over a later (higher-seq) machine applied_fixed (precedence)")


async def test_human_dismiss_fail_closed_on_short_prefix(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    res = await led.human_dismiss(fk[:8], "kilabz", "fp", principal_role="human")   # < 12 hex chars
    ok("error" in res, "a <12-hex prefix is refused (fail-closed)")
    row = await _current(led, fk, "kilabz")
    ok(row["outcome"] == "open", "nothing was dismissed on the short-prefix refusal")


async def test_human_dismiss_fail_closed_on_ambiguous_prefix(led):
    await _truncate(led)
    # two DISTINCT keys that share a >=12-hex prefix don't occur naturally (sha256), so seed two rows
    # whose finding_key we control to share a prefix, and assert the verb refuses + lists both.
    shared = "abcdef012345"
    for suffix in ("aaaa", "bbbb"):
        fk = shared + suffix + "0" * (64 - len(shared) - 4)
        await led._pool.execute(
            """INSERT INTO finding_outcome (id, finding_key, repo_id, ref, rule_tag, reviewer_family,
                   path, line_hash, source_event, tip_sha, outcome, outcome_source)
               VALUES (gen_random_uuid(), $1, 'repoA', 'main', 'fail-open', 'kilabz', 'src/a.py',
                       'h', 'review:x', 'tip', 'open', 'review_raised')""", fk)
    res = await led.human_dismiss(shared, "kilabz", "fp", principal_role="human")
    ok("error" in res and len(res.get("candidates", [])) == 2,
       "an ambiguous prefix is refused and BOTH colliding full keys are returned (fail-closed)")


# ---- cross-file collision does NOT merge histories --------------------------------------
async def test_cross_file_collision_does_not_merge(led):
    await _truncate(led)
    fa = _finding("fail-open", "src/a.py", "return None", "kilabz")
    fb = _finding("fail-open", "src/b.py", "return None", "kilabz")   # SAME line, different file
    ok(fa["line_hash"] == fb["line_hash"], "the two files share a line_hash (same content)")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py", "src/b.py"], [fa, fb])
    ka = outcomes.finding_key("repoA", "fail-open", "src/a.py", fa["line_hash"])
    kb = outcomes.finding_key("repoA", "fail-open", "src/b.py", fb["line_hash"])
    ok(ka != kb, "path-in-key gives them DISTINCT finding_keys")
    # dismiss a.py's finding; b.py's must stay open (histories not merged).
    await led.human_dismiss(ka[:12], "kilabz", "fp", principal_role="human")
    ok((await _current(led, ka, "kilabz"))["outcome"] == "dismissed_false_positive", "a.py finding dismissed")
    ok((await _current(led, kb, "kilabz"))["outcome"] == "open", "b.py finding UNAFFECTED (separate history)")


# ---- two-family independence (FIX 2 + FIX 4): kilabz AND oracle, per-family state -----------
async def test_open_dedups_per_family_not_key(led):
    # FIX 2: both families flag the SAME (repo,tag,path,content). The OPEN accumulator dedups by
    # (finding_key, reviewer_family), so BOTH rows open — one family's row is not lost.
    await _truncate(led)
    fk_kila = _finding("fail-open", "src/a.py", "return None", "kilabz")
    fk_orac = _finding("fail-open", "src/a.py", "return None", "oracle")   # identical but oracle
    res = await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                                    [fk_kila, fk_orac], _present(**{"src/a.py": ["return None"]}))
    ok(res["opened"] == 2, "both families' identical finding opens 2 rows (per-family dedup)")
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", fk_kila["line_hash"])
    ok((await _current(led, fk, "kilabz"))["outcome"] == "open", "kilabz has an independent open row")
    ok((await _current(led, fk, "oracle"))["outcome"] == "open", "oracle has an independent open row")


async def test_per_family_dismissal_independent(led):
    # FIX 4: dismiss kilabz's finding; oracle's stays open (per-family state).
    await _truncate(led)
    fk_kila = _finding("fail-open", "src/a.py", "return None", "kilabz")
    fk_orac = _finding("fail-open", "src/a.py", "return None", "oracle")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [fk_kila, fk_orac], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", fk_kila["line_hash"])
    await led.human_dismiss(fk[:12], "kilabz", "fp", principal_role="human")   # dismiss ONLY kilabz's
    ok((await _current(led, fk, "kilabz"))["outcome"] == "dismissed_false_positive", "kilabz dismissed")
    ok((await _current(led, fk, "oracle"))["outcome"] == "open", "oracle's finding is UNAFFECTED (open)")


async def test_dismiss_all_families(led):
    # 'all' dismisses every family currently open on the key (both here).
    await _truncate(led)
    fk_kila = _finding("fail-open", "src/a.py", "return None", "kilabz")
    fk_orac = _finding("fail-open", "src/a.py", "return None", "oracle")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [fk_kila, fk_orac], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", fk_kila["line_hash"])
    res = await led.human_dismiss(fk[:12], "all", "wontfix", principal_role="human")
    ok(res["dismissed"] == 2, "'all' dismisses both families currently open on the key")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "dismissed_wontfix", "kilabz dismissed")
    ok((await _current(led, fk, "oracle"))["outcome"] == "dismissed_wontfix", "oracle dismissed")


async def test_per_family_close_independent(led):
    # per-family close independence: the line is gone -> BOTH families' findings close on the same
    # review (each family's open row is evaluated on the same file-content set).
    await _truncate(led)
    fk_kila = _finding("fail-open", "src/a.py", "return None", "kilabz")
    fk_orac = _finding("fail-open", "src/a.py", "return None", "oracle")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"],
                              [fk_kila, fk_orac], _present(**{"src/a.py": ["return None"]}))
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", fk_kila["line_hash"])
    # oracle re-flags the same line this review, but the FILE no longer contains it -> both close
    # (close is a file-content check, not a re-flag check — so re-flagging can't keep it open).
    res = await led.record_findings("repoA", "main", "tip2", "play2", ["src/a.py"], [], {"src/a.py": set()})
    ok(res["closed"] == 2, "both families' findings close when the line is gone from the file")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "applied_fixed", "kilabz closed")
    ok((await _current(led, fk, "oracle"))["outcome"] == "applied_fixed", "oracle closed")


# ---- expire_open ------------------------------------------------------------------------
async def test_expire_open(led):
    await _truncate(led)
    f = _finding("fail-open", "src/a.py", "return None", "kilabz")
    await led.record_findings("repoA", "main", "tip1", "play1", ["src/a.py"], [f])
    fk = outcomes.finding_key("repoA", "fail-open", "src/a.py", f["line_hash"])
    ok(await led.expire_open(30) == 0, "a fresh open finding is NOT expired")
    # backdate the open row past the TTL
    await led._pool.execute(
        "UPDATE finding_outcome SET created_at = now() - interval '40 days' WHERE finding_key=$1", fk)
    ok(await led.expire_open(30) == 1, "an over-TTL open finding is expired")
    ok((await _current(led, fk, "kilabz"))["outcome"] == "expired", "current state is expired")
    ok(await led.expire_open(30) == 0, "re-running the sweep the same UTC day is a no-op (deterministic source_event)")


# ---- outcome_stats ----------------------------------------------------------------------
async def test_outcome_stats(led):
    await _truncate(led)
    # one applied_fixed + one dismissed_false_positive for the SAME class -> precision 0.5.
    f1 = _finding("fail-open", "src/a.py", "line one", "kilabz")
    f2 = _finding("fail-open", "src/b.py", "line two", "kilabz")
    await led.record_findings("repoA", "main", "t1", "p1", ["src/a.py", "src/b.py"], [f1, f2])
    k2 = outcomes.finding_key("repoA", "fail-open", "src/b.py", f2["line_hash"])
    await led.human_dismiss(k2[:12], "kilabz", "fp", principal_role="human")                 # f2 -> fp
    # f1's line ("line one") is GONE from src/a.py at t2 (present_hashes omits it) -> applied_fixed.
    await led.record_findings("repoA", "main", "t2", "p2", ["src/a.py"], [], {"src/a.py": set()})
    stats = await led.outcome_stats()
    ok(stats["open_count"] == 0, "no findings left open")
    row = next((r for r in stats["precision"] if r["rule_tag"] == "fail-open"
                and r["reviewer_family"] == "kilabz"), None)
    ok(row is not None and row["applied_fixed"] == 1 and row["dismissed_false_positive"] == 1,
       "the class shows 1 applied_fixed + 1 fp")
    ok(abs(float(row["precision"]) - 0.5) < 1e-9, "precision = 1/(1+1) = 0.5")


async def main():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    await led.migrate()   # prove 0008 is idempotent against the schema.sql mirror (IF NOT EXISTS)
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    try:
        for t in tests:
            await t(led)
            print("PASS", t.__name__)
    finally:
        await led.close()
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    asyncio.run(main())
