"""PostgresLedger concurrency proofs - the production ledger under REAL contention.

This is where the design's thesis is VERIFIED, not asserted: row-locking + a
status-guarded state machine end the file-IPC bug classes. Each test races real
asyncpg connections against a real local Postgres. The naive (no-lock) version of
each verb would FAIL the matching test - that's the regression guard.

Setup (once):
    brew services start postgresql@16
    createdb runtime_test
Run:
    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
        PYTHONPATH=src python3 tests/test_postgres_ledger.py
"""
import asyncio
import datetime as _dt
import inspect
import os
import uuid as _uuid

import asyncpg

from runtime.contracts import ErrorClass, Result, ResultStatus, TransportEnvelope
from runtime.ledger.postgres_store import LostLease, PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")

# real roster agents drive the authority paths:
#   kilabz  -> RESPONDER       (retry-safe; requeues)
#   mack    -> WORKSPACE_ACTOR (never auto-retried)
#   lobster -> CONTROLLER      (retry-safe)


def _env(dedupe: str) -> TransportEnvelope:
    return TransportEnvelope(transport="terminal", account="acct", sender_id="user1",
                             reply_target="terminal:demo", dedupe_key=dedupe)


def _ok(text="done", artifact=None) -> Result:
    return Result(status=ResultStatus.OK, text=text, artifact_ref=artifact)


def _retryable() -> Result:
    return Result(status=ResultStatus.ERROR, text="boom", error_class=ErrorClass.RETRYABLE)


async def _truncate(led: PostgresLedger) -> None:
    async with led._pool.acquire() as con:
        await con.execute(
            "TRUNCATE inbound_event, job, attempt, attempt_log, outbound, dead_letter, "
            "repo_concurrency, review_cursor, automerge_seen RESTART IDENTITY CASCADE")


async def _expire_lease(led: PostgresLedger, attempt_id) -> None:
    async with led._pool.acquire() as con:
        await con.execute(
            "UPDATE attempt SET lease_expires_at = statement_timestamp() - interval '1 second' "
            "WHERE id = $1", attempt_id)


async def _age_cursor(led: PostgresLedger, repo_id, ref) -> None:
    """Push a cursor's updated_at an hour into the past so a stale-pending re-claim fires."""
    async with led._pool.acquire() as con:
        await con.execute(
            "UPDATE review_cursor SET updated_at = now() - interval '1 hour' "
            "WHERE repo_id = $1 AND ref = $2", repo_id, ref)


# -- invariant 1: no double-lease ----------------------------------------------
async def test_no_double_lease(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    results = await asyncio.gather(*[led.lease_job(f"w{i}", []) for i in range(50)])
    leased = [r for r in results if r is not None]
    assert len(leased) == 1, f"expected exactly 1 lease, got {len(leased)}"
    async with led._pool.acquire() as con:
        n = await con.fetchval(
            "SELECT count(*) FROM attempt WHERE job_id=$1 AND status='open'", jid)
    assert n == 1, f"expected 1 open attempt, got {n}"


# -- invariant 2: reclaim vs complete are mutually exclusive (the crown jewel) --
async def test_reclaim_vs_complete(led: PostgresLedger) -> None:
    # (A) 200 concurrent races: under real contention EXACTLY one writer acts on
    # the attempt and the job ends in a single consistent state (mutual exclusion).
    for trial in range(200):
        await _truncate(led)
        jid = await led.submit_job(to_agent="kilabz", prompt="hi")  # responder -> requeues on reclaim
        att = await led.lease_job("w1", [])
        await _expire_lease(led, att)

        async def _complete():
            try:
                await led.complete_attempt(att, _ok())
                return "completed"
            except LostLease:
                return "lost"

        reclaimed, outcome = await asyncio.gather(led.reclaim_expired(), _complete())
        st = await led.get_status(jid)
        assert (outcome == "completed" and reclaimed == 0) or \
               (outcome == "lost" and reclaimed == 1), \
            f"trial {trial}: outcome={outcome} reclaimed={reclaimed} (not mutually exclusive)"
        assert st["status"] == ("done" if outcome == "completed" else "queued"), \
            f"trial {trial}: outcome={outcome} but job={st['status']}"
        async with led._pool.acquire() as con:
            opn = await con.fetchval(
                "SELECT count(*) FROM attempt WHERE job_id=$1 AND status='open'", jid)
        assert opn <= 1, f"trial {trial}: {opn} open attempts (double state)"

    # (B) both outcomes proven DETERMINISTICALLY (timing-independent coverage):
    # complete-wins: a worker finishing after expiry but before the janitor runs still wins
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    att = await led.lease_job("w1", [])
    await _expire_lease(led, att)
    await led.complete_attempt(att, _ok())
    assert (await led.get_status(jid))["status"] == "done"
    assert await led.reclaim_expired() == 0, "janitor must find nothing once the job is done"

    # reclaim-wins: the janitor reclaims first; the late complete loses (LostLease)
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    att = await led.lease_job("w1", [])
    await _expire_lease(led, att)
    assert await led.reclaim_expired() == 1
    lost = False
    try:
        await led.complete_attempt(att, _ok())
    except LostLease:
        lost = True
    assert lost, "a complete after reclaim must raise LostLease"
    assert (await led.get_status(jid))["status"] == "queued"


# -- invariant 3: ingest dedupe is exactly-once --------------------------------
async def test_ingest_dedupe(led: PostgresLedger) -> None:
    await _truncate(led)
    env = _env("dupe-key-1")
    ids = await asyncio.gather(*[led.ingest_inbound(env, "body") for _ in range(20)])
    assert len({str(i) for i in ids}) == 1, f"expected 1 unique id, got {len(set(ids))}"
    async with led._pool.acquire() as con:
        n = await con.fetchval(
            "SELECT count(*) FROM inbound_event WHERE dedupe_key=$1", "dupe-key-1")
    assert n == 1, f"expected 1 inbound_event, got {n}"


# -- 2026-09-20 regression: a surrogate byte in an UNTRUSTED diff must NOT abort
# ingest. Before the _utf8_safe belt, asyncpg raised DataError at bind ('utf-8'
# codec can't encode '\udcXX': surrogates not allowed) and the WHOLE review died
# at ingest — 3 FieldVision reviews stranded before the reviewer ever ran. The
# reviewer must still receive a reviewable body with the one bad byte -> U+FFFD.
async def test_ingest_repairs_surrogate_body(led: PostgresLedger) -> None:
    await _truncate(led)
    # every str a strict-UTF-8 codec / PG 'text' column rejects at bind. surrogateescape
    # covers only U+DC80–U+DCFF, so high/low-non-DC8x surrogates + NUL are the cases a
    # naive belt misses (cross-family review P1/P2) — assert ALL of them survive ingest.
    poisons = {
        "low-DC8x":  "\udce2",          # byte-smuggle (the original prod abort)
        "high-surr": "\ud800",          # surrogateescape can't encode -> would re-raise
        "low-DC00":  "\udc00",          # below DC80, also outside surrogateescape
        "nul":       "\x00",            # valid UTF-8, slips encode, PG rejects it
        "mixed":     chr(0xd834) + chr(0xdd1e),  # lone surrogates, outside surrogateescape
    }
    for i, (name, ch) in enumerate(poisons.items()):
        body = "OBJECTIVE: review " + ch + " tail"
        ev = await led.ingest_inbound(_env(f"surr-{i}"), body)     # must NOT raise
        async with led._pool.acquire() as con:
            stored = await con.fetchval("SELECT body FROM inbound_event WHERE id=$1", ev)
        assert "�" in stored, f"{name}: poison must degrade to U+FFFD"
        assert stored.startswith("OBJECTIVE: review ") and stored.endswith(" tail"), \
            f"{name}: surrounding text must be kept"
        assert "\x00" not in stored and not any(0xd800 <= ord(c) <= 0xdfff for c in stored), \
            f"{name}: stored value must be strictly storable"
    # submit_job carries the same untrusted body via prompt -> same belt (surrogate + NUL)
    ev2 = await led.ingest_inbound(_env("surr-submit"), "clean")
    jid = await led.submit_job(to_agent="kilabz", prompt="p\udce2\x00q", inbound_event_id=ev2)
    async with led._pool.acquire() as con:
        jbody = await con.fetchval("SELECT body FROM job WHERE id=$1", jid)
    assert "�" in jbody and "\x00" not in jbody, "submit_job prompt must be repaired too"


# -- invariant 4a: each outbound row is claimed by at most one sender -----------
async def test_outbound_single_claim(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("ob-1"), "hi")
    jid = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    for k in range(3):
        await led.enqueue_outbound(jid, f"reply {k}")
    claims = await asyncio.gather(*[led.claim_outbound("terminal") for _ in range(20)])
    got = [c for c in claims if c is not None]
    assert len(got) == 3, f"expected 3 claims, got {len(got)}"
    assert len({c["id"] for c in got}) == 3, "a row was claimed by two senders"


# -- invariant 4b: a send is recorded exactly once (no double delivery) ---------
async def test_outbound_send_exactly_once(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("ob-2"), "hi")
    jid = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    ob = await led.enqueue_outbound(jid, "reply")
    claimed = await led.claim_outbound("terminal")
    await led.mark_outbound_sent(claimed["id"], "provider-xyz")
    await led.mark_outbound_sent(claimed["id"], "provider-xyz")  # duplicate: must not raise/re-deliver
    async with led._pool.acquire() as con:
        st = await con.fetchval("SELECT status FROM outbound WHERE id=$1", ob)
        n = await con.fetchval(
            "SELECT count(*) FROM outbound WHERE provider_msg_id=$1", "provider-xyz")
    assert st == "sent", f"outbound status={st}"
    assert n == 1, f"provider_msg_id appears {n} times (double delivery)"


# -- invariant 4c: inline (sync) delivery tombstones pending WITHOUT a lease ----
# phone r7 #1: the CLI's sync-reply print IS the delivery, but it used to call the
# transport verb mark_outbound_sent, whose leased-only CAS silently no-opped — the row
# sat pending forever (latent duplicate delivery the day a cli sender ships).
async def test_outbound_inline_sent_tombstones_pending(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("ob-3"), "hi")
    jid = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    ob = await led.enqueue_outbound(jid, "reply")
    n = await led.mark_outbound_sent_inline(ob, f"cli-{ob}")
    assert n == 1, f"inline verb must report the transition (got {n})"   # r8 P3
    async with led._pool.acquire() as con:
        st = await con.fetchval("SELECT status FROM outbound WHERE id=$1", ob)
    assert st == "sent", f"inline tombstone missed: status={st}"
    assert await led.claim_outbound("terminal") is None, \
        "tombstoned row must be invisible to senders"

    # and the inline verb must NOT touch a row a transport already LEASED — delivery
    # belongs to the sender; the inline call is a benign no-op there
    ob2 = await led.enqueue_outbound(jid, "reply-2")
    claimed = await led.claim_outbound("terminal")   # rows inherit the envelope's transport
    assert claimed is not None and str(claimed["id"]) == str(ob2)
    n2 = await led.mark_outbound_sent_inline(ob2, f"cli-{ob2}")
    assert n2 == 0, f"leased-race must report 0 so the caller skips its print (got {n2})"  # r8 P3
    async with led._pool.acquire() as con:
        st2 = await con.fetchval("SELECT status FROM outbound WHERE id=$1", ob2)
    assert st2 == "leased", f"inline verb must no-op on a leased row, got {st2}"


# -- invariant 5: a workspace_actor is NEVER auto-retried ----------------------
async def test_workspace_actor_never_requeued(led: PostgresLedger) -> None:
    for trial in range(50):
        await _truncate(led)
        jid = await led.submit_job(to_agent="mack", prompt="edit code")  # workspace_actor
        att = await led.lease_job("w1", [])
        await _expire_lease(led, att)

        async def _fail():
            try:
                await led.fail_attempt(att, _retryable())  # retryable, but agent is unsafe
            except LostLease:
                pass

        await asyncio.gather(_fail(), led.reclaim_expired())
        st = await led.get_status(jid)
        assert st["status"] in ("failed", "dead"), \
            f"trial {trial}: workspace_actor was requeued! status={st['status']}"
        async with led._pool.acquire() as con:
            natt = await con.fetchval("SELECT count(*) FROM attempt WHERE job_id=$1", jid)
        assert natt == 1, f"trial {trial}: {natt} attempts (an unsafe retry happened)"


# -- invariant 6: heartbeat cannot resurrect a reclaimed attempt ---------------
async def test_heartbeat_lost_lease(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    att = await led.lease_job("w1", [])
    await _expire_lease(led, att)
    assert await led.reclaim_expired() == 1
    raised = False
    try:
        await led.heartbeat_attempt(att)
    except LostLease:
        raised = True
    assert raised, "heartbeat on a reclaimed attempt must raise LostLease"
    async with led._pool.acquire() as con:
        stt = await con.fetchval("SELECT status FROM attempt WHERE id=$1", att)
    assert stt == "failed", f"reclaimed attempt status={stt}"


# -- invariant 7: admission limits hold under concurrent submit ----------------
async def test_admission_limits_concurrent(led: PostgresLedger) -> None:
    await _truncate(led)
    parent = await led.submit_job(to_agent="lobster", prompt="root")
    await asyncio.gather(*[
        led.submit_job(to_agent="kilabz", prompt=f"c{i}", parent_id=parent)
        for i in range(50)])
    async with led._pool.acquire() as con:
        live = await con.fetchval(
            "SELECT count(*) FROM job WHERE parent_id=$1 AND status<>'dead'", parent)
        dead = await con.fetchval(
            "SELECT count(*) FROM job WHERE parent_id=$1 AND status='dead'", parent)
    assert live == led.MAX_CHILDREN, f"admission overshoot/undershoot: {live} live (max {led.MAX_CHILDREN})"
    assert live + dead == 50, f"expected 50 children, got {live} live + {dead} dead"


# -- responder DOES requeue on a retryable failure -----------------------------
async def test_responder_requeues_on_retryable(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")  # responder
    att = await led.lease_job("w1", [])
    await led.fail_attempt(att, _retryable())
    st = await led.get_status(jid)
    assert st["status"] == "queued", f"responder retryable should requeue, got {st['status']}"
    att2 = await led.lease_job("w2", [])
    assert att2 is not None, "requeued job should be re-leasable"


# -- bounded reclaim: poison job stops requeuing after MAX_ATTEMPTS (PR-1b) -----
async def test_poison_responder_dead_letters_after_max_attempts(led: PostgresLedger) -> None:
    """A responder that fails retryably EVERY attempt requeues up to MAX_ATTEMPTS-1 times,
    then the Nth failure dead-letters instead of looping forever. The count includes the
    just-closed attempt, so EXACTLY MAX_ATTEMPTS runs happen (off-by-one is intentional)."""
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="poison")   # responder, requeue-safe
    for i in range(1, led.MAX_ATTEMPTS + 1):
        att = await led.lease_job(f"w{i}", [])
        assert att is not None, f"attempt {i} should lease (under the cap)"
        await led.fail_attempt(att, _retryable())
        st = await led.get_status(jid)
        if i < led.MAX_ATTEMPTS:
            assert st["status"] == "queued", f"attempt {i}: under cap -> requeue, got {st['status']}"
        else:
            assert st["status"] == "dead", f"attempt {i}: at cap -> dead, got {st['status']}"
    assert await led.lease_job("w-final", []) is None, "dead job must not re-lease"
    async with led._pool.acquire() as con:
        nd = await con.fetchval(
            "SELECT count(*) FROM dead_letter WHERE source_id=$1 AND reason LIKE 'poison:%'", jid)
        natt = await con.fetchval("SELECT count(*) FROM attempt WHERE job_id=$1", jid)
    assert nd == 1, "exactly one poison dead_letter recorded"
    assert natt == led.MAX_ATTEMPTS, f"exactly MAX_ATTEMPTS runs, got {natt}"


async def test_poison_reclaim_dead_letters_after_max_attempts(led: PostgresLedger) -> None:
    """A responder whose worker keeps crashing (lease expires, never completes) is
    reclaimed->requeued up to the cap, then dead-lettered by reclaim_expired itself —
    closing the crash-loop that the requeue path alone never terminates."""
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="crash-loop")
    for i in range(1, led.MAX_ATTEMPTS + 1):
        att = await led.lease_job(f"w{i}", [])
        assert att is not None, f"attempt {i} should lease"
        await _expire_lease(led, att)
        assert await led.reclaim_expired() == 1, f"attempt {i} should be reclaimed"
        st = await led.get_status(jid)
        if i < led.MAX_ATTEMPTS:
            assert st["status"] == "queued", f"attempt {i}: under cap -> requeue"
        else:
            assert st["status"] == "dead", f"attempt {i}: at cap -> dead"
    async with led._pool.acquire() as con:
        nd = await con.fetchval(
            "SELECT count(*) FROM dead_letter WHERE source_id=$1 AND reason LIKE 'poison:%'", jid)
    assert nd == 1, "reclaim recorded exactly one poison dead_letter"


# -- worktree GC: reapable set excludes live + just-closed leases (PR-1c) -------
async def test_reapable_attempt_ids_respects_grace_window(led: PostgresLedger) -> None:
    """The worktree GC must never reap a live (open) attempt nor one closed within the
    grace window — only attempts CLOSED at least min_age_s ago are reapable. This is what
    keeps a sweep from deleting a worktree whose worker may still be writing."""
    await _truncate(led)
    # (a) an OPEN attempt — a live lease — is never reapable
    await led.submit_job(to_agent="kilabz", prompt="open")
    a_open = await led.lease_job("w-open", [])
    # (b) a CLOSED attempt, just now (ended_at = now) — excluded by the grace window
    await led.submit_job(to_agent="kilabz", prompt="closed")
    a_closed = await led.lease_job("w-closed", [])
    await led.fail_attempt(a_closed, _retryable())     # closed (failed), ended_at = now
    assert await led.reapable_attempt_ids(3600.0) == set(), "open + just-closed excluded"
    # backdate the closed attempt past the grace window -> now reapable; open stays out
    async with led._pool.acquire() as con:
        await con.execute(
            "UPDATE attempt SET ended_at = statement_timestamp() - interval '2 hours' "
            "WHERE id = $1", a_closed)
    reapable = await led.reapable_attempt_ids(3600.0)
    assert str(a_closed) in reapable, "an attempt closed past the grace window is reapable"
    assert str(a_open) not in reapable, "an open lease is never reapable"


# -- the DB itself rejects an illegal status (CHECK enforcement) ---------------
async def test_db_rejects_illegal_status(led: PostgresLedger) -> None:
    await _truncate(led)
    jid = await led.submit_job(to_agent="kilabz", prompt="hi")
    raised = False
    try:
        async with led._pool.acquire() as con:
            await con.execute("UPDATE job SET status='bogus' WHERE id=$1", jid)
    except asyncpg.CheckViolationError:
        raised = True
    assert raised, "DB must reject an illegal status via CHECK constraint"


# -- happy path: every verb once, end to end ----------------------------------
async def test_happy_path_all_verbs(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("hp-1"), "hello")
    jid = await led.submit_job(to_agent="kilabz", prompt="do it", inbound_event_id=ev)
    att = await led.lease_job("w1", [])
    assert att is not None
    job = await led.get_attempt_job(att)
    assert job.prompt == "do it"
    await led.heartbeat_attempt(att)
    await led.append_log(att, "stdout", "working...")
    await led.complete_attempt(att, _ok(text="result", artifact="patch.diff"))
    # complete auto-queued the reply (transactional outbox); claim + deliver it
    claimed = await led.claim_outbound("terminal")
    assert claimed is not None and claimed["body"] == "result"
    await led.mark_outbound_sent(claimed["id"], "msg-1")
    st = await led.get_status(jid)
    assert st["status"] == "done", f"job status={st['status']}"
    assert st["artifact_ref"] == "patch.diff"
    assert any(o["status"] == "sent" for o in (st["outbound"] or [])), "reply not sent"


async def test_context_round_trips(led: PostgresLedger) -> None:
    """Job.context persists through submit -> jsonb -> lease -> get_attempt_job (the
    plumbing the higgsfield runner relies on to read job.context['image_url'])."""
    await _truncate(led)
    ctx = {"image_url": "http://example.com/cat.png", "application": "/higgsfield-ai/dop/lite"}
    await led.submit_job(to_agent="higgsfield", prompt="gen", context=ctx)
    att = await led.lease_job("w1", [])
    job = await led.get_attempt_job(att)
    assert job is not None and job.context == ctx, f"context lost: {job.context if job else None}"

    # omitted context -> the jsonb DEFAULT '{}' -> an empty dict (never None), so the
    # runner's job.context.get("image_url") is a clean None, not an AttributeError.
    await _truncate(led)
    await led.submit_job(to_agent="kilabz", prompt="no ctx")
    att2 = await led.lease_job("w1", [])
    job2 = await led.get_attempt_job(att2)
    assert job2 is not None and job2.context == {}


# -- regression: serve must auto-heal a stale schema on startup ----------------
# Named test_zz_* so it runs LAST: it drops + restores job.context, so a failure
# mid-way can't strand the column for the tests after it.
async def test_zz_migrate_heals_stale_schema(led: PostgresLedger) -> None:
    """migrate() re-applies migrations/*.sql idempotently. Simulate an OLD DB missing
    job.context (the column the higgsfield deploy added), run migrate(), and the column
    is restored — exactly what serve() now does on startup so a restart can never run
    ahead of the schema (the 2026-06-24 dispatch outage)."""
    async def _has_context() -> int:
        async with led._pool.acquire() as con:
            return await con.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='job' AND column_name='context'")

    async with led._pool.acquire() as con:
        await con.execute("ALTER TABLE job DROP COLUMN IF EXISTS context")
    assert await _has_context() == 0, "precondition: context column dropped"

    applied = await led.migrate()
    assert "0001_add_job_context.sql" in applied, f"0001 not applied: {applied}"
    assert await _has_context() == 1, "migrate() should restore job.context"

    # idempotent: a second run against the now-current schema must be a clean no-op
    await led.migrate()
    assert await _has_context() == 1


# -- regression: 0015 — outbound.created_at + its head-check index -------------
# Named test_zz_* to run last: it drops outbound.created_at (and the index that
# rides on it) mid-flight; migrate() restores both, so later tests never see the
# hole. The index is also the substrate migration_head object — reconcile's
# health gate probes it via to_regclass, so its absence after migrate() would
# mean every deploy false-fails the gate.
async def test_zz_migrate_restores_outbound_created_at(led: PostgresLedger) -> None:
    async def _col() -> int:
        async with led._pool.acquire() as con:
            return await con.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='outbound' AND column_name='created_at'")

    async def _idx() -> bool:
        async with led._pool.acquire() as con:
            return await con.fetchval(
                "SELECT to_regclass('public.outbound_created_at_idx') IS NOT NULL")

    async with led._pool.acquire() as con:
        await con.execute("ALTER TABLE outbound DROP COLUMN IF EXISTS created_at CASCADE")
    assert await _col() == 0, "precondition: created_at dropped"
    assert not await _idx(), "precondition: index dropped with its column"

    applied = await led.migrate()
    assert "0015_outbound_created_at.sql" in applied, f"0015 not applied: {applied}"
    assert await _col() == 1, "migrate() should restore outbound.created_at"
    assert await _idx(), "migrate() should restore outbound_created_at_idx (the head-check object)"

    # idempotent: second run is a clean no-op
    await led.migrate()
    assert await _col() == 1 and await _idx()


# -- regression: 0016 — knowledge_scope_gen creation + fence continuity ---------
# The walk fence is now gen + MAX(seq) (no-op syncs must move it — review 20260910200253
# P2; the sum keeps pre-0016 row-writers visible through a mixed-deploy window). Migrating
# an old DB must create the table; with gen starting at 0 the fence VALUE stays numerically
# continuous with the old MAX(seq) fence; re-runs must never clobber a live counter.
async def test_zz_migrate_creates_knowledge_scope_gen(led: PostgresLedger) -> None:
    scope = "genseed"
    await led.knowledge_sync(scope, [{
        "path": "a.md", "title": "a", "tags": "", "doc_date": None,
        "body": "seed body", "content_sha": "s1", "lossy": False}])
    async with led._pool.acquire() as con:
        max_seq = await con.fetchval(
            "SELECT MAX(seq) FROM knowledge_doc WHERE scope=$1", scope)
        # simulate a pre-0016 DB: doc rows exist, the gen table does not
        await con.execute("DROP TABLE knowledge_scope_gen")

    applied = await led.migrate()
    assert "0016_knowledge_scope_gen.sql" in applied, f"0016 not applied: {applied}"
    # continuity: no gen row yet, so the fence equals the old MAX(seq) fence — a walker that
    # stamped under pre-0016 semantics with no interleaving commit still compares equal
    fence = await led.knowledge_fence(scope)
    assert fence == max_seq, f"fence must stay continuous with MAX(seq): {fence} != {max_seq}"

    # a later accepted TRUE no-op (same doc, unchanged — zero rows written) bumps gen
    # (fence +1); a migrate() re-run must NOT reset it
    await led.knowledge_sync(scope, [{
        "path": "a.md", "title": "a", "tags": "", "doc_date": None,
        "body": "seed body", "content_sha": "s1", "lossy": False}])
    fence2 = await led.knowledge_fence(scope)
    assert fence2 == fence + 1, f"accepted no-op must move the fence: {fence2} != {fence}+1"
    await led.migrate()
    fence3 = await led.knowledge_fence(scope)
    assert fence3 == fence2, f"re-migrate moved the fence: {fence3} != {fence2}"


# -- regression: cancel must NOT deadlock against complete/fail (the P0) --------
# Before the lock-order fix this failed ~99% of trials with DeadlockDetectedError;
# it is the test the green suite was missing (cancel had zero coverage).
async def test_cancel_vs_finish_no_deadlock(led: PostgresLedger) -> None:
    for trial in range(150):
        await _truncate(led)
        jid = await led.submit_job(to_agent="kilabz", prompt="hi")
        att = await led.lease_job("w1", [])
        errors: list = []

        async def _cancel():
            try:
                await led.cancel(jid)
            except Exception as e:  # any DB error here is a failure
                errors.append(("cancel", type(e).__name__))

        async def _finish():
            try:
                if trial % 2 == 0:
                    await led.complete_attempt(att, _ok())
                else:
                    await led.fail_attempt(att, _retryable())
            except LostLease:
                pass  # expected when cancel won the race
            except Exception as e:
                errors.append(("finish", type(e).__name__))

        await asyncio.gather(_cancel(), _finish())
        assert not errors, f"trial {trial}: unhandled DB error(s) {errors}"
        st = await led.get_status(jid)
        assert st["status"] in ("dead", "done"), f"trial {trial}: job ended {st['status']}"
        async with led._pool.acquire() as con:
            opn = await con.fetchval(
                "SELECT count(*) FROM attempt WHERE job_id=$1 AND status='open'", jid)
        assert opn == 0, f"trial {trial}: stuck open attempt after cancel race"


# -- regression: no live child under a terminal (cancelled) parent -------------
async def test_no_live_child_under_terminal_parent(led: PostgresLedger) -> None:
    await _truncate(led)
    parent = await led.submit_job(to_agent="lobster", prompt="root")
    await led.cancel(parent)  # parent -> dead
    child = await led.submit_job(to_agent="kilabz", prompt="child", parent_id=parent)
    st = await led.get_status(child)
    assert st["status"] == "dead", \
        f"child under a cancelled parent must be dead, got {st['status']}"
    assert await led.lease_job("w1", []) is None, "a dead child must not be leasable"


# -- regression: a cross-row provider_msg_id collision is dead-lettered ---------
# (this actually exercises the savepoint branch the prior test only claimed to)
async def test_outbound_duplicate_provider_id_dead_letters(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("ob-dup"), "hi")
    jid = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    await led.enqueue_outbound(jid, "reply 1")
    await led.enqueue_outbound(jid, "reply 2")
    c1 = await led.claim_outbound("terminal")
    c2 = await led.claim_outbound("terminal")
    await led.mark_outbound_sent(c1["id"], "same-id")
    await led.mark_outbound_sent(c2["id"], "same-id")  # collision: must NOT silently mark sent
    async with led._pool.acquire() as con:
        sent = await con.fetchval(
            "SELECT count(*) FROM outbound WHERE status='sent' AND provider_msg_id='same-id'")
        failed = await con.fetchval("SELECT count(*) FROM outbound WHERE status='failed'")
        dl = await con.fetchval(
            "SELECT count(*) FROM dead_letter WHERE reason LIKE 'duplicate provider_msg_id%'")
    assert sent == 1, f"exactly one row should own the provider id, got {sent}"
    assert failed == 1, f"the duplicate should be failed, got {failed}"
    assert dl == 1, f"the duplicate should be dead-lettered, got {dl}"


async def test_submit_idempotent_on_inbound_event(led: PostgresLedger) -> None:
    await _truncate(led)
    ev = await led.ingest_inbound(_env("idem-1"), "hi")
    j1 = await led.submit_job(to_agent="kilabz", prompt="hi", inbound_event_id=ev)
    j2 = await led.submit_job(to_agent="kilabz", prompt="hi again", inbound_event_id=ev)
    assert str(j1) == str(j2), "a second submit for the same inbound_event must return the SAME job"
    async with led._pool.acquire() as con:
        n = await con.fetchval("SELECT count(*) FROM job WHERE inbound_event_id=$1", ev)
    assert n == 1, f"expected exactly 1 job per inbound_event, got {n}"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# -- controller-loop cursor: baseline seeds once, then dispatch -> advance ------
async def test_cursor_baseline_seed_and_advance(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "myndaix-runtime", "refs/heads/main", "a" * 40, "b" * 40
    far_past = _utcnow() - _dt.timedelta(hours=1)

    assert await led.upsert_baseline(R, REF, A) is True, "first sight must seed the baseline"
    assert await led.upsert_baseline(R, REF, B) is False, "must NOT re-seed an existing cursor"
    cur = await led.get_cursor(R, REF)
    assert cur["baseline_sha"] == A and cur["reviewed_sha"] == A
    assert cur["state"] == "baseline" and cur["pending_sha"] is None

    # baseline head == reviewed -> claiming the SAME sha is refused (already reviewed)
    assert await led.claim_dispatch(R, REF, A, far_past) is False, "no review of the baseline sha"
    # HEAD advanced to B -> claim wins, marks in-flight
    assert await led.claim_dispatch(R, REF, B, far_past) is True
    cur = await led.get_cursor(R, REF)
    assert cur["pending_sha"] == B and cur["state"] == "dispatching" and cur["attempts"] == 1
    # a fresh same-head claim is refused (in flight)
    assert await led.claim_dispatch(R, REF, B, far_past) is False, "in-flight head must not re-claim"
    # delivery confirmed -> advance the cursor to B
    assert await led.advance_cursor(R, REF, B) is True
    cur = await led.get_cursor(R, REF)
    assert cur["reviewed_sha"] == B and cur["pending_sha"] is None
    assert cur["state"] == "delivered" and cur["attempts"] == 0
    # advancing a head we didn't dispatch is a no-op
    assert await led.advance_cursor(R, REF, "c" * 40) is False


# -- controller-loop cursor: concurrent claims -> EXACTLY one winner -----------
async def test_cursor_claim_is_single_winner(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "fieldvision", "refs/heads/main", "1" * 40, "2" * 40
    far_past = _utcnow() - _dt.timedelta(hours=1)
    await led.upsert_baseline(R, REF, A)
    # 8 connections race to claim the same advanced head; the cursor CAS must elect one.
    results = await asyncio.gather(*[led.claim_dispatch(R, REF, B, far_past) for _ in range(8)])
    assert sum(results) == 1, f"exactly one claim must win, got {sum(results)}"


# -- controller-loop cursor: stale re-claim, then block, then a new head escapes
async def test_cursor_stale_reclaim_and_block(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B, C = "repoX", "refs/heads/main", "1" * 40, "2" * 40, "3" * 40
    await led.upsert_baseline(R, REF, A)

    assert await led.claim_dispatch(R, REF, B, _utcnow()) is True  # attempt 1
    # not stale yet -> a re-claim with a far-past cutoff is refused
    assert await led.claim_dispatch(R, REF, B, _utcnow() - _dt.timedelta(hours=1)) is False
    # age it -> a stale same-head re-claim now fires and increments attempts
    await _age_cursor(led, R, REF)
    assert await led.claim_dispatch(R, REF, B, _utcnow()) is True
    assert (await led.get_cursor(R, REF))["attempts"] == 2, "stale re-claim increments attempts"

    # ceiling reached -> block (CAS: only fires for THIS pending head at the ceiling)
    assert await led.mark_blocked(R, REF, B, 2) is True
    assert await led.mark_blocked(R, REF, "9" * 40, 2) is False, "CAS must not block a different head"
    await _age_cursor(led, R, REF)
    assert await led.claim_dispatch(R, REF, B, _utcnow()) is False, "blocked head must not re-claim"
    # but a NEW head escapes the block and resets attempts to 1
    assert await led.claim_dispatch(R, REF, C, _utcnow()) is True
    cur = await led.get_cursor(R, REF)
    assert cur["pending_sha"] == C and cur["state"] == "dispatching" and cur["attempts"] == 1


# -- controller-loop cursor: in-flight head is NOT superseded by a new head ----
async def test_cursor_no_supersede_in_flight(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B, C = "repoY", "refs/heads/main", "1" * 40, "2" * 40, "3" * 40
    await led.upsert_baseline(R, REF, A)
    assert await led.claim_dispatch(R, REF, B, _utcnow() - _dt.timedelta(hours=1)) is True
    # a NEW head C arrives while B is freshly in flight -> must NOT claim (wait for B)
    assert await led.claim_dispatch(R, REF, C, _utcnow() - _dt.timedelta(hours=1)) is False
    assert (await led.get_cursor(R, REF))["pending_sha"] == B, "pending must stay on the in-flight head"


# -- controller-loop cursor: release_dispatch un-sticks a failed trigger -------
async def test_release_dispatch(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "repoR", "refs/heads/main", "1" * 40, "2" * 40
    hour = _dt.timedelta(hours=1)
    await led.upsert_baseline(R, REF, A)
    assert await led.claim_dispatch(R, REF, B, _utcnow() - hour) is True   # attempt 1
    assert await led.claim_dispatch(R, REF, B, _utcnow() - hour) is False, "fresh in-flight -> no re-claim"
    # a trigger failure releases the dispatch -> immediate re-claim, attempts climbs to the ceiling
    assert await led.release_dispatch(R, REF, B) is True
    assert await led.claim_dispatch(R, REF, B, _utcnow() - hour) is True   # attempt 2 (no 1h wait)
    assert (await led.get_cursor(R, REF))["attempts"] == 2, "release preserves the attempt count"
    # release only matches the pending + dispatching head
    assert await led.release_dispatch(R, REF, "9" * 40) is False


# -- controller-loop cursor: forgive_transient refunds the attempt + releases ---
async def test_forgive_transient_refunds_and_releases(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "repoT", "refs/heads/main", "1" * 40, "2" * 40
    hour = _dt.timedelta(hours=1)
    await led.upsert_baseline(R, REF, A)
    assert await led.claim_dispatch(R, REF, B, _utcnow() - hour) is True   # attempt 1
    assert (await led.get_cursor(R, REF))["attempts"] == 1
    # a canary (transient) abort forgives: attempt refunded, slot released, still dispatching
    assert await led.forgive_transient(R, REF, B) is True
    cur = await led.get_cursor(R, REF)
    assert cur["attempts"] == 0, "forgive must refund the attempt"
    assert cur["state"] == "dispatching" and cur["pending_sha"] == B
    # updated_at was epoch'd -> a NOW stale cutoff re-claims immediately (no PENDING_STALE wait)
    assert await led.claim_dispatch(R, REF, B, _utcnow()) is True
    assert (await led.get_cursor(R, REF))["attempts"] == 1, "re-claim increments back to 1 (net flat)"


# -- controller-loop cursor: forgive_transient guards ----------------------------
async def test_forgive_transient_guards(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "repoTG", "refs/heads/main", "1" * 40, "2" * 40
    hour = _dt.timedelta(hours=1)
    await led.upsert_baseline(R, REF, A)
    assert await led.claim_dispatch(R, REF, B, _utcnow() - hour) is True
    # wrong head -> no forgive
    assert await led.forgive_transient(R, REF, "9" * 40) is False
    # attempts floor at 0: first forgive refunds 1 -> 0; a second STILL matches the row
    # (state stays dispatching, pending same) so it returns True but attempts stays 0
    assert await led.forgive_transient(R, REF, B) is True
    assert (await led.get_cursor(R, REF))["attempts"] == 0
    assert await led.forgive_transient(R, REF, B) is True, "row still matches (dispatching + pending)"
    assert (await led.get_cursor(R, REF))["attempts"] == 0, "attempts floors at 0, never negative"
    # state not 'dispatching' (after advance) -> no forgive
    assert await led.advance_cursor(R, REF, B) is True
    assert await led.forgive_transient(R, REF, B) is False, "delivered row must not forgive"


# -- controller-loop cursor: transient cycles keep attempts net-flat ------------
async def test_forgive_transient_net_flat_cycle(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "repoTF", "refs/heads/main", "1" * 40, "2" * 40
    hour = _dt.timedelta(hours=1)
    await led.upsert_baseline(R, REF, A)
    assert await led.claim_dispatch(R, REF, B, _utcnow() - hour) is True   # claim -> 1
    assert (await led.get_cursor(R, REF))["attempts"] == 1
    assert await led.forgive_transient(R, REF, B) is True                  # forgive -> 0
    assert (await led.get_cursor(R, REF))["attempts"] == 0
    assert await led.claim_dispatch(R, REF, B, _utcnow()) is True          # re-claim -> 1
    assert (await led.get_cursor(R, REF))["attempts"] == 1
    assert await led.forgive_transient(R, REF, B) is True                  # forgive -> 0
    assert (await led.get_cursor(R, REF))["attempts"] == 0
    assert (await led.get_cursor(R, REF))["attempts"] < 2, \
        "attempts never reaches 2 across transient cycles (can't climb to the ceiling)"


# -- controller-loop cursor: forgive_transient repairs a raced BLOCKED row ------
async def test_forgive_transient_repairs_blocked_row(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "repoTB", "refs/heads/main", "1" * 40, "2" * 40
    hour = _dt.timedelta(hours=1)
    await led.upsert_baseline(R, REF, A)
    assert await led.claim_dispatch(R, REF, B, _utcnow() - hour) is True   # attempt 1
    assert await led.mark_blocked(R, REF, B, 1) is True                    # the race: block won
    assert (await led.get_cursor(R, REF))["state"] == "blocked"
    # the marker is written only by the LOCAL worker (a poison head never writes one), so a
    # blocked row WITH a marker is a mis-classified transient -> forgive repairs the block
    assert await led.forgive_transient(R, REF, B) is True
    cur = await led.get_cursor(R, REF)
    assert cur["state"] == "dispatching", "forgive must repair the blocked row"
    assert cur["attempts"] == 0, "and still refund the attempt"
    # epoch'd updated_at + repaired state -> a NOW stale cutoff re-claims immediately
    assert await led.claim_dispatch(R, REF, B, _utcnow()) is True
    assert (await led.get_cursor(R, REF))["attempts"] == 1


# -- controller-loop cursor: skip_to advances past an empty-diff head ----------
async def test_skip_to(led: PostgresLedger) -> None:
    await _truncate(led)
    R, REF, A, B = "repoS", "refs/heads/main", "1" * 40, "2" * 40
    await led.upsert_baseline(R, REF, A)
    await led.claim_dispatch(R, REF, B, _utcnow() - _dt.timedelta(hours=1))  # pending=B
    assert await led.skip_to(R, REF, B) is True
    cur = await led.get_cursor(R, REF)
    assert cur["reviewed_sha"] == B and cur["pending_sha"] is None and cur["state"] == "delivered"
    assert await led.skip_to(R, REF, B) is False, "idempotent: no-op once reviewed == head"


# -- automerge gate: per-(PR,head) decision dedup ------------------------------
async def test_automerge_seen(led: PostgresLedger) -> None:
    await _truncate(led)
    R, A, B = "myndaix-runtime", "a" * 40, "b" * 40
    assert await led.automerge_decision(R, 42, A) is None, "unseen head -> None"
    await led.record_automerge(R, 42, A, "skipped", "CI pending")
    assert await led.automerge_decision(R, 42, A) == "skipped"
    assert await led.automerge_decision(R, 42, B) is None, "a new head is a fresh row (re-evaluate)"
    await led.record_automerge(R, 42, A, "merged", None)            # upsert overwrites for the same head
    assert await led.automerge_decision(R, 42, A) == "merged"
    raised = False
    try:
        await led.record_automerge(R, 42, B, "bogus")              # DB CHECK rejects bad decisions
    except asyncpg.PostgresError:
        raised = True
    assert raised, "the decision CHECK must reject an invalid value"


async def test_get_attempt_job_gate_rejects_cancelled(led: PostgresLedger) -> None:
    # the worker's last DB read before the (paid) invoke is a LOCKING ownership gate: a cancelled
    # job must read as None so the worker never reaches the charging adapter (cross-family review).
    await _truncate(led)
    jid = await led.submit_job(to_agent="higgsfield", prompt="paid")   # non_idempotent paid agent
    att = await led.lease_job("w1", [])
    job = await led.get_attempt_job(att)
    assert job is not None and job.to_agent == "higgsfield", "leased paid job is fetchable pre-cancel"
    await led.cancel(jid)
    assert await led.get_attempt_job(att) is None, \
        "after cancel the ownership gate returns None -> worker skips the paid invoke"


async def test_resolve_job_prefix(led: PostgresLedger) -> None:
    # `mxr get <short-id>` resolver: unique prefix -> one full id, shared prefix -> all
    # matches newest-first, no match -> []. Ids are crafted via UPDATE so the prefixes
    # are deterministic (submit_job mints random uuids).
    await _truncate(led)
    j1 = await led.submit_job(to_agent="mack", prompt="older")
    j2 = await led.submit_job(to_agent="mack", prompt="newer")
    a = _uuid.UUID("deadbeef-0000-4000-8000-000000000001")
    b = _uuid.UUID("deadbeef-1111-4111-8111-000000000002")
    async with led._pool.acquire() as con:
        await con.execute("UPDATE job SET id = $2 WHERE id = $1", j1, a)
        await con.execute("UPDATE job SET id = $2 WHERE id = $1", j2, b)

    # unique 12-hex prefix (hyphen-stripped by the caller) -> exactly the one full id
    assert await led.resolve_job_prefix("deadbeef0000") == [str(a)]
    # shared 8-hex prefix -> both, newest first (j2 was submitted after j1)
    assert await led.resolve_job_prefix("deadbeef") == [str(b), str(a)]
    # no match -> empty (cli maps this to the same rc as an unknown full id)
    assert await led.resolve_job_prefix("ffffffff") == []


async def test_active_workdirs(led: PostgresLedger) -> None:
    # the review-staging reaper's fail-safe in-use denylist: only NON-terminal jobs with
    # a context.workdir appear; a done/failed/dead job's workdir drops out (safe to reap).
    await _truncate(led)
    j_live = await led.submit_job(to_agent="kilabz", prompt="review",
                                  context={"workdir": "/stage/review-live"})
    await led.submit_job(to_agent="kilabz", prompt="no-wd")            # no workdir key
    j_done = await led.submit_job(to_agent="kilabz", prompt="finished",
                                  context={"workdir": "/stage/review-done"})
    # drive every leasable job terminal, then re-queue the live one so it is non-terminal
    att = await led.lease_job("w1", [])
    while att is not None:
        await led.complete_attempt(att, _ok())
        att = await led.lease_job("w1", [])
    # all three are now done; flip j_live back to queued to model an in-flight review
    async with led._pool.acquire() as con:
        await con.execute("UPDATE job SET status='queued' WHERE id=$1", j_live)
    wds = await led.active_workdirs()
    assert "/stage/review-live" in wds, wds
    assert "/stage/review-done" not in wds, "a terminal job's workdir must not be protected"
    assert all(w for w in wds), "no null/empty workdirs"


async def main() -> None:
    led = await PostgresLedger.connect(DSN)
    # fresh schema for the run (schema.sql is plain CREATE, not IF NOT EXISTS)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()

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
