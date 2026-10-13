"""auto-capture ledger verbs (v0.4) against a real Postgres — multi-signal recurrence (S3) + the
S6 state machine (record -> ready -> proposing -> proposed -> promoted|declined|stale). The pure
recurrence core is in test_capture.py.

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_capture_verbs.py
"""
import asyncio
import inspect
import os

from runtime import capture
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
PASS = [0]
FAIL = [0]
TH = dict(min_recur=3, min_events=2, min_authors=1, repropose_mult=2)


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


async def _truncate(led):
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE capture_candidate, capture_occurrence")


async def sight(led, repo, tag, glob, commit, event, author, cross=True, **over):
    th = {**TH, **over}
    return await led.record_capture(repo, tag, glob, commit, event, author, cross, **th)


async def _drive_to_ready(led, repo="repoA", tag="fail-open", glob="src/*.py"):
    """3 distinct commits across 2 events, 1 author -> ready; returns the ready dict from the 3rd."""
    await sight(led, repo, tag, glob, "c1", "e1", "a1")
    await sight(led, repo, tag, glob, "c2", "e1", "a1")
    return await sight(led, repo, tag, glob, "c3", "e2", "a1")


# ---- S3: multi-signal recurrence --------------------------------------------------------
async def test_fires_only_when_all_signals_met(led):
    await _truncate(led)
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c1", "e1", "a1") is None, "1 commit -> not ready")
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c2", "e1", "a1") is None,
       "2 commits but ONE event -> not ready (anti-single-push)")
    r = await sight(led, "repoA", "fail-open", "src/*.py", "c3", "e2", "a1")
    ok(r is not None and r["rule_tag"] == "fail-open" and r["commits"] == 3,
       "3rd commit in a 2nd event -> READY")
    ok(r["events"] == 2 and r["authors"] == 1, "ready dict carries the distinct counts")


async def test_occurrence_deduped_per_commit(led):
    await _truncate(led)
    await sight(led, "repoA", "toctou-race", "src/*.py", "c1", "e1", "a1")
    await sight(led, "repoA", "toctou-race", "src/*.py", "c1", "e9", "a9")  # same commit, replayed
    fp = capture.fingerprint("repoA", "toctou-race")
    n = await led._pool.fetchval("SELECT count(*) FROM capture_occurrence WHERE fingerprint=$1", fp)
    ok(n == 1, "a replayed commit does not double-count (PK dedup)")


# ---- fail-closed gates ------------------------------------------------------------------
async def test_non_cross_family_records_nothing(led):
    await _truncate(led)
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c1", "e1", "a1", cross=False) is None,
       "single-family signal returns None")
    fp = capture.fingerprint("repoA", "fail-open")
    n = await led._pool.fetchval("SELECT count(*) FROM capture_candidate WHERE fingerprint=$1", fp)
    ok(n == 0, "single-family signal records NO candidate row at all")


async def test_offlist_tag_and_skills_path_record_nothing(led):
    await _truncate(led)
    ok(await sight(led, "repoA", "made-up-tag", "src/*.py", "c1", "e1", "a1") is None, "off-list tag ignored")
    ok(await sight(led, "repoA", "fail-open", "skills/x/SKILL.md", "c1", "e1", "a1") is None,
       "a signal from skills/** is ignored (no self-capture loop)")
    n = await led._pool.fetchval("SELECT count(*) FROM capture_candidate")
    ok(n == 0, "neither formed a candidate")


# ---- S6 state machine -------------------------------------------------------------------
async def test_claim_propose_resolve_happy_path(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    ok(await led.claim_for_proposing(fp, "skill/auto/fail-open", "deadbeef") is True, "ready -> proposing (CAS)")
    ok(await led.claim_for_proposing(fp, "skill/auto/fail-open", "deadbeef") is False, "double-claim blocked")
    ok(await led.count_open_proposals() == 1, "a proposing class counts as open")
    ok(await led.mark_capture_proposed(fp, "skill/auto/fail-open", "deadbeef", 42) is True, "proposing -> proposed")
    ok(await led.mark_capture_proposed(fp, "skill/auto/fail-open", "deadbeef", 43) is False, "double-propose blocked by CAS")
    ok(await led.resolve_capture(fp, "promoted") is True, "proposed -> promoted")
    ok(await led.count_open_proposals() == 0, "a promoted class is no longer open")
    ok(await led.resolve_capture(fp, "declined") is False, "resolve is a CAS from 'proposed' only")


async def test_already_ready_not_re_returned(led):
    await _truncate(led)
    await _drive_to_ready(led)
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c4", "e3", "a1") is None,
       "a class already 'ready' keeps counting but is not re-returned (no double-claim)")


async def test_release_proposing_recovers(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "skill/auto/fail-open", "sha")
    ok(await led.release_proposing(fp, "skill/auto/fail-open", "sha") is True, "proposing -> ready (recovery)")
    ok(await led.claim_for_proposing(fp, "skill/auto/fail-open", "sha2") is True, "re-claimable after release")


async def test_mark_release_fenced_on_claim_identity(led):
    # cross-family: after reap releases A's claim and B re-claims, A's late mark/release must NOT
    # clobber B's live claim (would orphan/duplicate PRs). Fence on (branch, draft_sha).
    await _truncate(led)
    fp = (await _drive_to_ready(led))["fingerprint"]
    await led.claim_for_proposing(fp, "branchA", "shaA")
    await led._pool.execute(
        "UPDATE capture_candidate SET proposed_at = now() - interval '120 minutes' WHERE fingerprint=$1", fp)
    await led.reap_stuck_proposing(60)                        # A's claim released -> ready
    await led.claim_for_proposing(fp, "branchB", "shaB")      # B claims
    ok(await led.mark_capture_proposed(fp, "branchA", "shaA", 1) is False, "A's stale mark is fenced out")
    ok(await led.release_proposing(fp, "branchA", "shaA") is False, "A's stale release is fenced out")
    ok(await led.mark_capture_proposed(fp, "branchB", "shaB", 2) is True, "B's real claim marks proposed")


async def test_declined_reproposes_only_past_higher_floor(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "b", "s")
    await led.mark_capture_proposed(fp, "b", "s", 7)
    ok(await led.resolve_capture(fp, "declined") is True, "proposed -> declined")
    dc = await led._pool.fetchval("SELECT decline_count FROM capture_candidate WHERE fingerprint=$1", fp)
    ok(dc == 1, "decline_count incremented")
    # base floor is 3; declined-once floor is 3*2=6. commits 4 and 5 do NOT re-fire.
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c4", "e3", "a1") is None, "4 commits < 6 floor")
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c5", "e4", "a1") is None, "5 commits < 6 floor")
    r6 = await sight(led, "repoA", "fail-open", "src/*.py", "c6", "e5", "a1")
    ok(r6 is not None and r6["decline_count"] == 1, "6th commit crosses the doubled floor -> re-ready")


async def test_stale_class_re_accumulates_with_backoff(led):
    # cross-family: a TTL-staled class must NOT wedge (re-readyable) but must respect the EXPONENTIAL
    # backoff (decline_count++ on stale) — else it re-proposes on the very next sighting (CRITICAL).
    await _truncate(led)
    fp = (await _drive_to_ready(led))["fingerprint"]      # 3 commits c1-c3
    await led.claim_for_proposing(fp, "b", "s")
    await led.mark_capture_proposed(fp, "b", "s", 55)
    await led._pool.execute(
        "UPDATE capture_candidate SET proposed_at = now() - interval '30 days' WHERE fingerprint=$1", fp)
    await led.expire_stale_captures(14)
    ok(await led._pool.fetchval("SELECT state FROM capture_candidate WHERE fingerprint=$1", fp) == "stale",
       "over-TTL proposal -> stale")
    # decline_count is now 1 -> floor = min_recur*2 = 6. It has 3 commits; the next 2 must NOT re-fire.
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c4", "e3", "a1") is None, "4 commits < 6 floor")
    ok(await sight(led, "repoA", "fail-open", "src/*.py", "c5", "e4", "a1") is None, "5 commits < 6 floor")
    r6 = await sight(led, "repoA", "fail-open", "src/*.py", "c6", "e5", "a1")
    ok(r6 is not None, "6th commit crosses the doubled floor -> re-ready (no wedge, with backoff)")


async def test_zz_migration_heals_v03_remnant(led):
    # cross-family suggestion: codify the guarded migration heal. Seed the pre-ship v0.3 shape, run
    # migrate(), assert the v0.4 column appears (healed) and a re-run is idempotent.
    async with led._pool.acquire() as con:
        await con.execute("DROP TABLE IF EXISTS capture_occurrence, capture_candidate CASCADE")
        await con.execute("""CREATE TABLE capture_candidate (
            fingerprint text PRIMARY KEY, repo_scope text NOT NULL, path_glob text NOT NULL,
            seen_count int NOT NULL DEFAULT 1, state text NOT NULL DEFAULT 'candidate',
            pr_number int, first_seen timestamptz DEFAULT now(), last_seen timestamptz DEFAULT now())""")
    has = await led._pool.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_name='capture_candidate' AND column_name='rule_tag'")
    ok(has == 0, "seeded the v0.3 shape (no rule_tag)")
    await led.migrate()
    has = await led._pool.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_name='capture_candidate' AND column_name='rule_tag'")
    ok(has == 1, "migrate() healed the v0.3 remnant to v0.4 (rule_tag present)")
    await led._pool.execute(
        "INSERT INTO capture_candidate(fingerprint,repo_scope,rule_tag) VALUES ('keep','r','fail-open')")
    await led.migrate()                                     # re-run: must NOT drop the healthy table
    kept = await led._pool.fetchval("SELECT count(*) FROM capture_candidate WHERE fingerprint='keep'")
    ok(kept == 1, "re-running migrate() is idempotent — does not drop a healthy v0.4 table")


async def test_reap_stuck_proposing_recovers_slot(led):
    # cross-family MAJOR: a crash after ready->proposing must not occupy a MAX_OPEN slot forever.
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "skill/auto/fail-open", "sha")
    ok(await led.count_open_proposals() == 1, "proposing occupies a slot")
    ok(await led.reap_stuck_proposing(60) == 0, "a fresh proposing row is NOT reaped")
    await led._pool.execute(
        "UPDATE capture_candidate SET proposed_at = now() - interval '120 minutes' WHERE fingerprint=$1", fp)
    ok(await led.reap_stuck_proposing(60) == 1, "a stuck proposing row is reaped after the timeout")
    ok(await led.count_open_proposals() == 0, "the slot is freed")
    ok(await led.claim_for_proposing(fp, "b2", "s2") is True, "reaped class is re-claimable (back to ready)")


async def test_resolve_bad_outcome_raises(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    await led.claim_for_proposing(r["fingerprint"], "b", "s")
    await led.mark_capture_proposed(r["fingerprint"], "b", "s", 9)
    raised = False
    try:
        await led.resolve_capture(r["fingerprint"], "garbage")
    except ValueError:
        raised = True
    ok(raised, "resolve_capture rejects a bad outcome (no silent bad state)")


async def test_expire_stale_closes_abandoned_pr(led):
    await _truncate(led)
    r = await _drive_to_ready(led)
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "b", "s")
    await led.mark_capture_proposed(fp, "b", "s", 99)
    # backdate the proposal past the TTL
    await led._pool.execute(
        "UPDATE capture_candidate SET proposed_at = now() - interval '30 days' WHERE fingerprint=$1", fp)
    stale = await led.expire_stale_captures(14)
    ok(len(stale) == 1 and stale[0]["pr_number"] == 99, "an over-TTL proposal is returned for PR-close")
    ok(await led.count_open_proposals() == 0, "a stale class frees its MAX_OPEN slot (anti-wedge)")
    dc = await led._pool.fetchval("SELECT decline_count FROM capture_candidate WHERE fingerprint=$1", fp)
    ok(dc == 1, "staling increments decline_count (so re-accumulation respects the backoff floor)")
    # a fresh proposal is NOT expired
    await _truncate(led)
    r2 = await _drive_to_ready(led)
    await led.claim_for_proposing(r2["fingerprint"], "b", "s")
    await led.mark_capture_proposed(r2["fingerprint"], "b", "s", 100)
    ok(await led.expire_stale_captures(14) == [], "a fresh proposal is not expired")


async def test_list_ready_candidates(led):
    await _truncate(led)
    r = await _drive_to_ready(led, repo="repoR", tag="toctou-race")
    rows = await led.list_ready_candidates(10)
    ok(any(x["fingerprint"] == r["fingerprint"] and x["repo_scope"] == "repoR"
           and x["rule_tag"] == "toctou-race" for x in rows), "the ready class is listed")
    ok(all(set(("fingerprint", "repo_scope", "rule_tag", "path_glob", "decline_count")) <= set(x)
           for x in rows), "list carries exactly the fields the resolve+render need")


async def test_list_ready_keyset_cursor(led):
    # kilabz r2 #7 (fairness): the scan is fingerprint-keyset — `after` returns strictly-greater
    # fingerprints only, so a persisted cursor rotates past previously-visited rows.
    await _truncate(led)
    r1 = await _drive_to_ready(led, repo="repoK1", tag="fail-open")
    r2 = await _drive_to_ready(led, repo="repoK2", tag="toctou-race")
    fps = sorted([r1["fingerprint"], r2["fingerprint"]])
    rows = await led.list_ready_candidates(10)
    ok([x["fingerprint"] for x in rows] == fps, "no cursor -> all ready rows in fingerprint order")
    rows = await led.list_ready_candidates(10, after=fps[0])
    ok([x["fingerprint"] for x in rows] == [fps[1]], "after=first -> only the strictly-greater row")
    ok(await led.list_ready_candidates(10, after=fps[1]) == [], "after=last -> empty (caller wraps)")


async def test_capture_provenance_hex_filtered(led):
    await _truncate(led)
    for c, e in (("deadbeef01", "e1"), ("cafebabe02", "e2")):
        await sight(led, "repoP", "fail-open", "src/*.py", c, e, "a1")
    await sight(led, "repoP", "fail-open", "src/*.py", "not-a-real-sha", "e3", "a1")
    fp = capture.fingerprint("repoP", "fail-open")
    prov = await led.capture_provenance(fp)
    ok(set(prov) == {"deadbeef01", "cafebabe02"}, "provenance returns the hex commit_shas")
    ok("not-a-real-sha" not in prov, "a non-hex commit_sha is filtered out in SQL (A3 belt)")


async def test_list_proposed(led):
    await _truncate(led)
    r = await _drive_to_ready(led, repo="repoQ")
    fp = r["fingerprint"]
    ok(await led.list_proposed() == [], "a merely-ready class is NOT in list_proposed")
    await led.claim_for_proposing(fp, "skill/auto/fail-open", "sha")
    await led.mark_capture_proposed(fp, "skill/auto/fail-open", "sha", 55)
    rows = await led.list_proposed()
    ok(len(rows) == 1 and rows[0]["pr_number"] == 55 and rows[0]["branch"] == "skill/auto/fail-open",
       "list_proposed returns the open proposal with its pr_number + branch (RECONCILE queue)")


async def test_resolve_stale(led):
    await _truncate(led)
    r = await _drive_to_ready(led, repo="repoS")
    fp = r["fingerprint"]
    await led.claim_for_proposing(fp, "b", "s")
    await led.mark_capture_proposed(fp, "b", "s", 66)
    ok(await led.resolve_capture(fp, "stale") is True, "stale resolves from 'proposed'")
    st = await led._pool.fetchrow(
        "SELECT state, decline_count, pr_number FROM capture_candidate WHERE fingerprint=$1", fp)
    ok(st["state"] == "stale", "state -> stale")
    ok(st["decline_count"] == 1, "stale bumps decline_count (respects the repropose backoff)")
    ok(st["pr_number"] is None, "proposal fields cleared")
    ok(await led.count_open_proposals() == 0, "a staled class frees its MAX_OPEN slot")
    ok(await led.resolve_capture(fp, "stale") is False, "idempotent: already-resolved returns False")


async def main():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
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
