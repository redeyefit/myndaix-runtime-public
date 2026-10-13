"""Self-labeling FENCE proofs (docs/self-labeling-design.md v0.4, dual-family APPROVE) — PR-1:
migration 0010 + the three write verbs. The airtightness matrix both families demanded:

  • no machine row (panel_proposed / exec_verified) enters finding_precision_promoted
  • NO machine LABEL source removes a finding from finding_labelqueue — only a human label or the
    TTL tombstone (panel_proposed, exec_verified, auto_fix_landed, review_raised, auto_git_revert all
    leave it in the queue)
  • a repeat / fp->real correction human confirm resolves to ONE current human label (no double-count)
  • the principal->source matrix denies a machine identity the human verb; each verb mints only its
    exact (source, outcome) pairs
  • the source-aware idempotency tuple (same=noop, cross-source no-shadow)
  • the v1 all-source metric is renamed finding_precision_raw (no gating-suggestive name survives)

Run: LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_self_labeling_fence.py
"""
import asyncio
import inspect
import os
import uuid

import asyncpg

from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")

FK = "a" * 64          # deterministic 64-hex finding_keys (real keys are sha256)
FK2 = "b" * 64


async def _seed(con, *, finding_key=FK, family="kilabz", outcome="open",
                source="review_raised", source_event=None, rule_tag="fail-open",
                path="src/x.py", tip="c" * 40):
    """Insert one raw finding_outcome row — seed a raised finding, a machine label, or a tombstone."""
    se = source_event or f"review:{finding_key[:8]}"
    await con.execute(
        """INSERT INTO finding_outcome
               (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path, line_hash,
                source_event, tip_sha, outcome, outcome_source)
           VALUES ($1,$2,'r','refs/heads/main',$3,$4,$5,$6,$7,$8,$9,$10)
           ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source, source_event)
               DO NOTHING""",
        uuid.uuid4(), finding_key, rule_tag, family, path, "lh-" + finding_key[:6],
        se, tip, outcome, source)


async def _truncate(led):
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE finding_outcome RESTART IDENTITY")


async def _val(led, sql, *a):
    async with led._pool.acquire() as con:
        return await con.fetchval(sql, *a)


async def _in_queue(led, finding_key=FK, family="kilabz") -> bool:
    return bool(await _val(led,
        "SELECT count(*) FROM finding_labelqueue WHERE finding_key=$1 AND reviewer_family=$2",
        finding_key, family))


async def _promoted_real(led, rule_tag="fail-open", family="kilabz"):
    return await _val(led,
        "SELECT confirmed_real FROM finding_precision_promoted WHERE rule_tag=$1 AND reviewer_family=$2",
        rule_tag, family)


# ---- the fence -------------------------------------------------------------------------------

async def test_migration_renamed_raw_and_added_views(led):
    # the v1 gating-suggestive name is GONE; the fenced views exist.
    assert await _val(led, "SELECT to_regclass('finding_precision')") is None, \
        "finding_precision must be renamed away (attractive nuisance)"
    for v in ("finding_precision_promoted", "finding_precision_raw",
              "finding_current_human", "finding_labelqueue"):
        assert await _val(led, "SELECT to_regclass($1)", v) is not None, f"missing view {v}"
    # the 5-col idempotency index replaced the 4-col one
    assert await _val(led, "SELECT to_regclass('finding_outcome_event_src_once')") is not None
    assert await _val(led, "SELECT to_regclass('finding_outcome_event_once')") is None


async def test_no_machine_row_enters_promoted(led):
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)                                          # raise the finding
    # machine labels: a panel proposal (REAL) + an exec prior (REAL)
    await led.propose_outcome(FK, "kilabz", "real", principal_role="labeler", play="p1")
    await led.record_exec_prior(FK, "kilabz", principal_role="exec_oracle", tip_sha="c" * 40)
    # promoted precision sees NOTHING (no human confirmed_real): the class row is absent or 0
    assert (await _promoted_real(led)) in (None, 0), "a machine label reached the gating metric"
    # only a HUMAN confirm enters it
    r = await led.confirm_outcome(FK, "kilabz", "real", principal_role="human")
    assert r["written"] == 1
    assert (await _promoted_real(led)) == 1, "a human REAL confirm must count in promoted precision"


async def test_labelqueue_is_machine_blind(led):
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)                                          # raised -> in queue
    assert await _in_queue(led), "a raised finding must be in the label queue"
    # EVERY non-human label source must FAIL to remove it
    await led.propose_outcome(FK, "kilabz", "fp", principal_role="labeler", play="p1")
    await led.record_exec_prior(FK, "kilabz", principal_role="exec_oracle", tip_sha="c" * 40)
    async with led._pool.acquire() as con:
        await _seed(con, outcome="applied_fixed", source="auto_fix_landed", source_event="af:1")
        await _seed(con, outcome="reverted", source="auto_git_revert", source_event="rv:1")
        await _seed(con, outcome="open", source="review_raised", source_event="review:again")
    assert await _in_queue(led), "a machine label/prior/churn must NOT remove a finding from the queue"
    # a HUMAN label removes it
    await led.confirm_outcome(FK, "kilabz", "fp", principal_role="human")
    assert not await _in_queue(led), "a human label must remove the finding from the queue"


async def test_ttl_tombstone_removes_but_is_not_a_label(led):
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
        await _seed(con, outcome="expired", source="ttl_sweep", source_event="sweep:2026-07-10")
    assert not await _in_queue(led), "an expired tombstone ages a finding out of the active queue"
    # but the tombstone is NOT a label: it never enters promoted precision
    assert (await _promoted_real(led)) in (None, 0)


async def test_reraise_after_expired_returns_to_queue(led):
    # regression (migration 0011, found by the loop on its own 0010 merge): the expired tombstone must be
    # LATEST-lifecycle-aware. A finding that expired then legitimately re-detects (the documented
    # re-raise-after-expired regression rule) gets a fresh review_raised row and MUST reappear in the
    # queue — the old all-history NOT EXISTS check hid it forever.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)                                                             # open (seq 1)
        await _seed(con, outcome="expired", source="ttl_sweep",
                    source_event="sweep:2026-07-10")                                 # expired (seq 2)
        assert not await _in_queue(led), "still tombstoned while expired is the latest state"
        await _seed(con, source_event="review:reraise")                             # re-raised open (seq 3)
    assert await _in_queue(led), "a finding re-raised after expiry must RETURN to the label queue"


async def test_machine_label_after_expiry_does_not_resurrect(led):
    # regression (0011 r1, kilabz): a LABEL source (panel_proposed / exec_verified) written AFTER a
    # finding expired must NOT lift the tombstone — only the open/expire lifecycle toggle
    # (review_raised / ttl_sweep) may. A machine can never change queue membership; only a genuine
    # re-detection or a human label does. (The first cut checked "latest non-human row", which let a
    # stale post-TTL machine write resurrect an expired finding without a fresh detection.)
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)                                                             # open (seq 1)
        await _seed(con, outcome="expired", source="ttl_sweep",
                    source_event="sweep:2026-07-10")                                 # expired (seq 2)
        await _seed(con, outcome="panel_real", source="panel_proposed",
                    source_event="panel:x")                                          # machine label (seq 3)
    assert not await _in_queue(led), "a machine label after expiry must NOT resurrect an expired finding"


async def test_reraise_resets_ttl_age_not_first_raise(led):
    # regression (0011 r2, kilabz HIGH): the labelqueue fix un-hides a re-raised finding, but expire_open
    # must anchor TTL age on the LATEST review_raised, not min(created_at) over all history — else the
    # re-raised finding (whose FIRST detection is old) is instantly re-expired on the next sweep, silently
    # re-hiding a live finding. Backdate the first detection + expiry far past TTL; re-raise NOW.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)                                                             # first detection
        await _seed(con, outcome="expired", source="ttl_sweep", source_event="sweep:old")
        await con.execute(
            "UPDATE finding_outcome SET created_at = now() - interval '99 days' WHERE finding_key=$1", FK)
        await _seed(con, source_event="review:reraise")                             # re-raised NOW (recent)
    assert await _in_queue(led), "re-raised finding is visible again"
    n = await led.expire_open(30)
    assert n == 0, "a finding re-raised within TTL must NOT be re-expired on its first-detection age"
    assert await _in_queue(led), "re-raised finding stays in the queue (not immediately re-tombstoned)"


async def test_no_double_count_on_repeat_and_correction(led):
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
    # repeat same-kind confirm is idempotent
    await led.confirm_outcome(FK, "kilabz", "fp", principal_role="human")
    r2 = await led.confirm_outcome(FK, "kilabz", "fp", principal_role="human")
    assert r2["written"] == 0, "a repeat same-kind confirm must be a no-op"
    n = await _val(led,
        "SELECT count(*) FROM finding_current_human WHERE finding_key=$1 AND reviewer_family=$2",
        FK, "kilabz")
    assert n == 1, "finding_current_human must hold exactly ONE current label"
    # a correction fp->real flips the current label (higher seq wins) without double-counting
    await led.confirm_outcome(FK, "kilabz", "real", principal_role="human")
    cur = await _val(led,
        "SELECT outcome FROM finding_current_human WHERE finding_key=$1 AND reviewer_family=$2",
        FK, "kilabz")
    assert cur == "confirmed_real", "the correction must become the current label"
    assert (await _promoted_real(led)) == 1
    fp = await _val(led,
        "SELECT dismissed_false_positive FROM finding_precision_promoted "
        "WHERE rule_tag='fail-open' AND reviewer_family='kilabz'")
    assert fp == 0, "the corrected-away fp must not still count"


async def test_principal_matrix_denies_machine_the_human_verb(led):
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
    for role in ("labeler", "exec_oracle", "reviewer", ""):
        try:
            await led.confirm_outcome(FK, "kilabz", "real", principal_role=role)
            raise AssertionError(f"confirm_outcome allowed non-human role {role!r}")
        except PermissionError:
            pass
    for bad in ("human", "labeler", "admin"):
        try:
            await led.record_exec_prior(FK, "kilabz", principal_role=bad, tip_sha="c" * 40)
            raise AssertionError(f"record_exec_prior allowed {bad!r}")
        except PermissionError:
            pass
    for bad in ("human", "exec_oracle", "admin"):
        try:
            await led.propose_outcome(FK, "kilabz", "real", principal_role=bad, play="p")
            raise AssertionError(f"propose_outcome allowed {bad!r}")
        except PermissionError:
            pass
    # off-pair verdicts/kinds are rejected (no verb can mint an off-pair combination)
    for kind in ("bogus", "confirmed_real"):
        try:
            await led.confirm_outcome(FK, "kilabz", kind, principal_role="human")
            raise AssertionError(f"confirm_outcome accepted bad kind {kind!r}")
        except ValueError:
            pass
    try:
        await led.propose_outcome(FK, "kilabz", "wontfix", principal_role="labeler", play="p")
        raise AssertionError("propose_outcome accepted a non-{real,fp} verdict")
    except ValueError:
        pass


async def test_idempotency_and_cross_source_no_shadow(led):
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
    # same proposal twice = one row (same 5-col tuple)
    await led.propose_outcome(FK, "kilabz", "real", principal_role="labeler", play="p1")
    r2 = await led.propose_outcome(FK, "kilabz", "real", principal_role="labeler", play="p1")
    assert r2["written"] == 0
    # a panel row and a human row for the SAME finding coexist (outcome_source is in the tuple), so
    # a machine event can never shadow / block a human promotion
    await led.confirm_outcome(FK, "kilabz", "real", principal_role="human")
    n_panel = await _val(led,
        "SELECT count(*) FROM finding_outcome WHERE finding_key=$1 AND outcome_source='panel_proposed'", FK)
    n_human = await _val(led,
        "SELECT count(*) FROM finding_outcome WHERE finding_key=$1 AND outcome_source='human_confirm'", FK)
    assert n_panel == 1 and n_human == 1, "panel and human rows must coexist (no cross-source collision)"
    assert (await _promoted_real(led)) == 1, "the human promotion must land, unshadowed"


async def test_verbs_require_an_existing_finding(led):
    await _truncate(led)
    r = await led.record_exec_prior(FK2, "kilabz", principal_role="exec_oracle", tip_sha="c" * 40)
    assert r.get("error") == "no such finding"
    r = await led.propose_outcome(FK2, "kilabz", "real", principal_role="labeler", play="p")
    assert r.get("error") == "no such finding"
    r = await led.confirm_outcome(FK2[:16], "kilabz", "real", principal_role="human")
    assert r.get("error") == "no finding matches that prefix"


async def test_human_dismiss_is_also_gated(led):
    # kilabz code-review BLOCKER: the pre-existing human_dismiss writes a human-terminal (gating +
    # queue-terminal) row, so it takes the SAME principal gate as confirm_outcome — not a fourth
    # unguarded human-label writer.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
    for role in ("labeler", "exec_oracle", "reviewer", ""):
        try:
            await led.human_dismiss(FK[:16], "kilabz", "fp", principal_role=role)
            raise AssertionError(f"human_dismiss allowed non-human role {role!r}")
        except PermissionError:
            pass
    r = await led.human_dismiss(FK[:16], "kilabz", "fp", principal_role="human")
    assert r["dismissed"] == 1 and not await _in_queue(led)


async def test_pair_check_rejects_illegal_source_outcome(led):
    # kilabz code-review HIGH: the DB pair-CHECK backs up the verb matrix so even a RAW insert can't
    # forge an illegal (source, outcome) that the trusting views would mishandle — e.g. a MACHINE
    # source with a lifecycle (expired) or gating (confirmed_real/dismissed_*) outcome.
    await _truncate(led)
    async with led._pool.acquire() as con:
        for src, outc in (("panel_proposed", "expired"),          # machine -> tombstone (queue)
                          ("review_raised", "confirmed_real"),    # machine -> gating
                          ("exec_verified", "dismissed_false_positive"),
                          ("panel_proposed", "open"),
                          ("human_confirm", "panel_real")):
            raised = False
            try:
                await _seed(con, source=src, outcome=outc, source_event=f"bad:{src}:{outc}")
            except asyncpg.CheckViolationError:
                raised = True
            assert raised, f"pair CHECK let illegal ({src!r},{outc!r}) through"


async def test_ttl_expires_a_machine_current_unlabeled_finding(led):
    # kilabz code-review MEDIUM: a machine label becomes latest-by-seq in finding_current, so an
    # 'open'-only sweep would MISS an unlabeled finding and strand it in the queue forever. The queue
    # sweep (age = raise-time) still tombstones it.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
        await con.execute(
            "UPDATE finding_outcome SET created_at = now() - interval '99 days' WHERE finding_key=$1", FK)
    await led.propose_outcome(FK, "kilabz", "real", principal_role="labeler", play="p1")
    assert await _in_queue(led), "an unlabeled finding stays in the queue after a machine label"
    n = await led.expire_open(30)
    assert n == 1, "TTL must tombstone a machine-CURRENT unlabeled finding (not just an 'open' one)"
    assert not await _in_queue(led)


async def test_consumer_proof_no_code_reads_the_bare_gating_name(led):
    # both families BLOCKER: make the unsafe path hard to use, not just add a safe one. No live .py
    # reads the bare `finding_precision` (the renamed-away all-source metric) — a reader must pick
    # `_raw` (diagnostic) or `_promoted` (gating) EXPLICITLY. (SQL migrations/schema define the views;
    # this checks CODE consumers only.)
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "runtime"
    bare = re.compile(r"finding_precision(?![_a-z])")   # 'finding_precision' NOT followed by _raw/_promoted
    offenders = []
    for py in src.rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if bare.search(line):
                offenders.append(f"{py.name}:{i}: {line.strip()[:80]}")
    assert not offenders, "code reads the bare gating-suggestive name finding_precision:\n" + "\n".join(offenders)


# ---- label-throughput PR-A (docs/label-throughput-design.md §8) --------------------------------

async def test_confirm_real_lands_in_promoted_and_drains_queue(led):
    # the whole point of PR-A: `real` finally has a caller, the gating numerator can move.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
    res = await led.confirm_outcome(FK[:12], "all", "real", principal_role="admin")
    assert res["written"] == 1
    assert (await _promoted_real(led)) == 1, "confirmed_real must land in the PROMOTED numerator"
    assert not await _in_queue(led), "a human label is queue-terminal"


async def test_real_after_applied_fixed_writes(led):
    # §2a any-state: post-hoc human truth on an auto-closed finding is exactly what promoted wants.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
        await _seed(con, outcome="applied_fixed", source="auto_fix_landed",
                    source_event="review:close1")
    res = await led.confirm_outcome(FK[:12], "all", "real", principal_role="admin")
    assert res["written"] == 1, "confirm-real must write on an auto-closed (applied_fixed) finding"
    assert (await _promoted_real(led)) == 1


async def test_fp_after_expired_writes_without_resurrect(led):
    # §2a any-state: truth recorded on an expired finding; queue membership UNCHANGED.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
        await _seed(con, outcome="expired", source="ttl_sweep", source_event="sweep:x")
    assert not await _in_queue(led)
    res = await led.confirm_outcome(FK[:12], "all", "fp", principal_role="admin")
    assert res["written"] == 1, "fp must write on an expired finding (truth is truth)"
    assert not await _in_queue(led), "labeling an expired finding must not resurrect it"
    n = await _val(led, "SELECT dismissed_false_positive FROM finding_precision_promoted "
                        "WHERE rule_tag='fail-open' AND reviewer_family='kilabz'")
    assert n == 1, "the fp lands in promoted"


async def test_all_scope_writes_only_raising_family(led):
    # §2a "all" semantics: a key raised by ONE family writes exactly 1 row — never labels unseen.
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)                                     # kilabz only
    res = await led.confirm_outcome(FK[:12], "all", "real", principal_role="admin")
    assert res["written"] == 1, "'all' must skip the family that never raised the finding"


async def test_cli_kind_maps_to_exact_pair(led):
    # §2a table: each kind mints EXACTLY its pair (the no-schema-change proof, asserted).
    table = {"real": ("human_confirm", "confirmed_real"),
             "fp": ("human_dismiss", "dismissed_false_positive"),
             "wontfix": ("human_dismiss", "dismissed_wontfix")}
    for kind, (src, outc) in table.items():
        await _truncate(led)
        async with led._pool.acquire() as con:
            await _seed(con)
        await led.confirm_outcome(FK[:12], "all", kind, principal_role="admin")
        n = await _val(led, "SELECT count(*) FROM finding_outcome WHERE finding_key=$1 "
                            "AND outcome=$2 AND outcome_source=$3", FK, outc, src)
        assert n == 1, f"kind {kind!r} must mint exactly ({src},{outc})"


async def test_label_batch_mixed_dedup_and_idempotent(led):
    # §2b: per-key fail-closed, batch fail-open, duplicates deduped, four-count summary exact.
    from runtime import outcomerecord as orec
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
        await _seed(con, finding_key=FK2, path="src/y.py", source_event="review:second")
    s = await orec.label_batch(led, "real", [FK[:12], "zz-not-hex!!", FK2[:12], FK[:12], "abc"])
    assert s == {"labeled": 2, "rows": 2, "refused": 2, "duplicates": 1}, s
    s2 = await orec.label_batch(led, "real", [FK[:12], FK2[:12]])       # re-run: idempotent
    assert s2["labeled"] == 2 and s2["rows"] == 0, s2


async def test_label_batch_cap_refuses_whole_invocation(led):
    from runtime import outcomerecord as orec
    s = await orec.label_batch(led, "real", [f"{i:012x}" for i in range(201)])
    assert s["labeled"] == 0 and s["rows"] == 0 and s["refused"] == 201, s


async def test_label_queue_shows_auto_closed_and_keys_roundtrip(led):
    # §2c: the browser includes applied_fixed findings (latest-RAISE join, not latest-open),
    # emits 12-hex keys, and those keys round-trip VERBATIM into the batch verb.
    from runtime import outcomerecord as orec
    await _truncate(led)
    fk3 = "c" * 64
    async with led._pool.acquire() as con:
        await _seed(con)                                                 # open
        await _seed(con, finding_key=fk3, path="src/z.py", source_event="review:r2")
        await _seed(con, finding_key=fk3, path="src/z.py", outcome="applied_fixed",
                    source="auto_fix_landed", source_event="review:close2")
    rows = await led.label_queue()
    keys = {r["key12"] for r in rows}
    assert FK[:12] in keys and fk3[:12] in keys, "auto-closed finding must stay visible"
    assert all(len(k) == 12 and all(c in "0123456789abcdef" for c in k) for k in keys)
    txt = orec.format_label_queue(rows)
    assert FK[:12] in txt and "fail-open" in txt, "formatter surfaces keys + cluster tag"
    s = await orec.label_batch(led, "wontfix", sorted(keys))            # verbatim round-trip
    assert s["labeled"] == 2 and s["refused"] == 0, s
    assert await led.label_queue() == [], "human labels drain the queue"


async def test_fp_to_real_correction_consistent_in_raw_current(led):
    # code-gate MED (migration 0012): after an fp -> real correction the RAW finding_current must
    # show the latest human word, not the stale fp (finding_current_human already did).
    await _truncate(led)
    async with led._pool.acquire() as con:
        await _seed(con)
    await led.confirm_outcome(FK[:12], "all", "fp", principal_role="admin")
    await led.confirm_outcome(FK[:12], "all", "real", principal_role="admin")
    cur = await _val(led, "SELECT outcome FROM finding_current WHERE finding_key=$1", FK)
    assert cur == "confirmed_real", "finding_current must show the LATEST human word after a correction"


async def test_confirmed_real_sticky_no_reraise_and_outranks_machine(led):
    # code-gate MED: a human-confirmed finding must (a) outrank any LATER machine row in
    # finding_current, and (b) never be re-raised by a re-detection (no keys-file re-ask).
    await _truncate(led)
    of = [{"tag": "fail-open", "path": "src/x.py", "line_hash": "lh-1", "reviewer_family": "kilabz"}]
    r1 = await led.record_findings("r", "refs/heads/main", "d" * 40, "p1", ["src/x.py"], of, None)
    assert r1["opened"] == 1
    fk = await _val(led, "SELECT finding_key FROM finding_current WHERE reviewer_family='kilabz'")
    await led.confirm_outcome(fk[:12], "all", "real", principal_role="admin")
    cur = await _val(led, "SELECT outcome FROM finding_current WHERE finding_key=$1", fk)
    assert cur == "confirmed_real", "the human row outranks machine rows in the raw current view"
    r2 = await led.record_findings("r", "refs/heads/main", "e" * 40, "p2", ["src/x.py"], of, None)
    assert r2["opened"] == 0 and r2["opened_rows"] == [], \
        "a re-detection of a human-confirmed finding must not re-open or resurface it"
    assert r2["skipped_dismissed"] == 1, "the skip is counted"


async def main():
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    await led.migrate()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    passed = 0
    try:
        for t in tests:
            await t(led)
            print("PASS", t.__name__)
            passed += 1
    finally:
        await led.close()
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(main())
