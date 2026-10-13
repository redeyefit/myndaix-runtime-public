"""asyncpg-backed PostgresLedger - the production CommandAPI (DESIGN.md C2).

The SQLite store (sqlite_store.py) and this one implement the SAME Command-API
verbs; persistence swaps behind the contract. This is where the design's core
thesis lives: row-locking + a status-guarded state machine end the concurrency
bug classes that file-IPC caused (double-lease, lost update, dup delivery, a
crashed worker's job stuck forever).

Design rules, enforced everywhere below:
  * ONE transaction per verb. The Command API is the only writer.
  * Every transition is a status-guarded compare-and-swap:
        UPDATE ... WHERE <pk> AND status IN (<legal sources>) RETURNING id
    zero rows back => the move was illegal/already-done => the named no-op path.
  * Where two writers can collide on the same row we take a row lock:
        FOR UPDATE          - serialize (admission, complete-vs-reclaim)
        FOR UPDATE SKIP LOCKED - hand DISTINCT rows to concurrent workers (lease, claim)
  * READ COMMITTED isolation (Postgres default) is sufficient: correctness comes
    from the locks + the WHERE re-check, not from snapshot isolation.
  * ONE canonical lock order everywhere: attempt-row THEN job-row THEN
    repo_concurrency-row (the per-repo cap counter is ALWAYS locked LAST). Holding
    to a single order is what keeps the locks deadlock-free, so no verb raises a
    serialization/deadlock error and no retry loop is needed. (A cancel() that
    locked job-then-attempt formed an ABBA cycle with complete/fail/reclaim - a
    real deadlock caught by adversarial review; cancel now locks attempt-first.)
    lease_job locks the job (PICK) before the rc row, and its hard COUNT is a
    NON-locking read, so it never holds an attempt lock while waiting on the job.
    Multi-repo writers (reclaim batch, reconciler) lock rc rows in repo_id order.
  * authority (retry-safety) is NOT in the DB - only job.to_agent is - so the
    retry decision consults the registry (fail-closed on unknown agents).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Optional
from uuid import UUID

import asyncpg

from runtime import capture, outcomes, registry, skillmatch
from runtime.contracts import (
    Authority, ErrorClass, Job, LostLease, Result, TransportEnvelope,
)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Distinguishes "caller passed no CAS expectation" from "caller expects NO row / a NULL
# history_id" in inbox_mark_cursor_error (None is a meaningful expectation there — r10 #2/#3).
_UNBOUND = object()
# Fixed advisory-lock key so concurrent serve boots serialize their migrate() and
# don't race the same DDL. Arbitrary stable bigint ("mxrMIG").
_MIGRATE_LOCK_KEY = 0x6D7872_4D4947


def _new_id() -> str:
    return str(uuid.uuid4())


def _json(obj: dict) -> dict:
    """Pass-through; the jsonb codec (registered per connection) does json.dumps."""
    return obj


# The two things a str can carry that a strict-UTF-8 wire codec (asyncpg bind) or a
# Postgres 'text' column rejects at bind: a lone surrogate (the FULL D800–DFFF range)
# and NUL. Both must degrade, not abort.
_UNSTORABLE_RE = re.compile("[\x00\ud800-\udfff]")


def _utf8_safe(s: str) -> str:
    """Make an UNTRUSTED string storable so a poison byte can't abort a review at
    ingest. Two failure modes, both seen as a review aborted BEFORE the reviewer ran
    (the 2026-09-20 kilabz-abort class: 3 FieldVision reviews died at ingest):
      * a lone surrogate (\\udcXX) — an undecodable byte smuggled through argv/JSON;
        s.encode('utf-8') would raise DataError at bind. The FULL D800–DFFF range is
        replaced, not just surrogateescape's U+DC80–U+DCFF (which would leave a HIGH
        surrogate like \\ud800 to re-raise — cross-family review P2).
      * a NUL (\\x00) — valid UTF-8, so an encode check would miss it, but Postgres
        'text' rejects it and asyncpg aborts ingest all the same (review P1).
    Both degrade to U+FFFD; the diff stays fully reviewable. One C-level regex scan
    detects either: a clean str (the 99% case) returns untouched with NO transient
    bytes copy (review P3 — an encode() would double RSS on a large diff), and the
    sub pass runs only when there is actually something to repair."""
    if not _UNSTORABLE_RE.search(s):
        return s
    return _UNSTORABLE_RE.sub("�", s)


class PostgresLedger:
    # policy constants - NOT part of the Protocol; tuned at construction
    # LEASE + HEARTBEAT set TOGETHER (design M4 structure). LEASE_SECONDS is the window a
    # fresh attempt gets; HEARTBEAT_SECONDS is the EXTEND amount each ping pushes expiry to
    # (must be >= LEASE so a ping never SHRINKS the window). The pool's FIRING cadence
    # (heartbeat_interval_s, a separate quantity) must be <= lease/3 so a healthy job
    # heartbeats well within the window.
    #   CRASH-RECOVERY: the value also bounds slot-hold-after-crash. A crashed worker's
    #   lease expires HEARTBEAT_SECONDS after its LAST heartbeat — and a tight reclaim
    #   cadence does NOT shrink that (it only reclaims promptly AT expiry, not sooner). So
    #   with a per-repo cap, a long value would let a few crashed workers paralyze a repo
    #   for that long (cross-family review caught the design's flawed "reclaim mitigates"
    #   claim). Kept at 120s: heartbeats still cover arbitrarily long live jobs (the ping
    #   re-extends every <= lease/3), so a short window costs nothing and frees a crashed
    #   slot in ~2min instead of ~10. Tune up only if heartbeat churn ever matters.
    LEASE_SECONDS = 120          # a fresh lease; heartbeat extends long jobs
    HEARTBEAT_SECONDS = 120      # each heartbeat pushes expiry to now()+this (>= LEASE)
    RECLAIM_BATCH = 100          # reclaim_expired processes at most this per call
    OUTBOUND_MAX_TRIES = 5       # mark_outbound_failed exhaustion threshold
    MAX_CHILDREN = 32            # admission: max direct children per parent
    MAX_DEPTH = 8                # admission: max job-tree depth
    MAX_ATTEMPTS = 3             # poison ceiling: a job that fails/crashes this many
                                 # times stops requeuing -> dead + dead_letter (both
                                 # fail_attempt and reclaim_expired enforce it)
    # per-repo concurrency cap (design §4). FEATURE FLAG: a huge value disables the cap
    # (soft filter and hard count never trip) for instant rollback WITHOUT a code revert
    # — set $MYNDAIX_MAX_PER_REPO=1000000 in the pool's env and restart. Default 4.
    MAX_PER_REPO = int(os.environ.get("MYNDAIX_MAX_PER_REPO") or 4)
    # +learning rung: review-skill lifecycle windows (deterministic prune, archive-not-delete).
    SKILL_STALE_DAYS = 30        # active -> stale after this many days with no skill_use
    SKILL_ARCHIVE_DAYS = 90      # stale -> archived after this many days (reactivation = human re-arm)
    LEASE_MAX_REPICKS = 16       # bounded re-PICK budget per lease_job call: each over-cap
                                 # repo is excluded from the next PICK, so the eligible set
                                 # strictly shrinks (anti-spin). Exhaustion -> None (re-poll).

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ---- lifecycle ---------------------------------------------------------
    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 2, max_size: int = 32) -> "PostgresLedger":
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size,
            command_timeout=30.0, init=cls._init_connection,
        )
        return cls(pool)

    @staticmethod
    async def _init_connection(con: asyncpg.Connection) -> None:
        # dict <-> jsonb/json. Registered once per physical connection.
        for typ in ("jsonb", "json"):
            await con.set_type_codec(
                typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    async def close(self) -> None:
        await self._pool.close()

    async def init_schema(self) -> None:
        """Run schema.sql (DDL is plain CREATE, so call once on a fresh DB)."""
        sql = _SCHEMA_PATH.read_text()
        async with self._pool.acquire() as con:
            await con.execute(sql)

    async def migrate(self) -> list[str]:
        """Apply every migrations/*.sql in filename order, idempotently.

        serve() calls this on startup so deploying new code can NEVER run against a
        stale schema. That ordering footgun took dispatch down on 2026-06-24: serve was
        restarted onto code reading job.context before the column existed, so every
        dispatch errored. Auto-migrate-on-start removes the ordering decision entirely.

        Migrations MUST be idempotent (CREATE/ALTER ... IF NOT EXISTS) — they are
        re-run on every boot, including ones already applied by hand on prod. A pg
        advisory lock serializes concurrent serve instances so two boots can't race the
        same DDL. A failing migration raises (fail-closed: serve refuses to come up on a
        broken schema rather than serve a half-migrated DB). Returns filenames applied."""
        files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            return []
        async with self._pool.acquire() as con:
            await con.execute("SELECT pg_advisory_lock($1)", _MIGRATE_LOCK_KEY)
            try:
                for path in files:
                    await con.execute(path.read_text())
            finally:
                await con.execute("SELECT pg_advisory_unlock($1)", _MIGRATE_LOCK_KEY)
        return [p.name for p in files]

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _requeue_safe(to_agent: str) -> bool:
        """Authority-driven retry safety. FAIL-CLOSED: a workspace_actor (or an
        unknown agent, or a composite) is never auto-requeued - replaying a
        partial file mutation is unsafe. Only responders/controllers requeue.

        NOTE: the registry is in-memory today, so this is non-blocking even though
        fail_attempt/reclaim_expired call it while holding row locks. If the
        registry ever becomes I/O-backed, precompute the authority map OUTSIDE the
        transaction so no I/O happens under a lock."""
        try:
            spec = registry.get(to_agent)
            # a non-idempotent PAID supplier (its submit charges credits, no dedup) must NEVER
            # auto-requeue on crash/lease-expiry — a re-submit would double-charge. It goes
            # dead+surfaced regardless of authority (cross-family review CRITICAL).
            if spec.adapter.get("non_idempotent"):
                return False
            authority = spec.authority
        except Exception:
            return False
        return authority in (Authority.RESPONDER, Authority.CONTROLLER)

    # ---- ingest / dispatch -------------------------------------------------
    async def ingest_inbound(self, envelope: TransportEnvelope, body: str) -> UUID:
        """Create an inbound_event; dedupe on envelope.dedupe_key (exactly-once
        ingest). A duplicate returns the ORIGINAL id and raises nothing - the
        loser must not go on to submit a second job."""
        body = _utf8_safe(body)                      # untrusted diff -> never abort ingest on a bad byte
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """INSERT INTO inbound_event (id, transport, envelope, body, dedupe_key)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (dedupe_key) DO UPDATE SET dedupe_key = inbound_event.dedupe_key
                   RETURNING id""",
                _new_id(), envelope.transport, _json(envelope.model_dump(mode="json")),
                body, envelope.dedupe_key)
            return row["id"]

    async def submit_job(
        self, *, to_agent: str, prompt: str,
        context: Optional[dict] = None,
        parent_id: Optional[UUID] = None,
        inbound_event_id: Optional[UUID] = None,
        created_by: str = "human",
        repo_id: Optional[str] = None, base_ref: Optional[str] = None,
        priority: int = 0,
    ) -> UUID:
        """Queue a job. Admission (max_depth / max_children) is serialized by a
        FOR UPDATE lock on the parent row, so concurrent siblings funnel one at a
        time and the limit holds under load. Rejected -> a 'dead' job + dead_letter
        (returns a real id get_status can report)."""
        prompt = _utf8_safe(prompt)                  # same belt as ingest: prompt is the same untrusted body
        jid = _new_id()
        async with self._pool.acquire() as con:
            async with con.transaction():
                depth = 0
                root_id = jid                       # a root job is its own tree root
                if parent_id is not None:
                    parent = await con.fetchrow(
                        "SELECT depth, root_id, status FROM job WHERE id = $1 FOR UPDATE",
                        parent_id)
                    if parent is None:
                        raise ValueError(f"submit_job: unknown parent_id {parent_id}")
                    depth = parent["depth"] + 1
                    root_id = parent["root_id"]
                    nkids = await con.fetchval(
                        "SELECT count(*) FROM job WHERE parent_id = $1 AND status <> 'dead'",
                        parent_id)
                    # reject a child of a terminal parent (cancel() containment) OR over-limit
                    parent_live = parent["status"] in ("queued", "leased", "running")
                    reason = None
                    if not parent_live:
                        reason = (f"admission rejected: parent {parent_id} is "
                                  f"'{parent['status']}', not live")
                    elif depth > self.MAX_DEPTH or nkids >= self.MAX_CHILDREN:
                        reason = (f"admission rejected: depth={depth} (max {self.MAX_DEPTH}), "
                                  f"children={nkids} (max {self.MAX_CHILDREN})")
                    if reason is not None:
                        await con.execute(
                            """INSERT INTO job (id, parent_id, root_id, depth, created_by,
                                   inbound_event_id, to_agent, body, context, priority, status)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'dead')""",
                            jid, parent_id, root_id, depth, created_by, inbound_event_id,
                            to_agent, prompt, context or {}, priority)
                        await con.execute(
                            "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                            _new_id(), jid, reason)
                        return jid
                try:
                    async with con.transaction():  # savepoint around the insert
                        await con.execute(
                            """INSERT INTO job (id, parent_id, root_id, depth, created_by,
                                   inbound_event_id, to_agent, body, context, repo_id, base_ref,
                                   priority, status)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'queued')""",
                            jid, parent_id, root_id, depth, created_by, inbound_event_id,
                            to_agent, prompt, context or {}, repo_id, base_ref, priority)
                except asyncpg.UniqueViolationError:
                    # a job for this inbound_event already exists -> idempotent dispatch
                    # (a redelivered message must not spawn a second job + second reply).
                    return await con.fetchval(
                        "SELECT id FROM job WHERE inbound_event_id = $1", inbound_event_id)
        return jid

    # ---- worker lifecycle --------------------------------------------------
    # ONE INSERT-attempt CTE used by both the NULL-repo (cap-exempt) and the capped
    # CLAIM paths: flip job queued->leased and open the attempt in one statement.
    _CLAIM_SQL = """WITH leased AS (
                        UPDATE job SET status = 'leased'
                         WHERE id = $1 AND status = 'queued'
                        RETURNING id
                    )
                    INSERT INTO attempt (id, job_id, worker_id, lease_expires_at, status)
                    SELECT $2, id, $3,
                           statement_timestamp() + ($4 * interval '1 second'), 'open'
                      FROM leased
                    RETURNING id"""

    async def lease_job(self, worker_id: str, capabilities: list[str]) -> Optional[UUID]:
        """Atomically lease ONE queued job and open its attempt, enforcing the per-repo
        cap (design §4): PICK -> LOCK+SEED -> HARD-COUNT -> CLAIM, with a bounded re-PICK.

        PICK applies the SOFT filter (repo_id IS NULL OR cached active < cap) under
        FOR UPDATE OF j SKIP LOCKED LIMIT 1, so racing workers get DIFFERENT job rows.
        For a non-NULL repo we then seed+lock the rc row and take the HARD AUTHORITY:
        count(*) of OPEN attempts on the repo's LEASED/RUNNING jobs, UNDER that rc lock.
        < cap -> CLAIM (job->leased, INSERT attempt, set active=count+1). >= cap -> the
        soft filter was stale; sync the cache up and re-PICK with this repo EXCLUDED
        (so no spin). NULL repo_id is cap-EXEMPT and claims directly without touching rc.

        Because the cached `active` is only a soft filter, drift can never breach the
        cap — only the count-under-lock gates admission. Lock order is the canonical
        attempt -> job -> repo_concurrency (counter LAST): PICK locks the job (FOR UPDATE
        OF j); the rc row is locked AFTER, and the hard COUNT is a NON-locking read, so
        lease never holds an attempt lock while waiting on the job (no ABBA / 40P01).
        The hard count is scoped to leased/running jobs, so an orphan open attempt on an
        already-dead job (a transient cancel-race artifact) can never inflate the count.

        `capabilities` is accepted (Protocol) but capability-gated routing is a later
        slice: filtering on it now would only STARVE a gated job."""
        skip_repos: list[str] = []   # repos found AT-CAP this call; excluded from re-PICK
        async with self._pool.acquire() as con:
            for _ in range(self.LEASE_MAX_REPICKS):
                # one transaction per iteration: a re-PICK 'continue' COMMITS, releasing
                # the rejected candidate's job lock + the rc lock before the next PICK, so
                # locks never accumulate across iterations (no queue-wide serialization).
                async with con.transaction():
                    picked = await con.fetchrow(
                        """SELECT j.id, j.repo_id FROM job j
                             LEFT JOIN repo_concurrency rc ON rc.repo_id = j.repo_id
                            WHERE j.status = 'queued'
                              AND NOT EXISTS (SELECT 1 FROM attempt a
                                               WHERE a.job_id = j.id AND a.status = 'open')
                              AND (j.repo_id IS NULL OR COALESCE(rc.active, 0) < $1)
                              AND (j.repo_id IS NULL OR j.repo_id <> ALL($2::text[]))
                            ORDER BY j.priority DESC, j.created_at, j.id
                            FOR UPDATE OF j SKIP LOCKED
                            LIMIT 1""",
                        self.MAX_PER_REPO, skip_repos)
                    if picked is None:
                        return None                       # nothing leasable right now
                    repo_id = picked["repo_id"]
                    attempt_id = _new_id()

                    if repo_id is None:                   # NULL repo -> cap-EXEMPT
                        leased = await con.fetchval(
                            self._CLAIM_SQL, picked["id"], attempt_id, worker_id,
                            self.LEASE_SECONDS)
                        if leased is not None:
                            return leased
                        continue                          # lost the status race -> re-PICK

                    # SEED then LOCK the rc row (B4: ON CONFLICT DO NOTHING doesn't lock the
                    # conflicting row, so a separate FOR UPDATE select is required). Counter
                    # locked LAST, after the job row from PICK -> canonical order.
                    await con.execute(
                        "INSERT INTO repo_concurrency (repo_id, active) VALUES ($1, 0)"
                        " ON CONFLICT (repo_id) DO NOTHING", repo_id)
                    await con.fetchval(
                        "SELECT active FROM repo_concurrency WHERE repo_id = $1 FOR UPDATE",
                        repo_id)
                    # HARD authority: reality under the rc lock (NON-locking read of attempt).
                    open_now = await con.fetchval(
                        """SELECT count(*) FROM attempt a JOIN job j2 ON j2.id = a.job_id
                            WHERE a.status = 'open' AND j2.repo_id = $1
                              AND j2.status IN ('leased','running')""",
                        repo_id)
                    if open_now >= self.MAX_PER_REPO:
                        # cache was stale-low: sync it up to truth so OTHER workers' soft
                        # filter excludes this repo too, then exclude it from THIS call's
                        # next PICK (deterministic anti-spin) and re-PICK.
                        await con.execute(
                            "UPDATE repo_concurrency SET active = $2 WHERE repo_id = $1",
                            repo_id, open_now)
                        skip_repos.append(repo_id)
                        continue

                    leased = await con.fetchval(
                        self._CLAIM_SQL, picked["id"], attempt_id, worker_id,
                        self.LEASE_SECONDS)
                    if leased is None:
                        continue                          # job no longer queued -> re-PICK
                    # keep the soft filter honest: active := the true count we measured + 1
                    # (absolute, so it self-corrects prior increment-side drift too).
                    await con.execute(
                        "UPDATE repo_concurrency SET active = $2 WHERE repo_id = $1",
                        repo_id, open_now + 1)
                    return leased
            return None   # re-PICK budget exhausted (all eligible repos capped) -> re-poll

    async def get_attempt_job(self, attempt_id: UUID) -> Optional[Job]:
        """Internal: the Job a worker should run after leasing (the Protocol's
        lease_job returns only an id; a worker needs the prompt/repo to run).
        Returns None if the lease is no longer valid (attempt closed / job
        cancelled or reclaimed) so the worker skips running already-discarded work.

        LOCKING OWNERSHIP GATE (cross-family review): this is the worker's last DB read
        before _invoke (which, for a paid supplier, performs a NON-IDEMPOTENT charging POST).
        A plain non-locking read could observe a freshly-`leased` row in the gap BETWEEN a
        concurrent cancel()'s stmt-1 (close attempt) and stmt-2 (flip job dead), then invoke +
        charge after cancel was already in progress. Taking FOR UPDATE on attempt THEN job —
        the SAME canonical order cancel/fail/complete use, so no ABBA deadlock — serializes this
        check with cancel/reclaim: a cancelled/closed job is now reliably seen as None here. (The
        irreducible residual is a cancel that commits AFTER this locked read releases but before
        the HTTP submit lands — a network TOCTOU no DB state can close; bounded by the paid
        agent's non_idempotent flag to at most one surfaced charge.)"""
        async with self._pool.acquire() as con:
            async with con.transaction():
                a = await con.fetchrow(
                    "SELECT job_id FROM attempt WHERE id = $1 AND status = 'open' FOR UPDATE",
                    attempt_id)
                if a is None:
                    return None
                row = await con.fetchrow(
                    """SELECT id, to_agent, body, context, repo_id, base_ref, base_sha, worktree_path
                         FROM job WHERE id = $1 AND status IN ('leased','running') FOR UPDATE""",
                    a["job_id"])
        if row is None:
            return None
        return Job(id=row["id"], to_agent=row["to_agent"], prompt=row["body"],
                   context=row["context"] or {},
                   repo_id=row["repo_id"], base_ref=row["base_ref"],
                   base_sha=row["base_sha"], worktree_path=row["worktree_path"])

    async def heartbeat_attempt(self, attempt_id: UUID) -> None:
        """Extend the lease so a long job isn't reclaimed as crashed. A no-op on a
        closed/reclaimed attempt -> raise LostLease so the zombie worker aborts
        and never later calls complete_attempt."""
        async with self._pool.acquire() as con:
            row = await con.fetchval(
                """UPDATE attempt a
                      SET lease_expires_at = statement_timestamp() + ($2 * interval '1 second')
                     FROM job j
                    WHERE a.id = $1 AND a.status = 'open'
                      AND j.id = a.job_id AND j.status IN ('leased','running')
                   RETURNING a.id""",
                attempt_id, self.HEARTBEAT_SECONDS)
        if row is None:
            raise LostLease(f"heartbeat: attempt {attempt_id} no longer open")

    async def complete_attempt(self, attempt_id: UUID, result: Result) -> None:
        """attempt open->ok AND job leased/running->done, in one tx, locking BOTH
        rows (FOR UPDATE OF a,j - attempt first, the canonical order). Atomically
        queues the reply (transactional outbox) for a transport-originated job, so
        a 'done' job can never lose its reply. Zero rows back means the lease was
        reclaimed out from under this worker -> LostLease (NOT a harmless no-op).

        Policy (intentional): a worker that finishes AFTER its lease expired but
        BEFORE reclaim_expired runs still wins - 'finished-before-the-janitor-
        noticed'. reclaim's SKIP LOCKED yields to the in-flight completion."""
        # ONE statement (no explicit BEGIN round-trip, so it stays fast enough to
        # win the complete-vs-reclaim race): the `reply` data-modifying CTE always
        # executes and queues the outbound atomically (no-op for a controller job
        # with no inbound_event), while the final SELECT returns the job id - or
        # nothing, meaning the lease was reclaimed (LostLease).
        async with self._pool.acquire() as con:
            job_id = await con.fetchval(
                """WITH locked AS (
                       SELECT a.id, a.job_id
                         FROM attempt a JOIN job j ON j.id = a.job_id
                        WHERE a.id = $1 AND a.status = 'open'
                          AND j.status IN ('leased','running')
                        FOR UPDATE OF a, j
                   ),
                   closed AS (
                       UPDATE attempt a
                          SET status = 'ok', ended_at = statement_timestamp(), result = $2
                         FROM locked l WHERE a.id = l.id
                       RETURNING a.job_id
                   ),
                   dec AS (   -- per-repo cap: decrement gated on the attempt open->ok
                              -- close (RETURNING job_id), counter LAST. NULL repo_id ->
                              -- no matching rc row -> no-op (cap-exempt). GREATEST floors drift.
                       UPDATE repo_concurrency rc
                          SET active = GREATEST(rc.active - 1, 0)
                         FROM closed c JOIN job j2 ON j2.id = c.job_id
                        WHERE j2.repo_id IS NOT NULL AND rc.repo_id = j2.repo_id
                       RETURNING rc.repo_id
                   ),
                   done AS (
                       UPDATE job j SET status = 'done', artifact_ref = $3
                         FROM closed c
                        WHERE j.id = c.job_id AND j.status IN ('leased','running')
                       RETURNING j.id, j.inbound_event_id
                   ),
                   reply AS (
                       INSERT INTO outbound (id, job_id, transport, reply_target, body, status)
                       SELECT $4, d.id, ie.transport,
                              COALESCE(ie.envelope->>'reply_target', ie.transport || ':unknown'),
                              $5, 'pending'
                         FROM done d JOIN inbound_event ie ON ie.id = d.inbound_event_id
                       RETURNING id
                   )
                   SELECT id FROM done""",
                attempt_id, _json(result.model_dump(mode="json")), result.artifact_ref,
                _new_id(), result.text)
        if job_id is None:
            raise LostLease(f"complete: attempt {attempt_id} was reclaimed/closed")

    async def fail_attempt(self, attempt_id: UUID, result: Result) -> None:
        """attempt open->failed, then route the job by error_class AND authority:
          retryable + responder/controller -> queued (requeue)
          retryable + workspace_actor/unknown -> dead + dead_letter (never replay a mutation)
          terminal / needs_human / unknown class -> failed (fail-closed)
        Raises LostLease if the attempt is already closed (reclaimed) - symmetric
        with complete_attempt, so a worker never double-counts a job it lost."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                att = await con.fetchrow(
                    "SELECT id, job_id, status FROM attempt WHERE id = $1 FOR UPDATE", attempt_id)
                if att is None or att["status"] != "open":
                    raise LostLease(f"fail: attempt {attempt_id} no longer open")
                ec = result.error_class.value if result.error_class else None
                await con.execute(
                    """UPDATE attempt SET status='failed', result=$2, error_class=$3,
                           ended_at = statement_timestamp() WHERE id = $1""",
                    attempt_id, _json(result.model_dump(mode="json")), ec)
                job = await con.fetchrow(
                    "SELECT id, to_agent, repo_id FROM job WHERE id = $1 FOR UPDATE",
                    att["job_id"])
                to_agent = job["to_agent"]
                dead_reason = None
                if result.error_class is ErrorClass.RETRYABLE and self._requeue_safe(to_agent):
                    # poison ceiling: count ALL attempts (incl. the one just closed above,
                    # so the off-by-one is intentional) and dead-letter instead of requeuing
                    # once it reaches MAX_ATTEMPTS — a job that fails retryably every time
                    # can't requeue forever.
                    n_att = await con.fetchval(
                        "SELECT count(*) FROM attempt WHERE job_id = $1", att["job_id"])
                    if n_att >= self.MAX_ATTEMPTS:
                        new_status = "dead"
                        dead_reason = (f"poison: {n_att} attempts reached MAX_ATTEMPTS "
                                       f"({self.MAX_ATTEMPTS}) on '{to_agent}'")
                    else:
                        new_status = "queued"
                elif result.error_class is ErrorClass.RETRYABLE:
                    new_status = "dead"   # mutating/unknown agent: do not replay
                    dead_reason = f"retryable failure on non-retry-safe agent '{to_agent}'"
                else:
                    new_status = "failed"  # terminal / needs_human / None -> fail-closed
                await con.execute(
                    "UPDATE job SET status = $2 WHERE id = $1 AND status IN ('leased','running')",
                    att["job_id"], new_status)
                if new_status == "dead":
                    await con.execute(
                        "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                        _new_id(), att["job_id"], dead_reason)
                # per-repo cap: counter LAST. We KNOW the attempt went open->failed (the
                # FOR UPDATE + status guard above raised LostLease otherwise), so exactly
                # one slot is freed regardless of the requeue/dead/failed route. NULL repo
                # is cap-exempt; GREATEST floors any soft-cache drift at 0.
                if job["repo_id"] is not None:
                    await con.execute(
                        "UPDATE repo_concurrency SET active = GREATEST(active - 1, 0) "
                        "WHERE repo_id = $1", job["repo_id"])

    async def append_log(self, attempt_id: UUID, stream: str, chunk: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO attempt_log (attempt_id, stream, chunk) VALUES ($1,$2,$3)",
                attempt_id, stream, chunk)

    # ---- outbox (reliable, deduped delivery) -------------------------------
    async def enqueue_outbound(self, job_id: UUID, body: str) -> UUID:
        """Queue a reply, resolving transport+reply_target from the job's
        originating inbound_event (the outbox replies to the source transport)."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """INSERT INTO outbound (id, job_id, transport, reply_target, body, status)
                   SELECT $1, j.id, ie.transport, ie.envelope->>'reply_target', $3, 'pending'
                     FROM job j JOIN inbound_event ie ON ie.id = j.inbound_event_id
                    WHERE j.id = $2
                   RETURNING id""",
                _new_id(), job_id, body)
        if row is None:
            raise ValueError(
                f"enqueue_outbound: job {job_id} has no inbound_event "
                "(controller-originated replies are a later slice)")
        return row["id"]

    async def claim_outbound(self, transport: str) -> Optional[dict]:
        """Claim ONE pending row for a transport (SKIP LOCKED -> two senders get
        different rows) and return its payload {id, reply_target, body}, ready to
        deliver. pending->leased, tries+1. None if nothing pending."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """WITH picked AS (
                       SELECT id FROM outbound
                        WHERE transport = $1 AND status = 'pending'
                        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
                   )
                   UPDATE outbound o SET status = 'leased', tries = tries + 1
                     FROM picked p WHERE o.id = p.id
                   RETURNING o.id, o.reply_target, o.body""", transport)
        if row is None:
            return None
        return {"id": row["id"], "reply_target": row["reply_target"], "body": row["body"]}

    async def mark_outbound_sent(self, outbound_id: UUID, provider_msg_id: str) -> None:
        """leased->sent, recording provider_msg_id (exactly-once via UNIQUE). A
        re-send of the SAME row is a no-op (status no longer 'leased'). If the id
        is already owned by a DIFFERENT row, that's an anomalous duplicate send ->
        dead-letter THIS row rather than silently marking it sent with no id
        (which would hide the anomaly from get_status / the audit trail)."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                try:
                    async with con.transaction():  # savepoint
                        await con.execute(
                            """UPDATE outbound SET status='sent', provider_msg_id=$2
                                WHERE id = $1 AND status = 'leased'""",
                            outbound_id, provider_msg_id)
                except asyncpg.UniqueViolationError:
                    await con.execute(
                        "UPDATE outbound SET status='failed' WHERE id=$1 AND status='leased'",
                        outbound_id)
                    await con.execute(
                        "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                        _new_id(), outbound_id,
                        f"duplicate provider_msg_id {provider_msg_id} (already delivered)")

    async def mark_outbound_sent_inline(self, outbound_id: UUID, provider_msg_id: str) -> int:
        """pending->sent for INLINE (sync) delivery, returning the ROW COUNT (0 or 1) so
        the caller can gate its print on actually owning delivery (r8 P3 — a discarded
        count made the leased-race undetectable). Distinct verb on purpose (phone r7 #1):
        the CLI used to call the transport verb mark_outbound_sent, whose leased-only CAS
        silently no-opped on pending rows — the tombstone never landed and rows sat
        pending forever (latent duplicate delivery the day a cli-transport sender ships).
        Widening the transport CAS instead would let a buggy sender skip the claim step
        unnoticed; a separate pending-only verb keeps both state machines honest. A row a
        sender just LEASED returns 0 (delivery is the sender's); a row marked sent here is
        invisible to claim_outbound."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                try:
                    async with con.transaction():  # savepoint
                        tag = await con.execute(
                            """UPDATE outbound SET status='sent', provider_msg_id=$2
                                WHERE id = $1 AND status = 'pending'""",
                            outbound_id, provider_msg_id)
                        return int(tag.split()[-1])          # "UPDATE N"
                except asyncpg.UniqueViolationError:
                    await con.execute(
                        "UPDATE outbound SET status='failed' WHERE id=$1 AND status='pending'",
                        outbound_id)
                    await con.execute(
                        "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                        _new_id(), outbound_id,
                        f"duplicate provider_msg_id {provider_msg_id} (already delivered)")
                    return 0

    async def mark_outbound_failed(self, outbound_id: UUID) -> None:
        """leased->pending (retry) until tries exhaust, then leased->failed + dead_letter."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    """UPDATE outbound
                          SET status = CASE WHEN tries >= $2 THEN 'failed' ELSE 'pending' END
                        WHERE id = $1 AND status = 'leased'
                       RETURNING status""",
                    outbound_id, self.OUTBOUND_MAX_TRIES)
                if row is not None and row["status"] == "failed":
                    await con.execute(
                        "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                        _new_id(), outbound_id, "outbound delivery failed after max tries")

    # ---- janitor / control -------------------------------------------------
    async def reclaim_expired(self) -> int:
        """Requeue (or dead-letter) jobs whose lease expired - a crashed worker.
        FOR UPDATE OF a,j SKIP LOCKED means the janitor NEVER grabs a row a worker
        is mid-completing (that worker holds the lock without SKIP LOCKED, so it
        wins). Authority decides the fate: workspace_actors go dead, not requeued.

        Also heals the transient cancel-race orphan FAST (every tick): a queued job
        cancelled while a lease is mid-CLAIM can end up 'dead' with an open attempt the
        cancel's snapshot couldn't see (and a leaked rc slot). The hard count already
        ignores it (scoped to leased/running), so it's never a cap breach — but left
        until the slow reconciler it could soft-filter-starve the repo for ~45s. Closing
        it here (attempt-first lock, canonical order) + freeing its slot shrinks that to
        one janitor tick. Returns the count of EXPIRED leases reclaimed (orphans are a
        side-effect heal, not counted)."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                # FAST orphan heal — runs unconditionally, before the expired-lease pass.
                orphans = await con.fetch(
                    """WITH o AS (
                           SELECT a.id, j.repo_id FROM attempt a JOIN job j ON j.id = a.job_id
                            WHERE a.status='open' AND j.status NOT IN ('leased','running')
                            FOR UPDATE OF a SKIP LOCKED
                       ),
                       oc AS (
                           UPDATE attempt a SET status='failed', ended_at=statement_timestamp(),
                                  error_class='terminal'
                            WHERE a.id IN (SELECT id FROM o) RETURNING id
                       )
                       SELECT repo_id FROM o WHERE repo_id IS NOT NULL""")
                # Orphan heal already marked the dead attempts failed (above); ACCUMULATE their per-repo
                # slot frees into repo_dec and apply them WITH the expired-lease frees in the ONE sorted
                # rc loop at the end. Two SEPARATE sorted loops (orphan set THEN expired set) are each
                # ordered but their UNION is not monotonic in repo_id — orphan {'zeta'} then expired
                # {'alpha'} would lock rc['zeta'] before rc['alpha'] (descending) and can rc<->rc ABBA
                # vs the reconciler/another reclaimer (core-audit: violated the single-lock-order
                # invariant the module header claims). Merging keeps one ascending order. (orphans is
                # already WHERE repo_id IS NOT NULL.)
                repo_dec: dict[str, int] = {}
                for r in orphans:
                    repo_dec[r["repo_id"]] = repo_dec.get(r["repo_id"], 0) + 1
                rows = await con.fetch(
                    """WITH expired AS (
                           SELECT a.id AS attempt_id, a.job_id, j.to_agent, j.repo_id
                             FROM attempt a JOIN job j ON j.id = a.job_id
                            WHERE a.status = 'open'
                              AND a.lease_expires_at <= statement_timestamp()
                              AND j.status IN ('leased','running')
                            FOR UPDATE OF a, j SKIP LOCKED
                            LIMIT $1
                       ),
                       closed AS (
                           UPDATE attempt a
                              SET status='failed', ended_at=statement_timestamp(),
                                  error_class='retryable'
                            WHERE a.id IN (SELECT attempt_id FROM expired)
                           RETURNING a.id, a.job_id
                       )
                       SELECT c.job_id, e.to_agent, e.repo_id
                         FROM closed c JOIN expired e ON e.attempt_id = c.id""",
                    self.RECLAIM_BATCH)
                # NO early return on empty rows: the orphan frees accumulated above must still be applied
                # in the single sorted rc loop below (else an orphan-only tick leaks the healed slot).
                # Split the expired batch: workspace_actors die (never replay a mutation); requeue-safe
                # jobs requeue UNLESS they've hit the poison ceiling (a worker that keeps crashing mid-run
                # would otherwise reclaim->requeue forever). The count includes the attempt the `closed`
                # CTE just failed above (off-by-one intended), so the Nth crash is the one that
                # dead-letters. Every closed attempt frees a repo slot (BOTH requeue and dead), so
                # accumulate the per-repo decrement here (NULL repo_id is cap-exempt -> excluded).
                requeue, dead, dead_reasons = [], [], {}
                for r in rows:
                    jid = r["job_id"]
                    if r["repo_id"] is not None:
                        repo_dec[r["repo_id"]] = repo_dec.get(r["repo_id"], 0) + 1
                    if not self._requeue_safe(r["to_agent"]):
                        dead.append(jid)
                        dead_reasons[jid] = "lease expired; agent not auto-retried (non-requeue-safe)"
                        continue
                    n_att = await con.fetchval(
                        "SELECT count(*) FROM attempt WHERE job_id = $1", jid)
                    if n_att >= self.MAX_ATTEMPTS:
                        dead.append(jid)
                        dead_reasons[jid] = (f"poison: {n_att} attempts reached MAX_ATTEMPTS "
                                             f"({self.MAX_ATTEMPTS}) after lease expiry")
                    else:
                        requeue.append(jid)
                if requeue:
                    await con.execute(
                        """UPDATE job SET status='queued'
                            WHERE id = ANY($1::uuid[]) AND status IN ('leased','running')""",
                        requeue)
                if dead:
                    await con.execute(
                        """UPDATE job SET status='dead'
                            WHERE id = ANY($1::uuid[]) AND status IN ('leased','running')""",
                        dead)
                    for jid in dead:
                        await con.execute(
                            "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                            _new_id(), jid, dead_reasons[jid])
                # per-repo cap: counter LAST, applied per-repo in repo_id ORDER over the MERGED orphan +
                # expired frees (so this whole verb acquires rc rows in ONE consistent ascending order,
                # never rc<->rc ABBA vs the reconciler/another reclaimer). GREATEST floors soft-cache drift at 0.
                for rid in sorted(repo_dec):
                    await con.execute(
                        "UPDATE repo_concurrency SET active = GREATEST(active - $2, 0) "
                        "WHERE repo_id = $1", rid, repo_dec[rid])
                return len(rows)

    async def reapable_attempt_ids(self, min_age_s: float) -> set[str]:
        """Attempt ids the worktree GC may safely reap: CLOSED (status <> 'open') and
        closed at least min_age_s ago (by ended_at). This EXCLUDES live leases AND
        just-closed attempts, so the sweep can never delete a worktree whose worker might
        still be writing — a lost lease is only noticed on the worker's next heartbeat, so
        the grace window gives it time to abort and run its own cleanup first. Decided by
        attempt state, never filesystem mtime (an in-place edit doesn't refresh dir mtime)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT id FROM attempt
                    WHERE status <> 'open' AND ended_at IS NOT NULL
                      AND ended_at <= statement_timestamp() - make_interval(secs => $1)""",
                float(min_age_s))
        return {str(r["id"]) for r in rows}

    async def reconcile_repo_concurrency(self) -> int:
        """Slow soft-cache backstop (design §4 M1) — runs off the janitor at a slow
        cadence. Pure soft-cache maintenance: heals repo_concurrency.active back to the
        TRUTH = count of open attempts whose job is leased/running, per repo. Correctness
        never depends on it (the hard COUNT under the rc lock at lease time is the cap
        authority); this only restores soft-filter fairness, so disabling it leaves the
        cap intact. For the NORMAL close paths (complete/fail/reclaim of a live lease) the
        attempt-close decrements alone reconverge active to EXACTLY 0 at quiescence; the
        ONE case the decrements can't self-heal is the queued-cancel orphan (see cancel()
        — a leaked slot + orphan attempt on a dead job), which reclaim_expired's fast
        orphan-heal closes within a janitor tick. THIS reconciler is the slow absolute
        backstop that heals any residual drift (incl. that orphan if reclaim is idle).

        Three steps, all idempotent:
          0. ORPHAN backstop: close any open attempt whose job is ALREADY terminal (the
             transient cancel-race artifact). The hard count is scoped to leased/running
             so an orphan never breaches the cap, but closing it keeps the attempt table
             clean and lets the count below see truth.
          1. UPSERT-heal active to truth for every repo with live open attempts — UPSERT
             so a MISSING row is created (a missing row reads 0 -> over-FILTERS, never
             over-admits since the hard COUNT gates).
          2. Zero any rc row whose repo now has no live open attempts (drift-down).
        Locks every rc row FOR UPDATE in PK order first, so it serializes with — and never
        clobbers — a concurrent lease/decrement (which lock the rc row last), and can't
        ABBA another multi-repo writer. Returns the number of rc rows healed."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                # 0. orphan backstop: an open attempt on a non-live job is dead work.
                #    Lock the rows in a deterministic id order (FOR UPDATE OF a SKIP LOCKED)
                #    BEFORE updating, so two concurrent reconcilers (multiple serve
                #    instances) can't acquire attempt locks in clashing heap order -> no
                #    40P01. SKIP LOCKED yields any orphan a fast-heal/cancel already holds.
                await con.execute(
                    """WITH o AS (
                           SELECT a.id FROM attempt a JOIN job j ON j.id = a.job_id
                            WHERE a.status = 'open' AND j.status NOT IN ('leased','running')
                            ORDER BY a.id FOR UPDATE OF a SKIP LOCKED
                       )
                       UPDATE attempt a
                          SET status='failed', ended_at=statement_timestamp(),
                              error_class='terminal'
                        WHERE a.id IN (SELECT id FROM o)""")
                # serialize against lease/decrement: lock all rc rows in PK order.
                await con.execute(
                    "SELECT repo_id FROM repo_concurrency ORDER BY repo_id FOR UPDATE")
                # 1. heal/create rows to truth (open attempts on leased/running jobs).
                healed = await con.fetch(
                    """INSERT INTO repo_concurrency (repo_id, active)
                       SELECT j.repo_id, count(*)
                         FROM attempt a JOIN job j ON j.id = a.job_id
                        WHERE a.status = 'open' AND j.repo_id IS NOT NULL
                          AND j.status IN ('leased','running')
                        GROUP BY j.repo_id
                       ON CONFLICT (repo_id) DO UPDATE SET active = EXCLUDED.active
                       RETURNING repo_id""")
                # 2. zero rc rows whose repo has no live open attempts (drift-down).
                await con.execute(
                    """UPDATE repo_concurrency SET active = 0
                        WHERE active <> 0
                          AND NOT EXISTS (
                              SELECT 1 FROM attempt a JOIN job j ON j.id = a.job_id
                               WHERE a.status = 'open' AND j.repo_id = repo_concurrency.repo_id
                                 AND j.status IN ('leased','running'))""")
                return len(healed)

    async def dead_letter(self, source_id: UUID, reason: str) -> None:
        """Pure log-write. The owning verb already performed the source's state
        transition; this only records why."""
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO dead_letter (id, source_id, reason) VALUES ($1,$2,$3)",
                _new_id(), source_id, reason)

    async def cancel(self, job_id: UUID) -> None:
        """Administratively terminate a non-terminal job -> 'dead', failing its open
        attempt. The supervisor kills the OS process on the worker's next heartbeat
        (which will now raise LostLease).

        THREE EXPLICIT SEQUENTIAL statements, NOT one CTE: sibling data-modifying CTEs in
        a single statement execute in a planner-UNPREDICTABLE order (Postgres gives no
        text-order guarantee), so a single-CTE form could lock the job (`killed`) BEFORE
        the attempt (`closed`) -> job-then-attempt -> an ABBA / 40P01 deadlock with
        complete/fail (which lock attempt-then-job). Sequential statements GUARANTEE the
        canonical attempt -> job -> repo_concurrency (counter LAST) order. (Cross-family
        review — codex + Oracle — both caught the CTE-order risk; the single CTE also gave
        ZERO benefit since it never closed the orphan race below.) The decrement is gated
        on a REAL open-attempt close (stmt 1's RETURNING), so a queued / already-terminal /
        duplicate cancel decrements ZERO. NULL repo_id is cap-exempt.

        NOTE — this does NOT fully close the queued-cancel-vs-lease race: a lease that
        COMMITS its CLAIM after cancel's stmt-1 snapshot (while stmt-2 is about to flip the
        job) leaves an orphan open attempt on the dead job + a leaked soft slot. That
        orphan is HARMLESS to the cap (the hard count is scoped to leased/running, so it's
        never counted) and is healed within ONE janitor tick by reclaim_expired's fast
        orphan-heal (+ the reconciler backstop). A job-first cancel that COULD close it is
        rejected — it reintroduces the ABBA above."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                # 1. close any open attempt FIRST (locks the attempt row). RETURNING tells
                #    us whether a REAL open attempt closed here.
                closed_job = await con.fetchval(
                    """UPDATE attempt SET status='failed', ended_at=statement_timestamp(),
                           error_class='terminal'
                        WHERE job_id = $1 AND status = 'open' RETURNING job_id""",
                    job_id)
                # 2. flip the job dead (locks the job row) — attempt-then-job, canonical.
                await con.execute(
                    """UPDATE job SET status='dead'
                        WHERE id = $1 AND status IN ('queued','leased','running')""",
                    job_id)
                # 3. counter LAST: decrement iff a real open attempt closed, non-NULL repo
                #    (the job row is already locked by stmt 2 in this tx).
                if closed_job is not None:
                    await con.execute(
                        """UPDATE repo_concurrency rc SET active = GREATEST(rc.active - 1, 0)
                             FROM job j WHERE j.id = $1 AND j.repo_id IS NOT NULL
                              AND rc.repo_id = j.repo_id""",
                        job_id)

    async def get_status(self, job_id: UUID) -> dict:
        """Job + its attempts + outbound, as a plain dict. {} for an unknown id."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """SELECT j.id, j.to_agent, j.status, j.depth, j.artifact_ref,
                          j.base_sha, j.base_ref, j.repo_id, j.created_at, j.created_by,
                          (SELECT json_agg(json_build_object(
                                'id', a.id, 'status', a.status, 'worker_id', a.worker_id,
                                'error_class', a.error_class, 'text', a.result->>'text'))
                             FROM attempt a WHERE a.job_id = j.id) AS attempts,
                          (SELECT json_agg(json_build_object(
                                'id', o.id, 'status', o.status, 'reply_target', o.reply_target,
                                'body', o.body, 'provider_msg_id', o.provider_msg_id)
                                ORDER BY o.created_at, o.id)
                             FROM outbound o WHERE o.job_id = j.id) AS outbound
                     FROM job j WHERE j.id = $1""", job_id)
        if row is None:
            return {}
        out = dict(row)
        out["id"] = str(out["id"])
        if isinstance(out.get("created_at"), _dt.datetime):
            out["created_at"] = out["created_at"].isoformat()
        return out

    async def active_workdirs(self) -> set[str]:
        """Every context.workdir currently claimed by a NON-terminal job (queued/leased/
        running) — the fail-safe denylist the review-staging reaper consults so it can
        never remove a dir a live reviewer is still reading. Mirrors workspace.sweep
        deciding liveness by JOB STATE, never directory mtime: a reviewer reading a
        chmod'd-a-w snapshot never refreshes its mtime, so an mtime-only reaper would
        yank a long-running review's cwd (the terminal-state-gate defeat the adversarial
        review found). A dir is safe to reap only when NO live job points at it AND it is
        old."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT context->>'workdir' AS wd FROM job
                    WHERE status NOT IN ('done','failed','dead')
                      AND context ? 'workdir'""")
        return {r["wd"] for r in rows if r["wd"]}

    async def resolve_job_prefix(self, prefix: str) -> list[str]:
        """Full job ids (uuid text) whose hyphen-stripped id starts with `prefix` —
        the `mxr get <short-id>` resolver (cli.get_job validates lowercase hex,
        >=8 chars, BEFORE calling, so the LIKE pattern can carry no wildcards).
        Newest first, capped at 10: the cap only shapes the ambiguity error listing
        (>1 already fails closed); an 8-hex prefix colliding 10+ times isn't a real
        operator case."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT id::text AS id FROM job
                    WHERE replace(id::text, '-', '') LIKE $1 || '%'
                    ORDER BY created_at DESC LIMIT 10""", prefix)
        return [r["id"] for r in rows]

    async def count_queued(self) -> int:
        """Cheap queue-depth probe for a pool's idle detection."""
        async with self._pool.acquire() as con:
            return await con.fetchval("SELECT count(*) FROM job WHERE status = 'queued'")

    # ---- docs-only PR auto-merge gate (DESIGN v0.3 §4) ---------------------
    async def automerge_decision(self, repo_id: str, pr_number: int, head_sha: str) -> Optional[str]:
        """The terminal decision already recorded for this exact (repo, PR, head), or None
        if unseen. The gate skips a (PR, head) it has already decided so it never re-reviews
        the same head every tick; a new push (new head_sha) is a fresh row, so a re-pushed
        PR is re-evaluated."""
        async with self._pool.acquire() as con:
            return await con.fetchval(
                """SELECT decision FROM automerge_seen
                    WHERE repo_id = $1 AND pr_number = $2 AND head_sha = $3""",
                repo_id, pr_number, head_sha)

    async def record_automerge(self, repo_id: str, pr_number: int, head_sha: str,
                               decision: str, reason: Optional[str] = None) -> None:
        """Record the terminal decision for (repo, PR, head). Idempotent UPSERT on the head
        key (a re-evaluation of the same head overwrites the reason, e.g. a transient skip
        that later merged). `decision` ∈ merged|needs_fix|skipped|error (DB-checked)."""
        async with self._pool.acquire() as con:
            await con.execute(
                """INSERT INTO automerge_seen (repo_id, pr_number, head_sha, decision, reason)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (repo_id, pr_number, head_sha)
                   DO UPDATE SET decision = EXCLUDED.decision, reason = EXCLUDED.reason,
                                 decided_at = now()""",
                repo_id, pr_number, head_sha, decision, reason)

    # ---- controller-loop ("the brain") cursor (DESIGN v0.2 §2) -------------
    # The proactive review scheduler's only state. Each verb is one status-guarded
    # CAS returning whether THIS caller won the transition — same discipline as the
    # job state machine above, so the single-instance lock is belt-and-suspenders,
    # not the sole guard against a double dispatch.
    async def get_cursor(self, repo_id: str, ref: str) -> Optional[dict]:
        """The (repo_id, ref) cursor row as a plain dict, or None if unseen.
        `updated_at` is left as a datetime so the controller can compare it to its
        stale-pending cutoff."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """SELECT repo_id, ref, baseline_sha, reviewed_sha, pending_sha,
                          state, attempts, updated_at
                     FROM review_cursor WHERE repo_id = $1 AND ref = $2""",
                repo_id, ref)
        return dict(row) if row is not None else None

    async def upsert_baseline(self, repo_id: str, ref: str, head: str) -> bool:
        """Seed the high-water mark at first sight: baseline=reviewed=head, NOT
        reviewed (B2 — avoids the whole-tree diff blow-up). INSERT ... ON CONFLICT
        DO NOTHING, so it only ever seeds an ABSENT cursor; returns True iff seeded."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """INSERT INTO review_cursor
                       (repo_id, ref, baseline_sha, reviewed_sha, pending_sha, state, attempts)
                   VALUES ($1, $2, $3, $3, NULL, 'baseline', 0)
                   ON CONFLICT (repo_id, ref) DO NOTHING
                   RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def claim_dispatch(
        self, repo_id: str, ref: str, head: str, stale_before: _dt.datetime
    ) -> bool:
        """The dedup GATE: atomically claim `head` for dispatch. Returns True iff THIS
        caller won the slot (then it may invoke the review). The WHERE refuses to claim
        a head that is already reviewed, already blocked for that same head, or freshly
        in flight — but DOES re-claim a same-head pending older than `stale_before` (a
        dead dispatch) and always claims a NEW head. attempts resets to 1 on a new head,
        increments on a stale re-claim, so the blocked ceiling counts only the current
        head's tries."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor
                      SET pending_sha = $3,
                          state = 'dispatching',
                          attempts = CASE WHEN pending_sha IS DISTINCT FROM $3
                                          THEN 1 ELSE attempts + 1 END,
                          updated_at = now()
                    WHERE repo_id = $1 AND ref = $2
                      AND reviewed_sha <> $3
                      AND NOT (state = 'blocked' AND pending_sha = $3)
                      -- claim ONLY when idle, escaping a block, or re-claiming a dead
                      -- (stale) dispatch. A fresh in-flight head is NOT superseded — we
                      -- wait for it to deliver, then review the union next tick (Oracle
                      -- B2: superseding abandons the prior head's delivery + wastes a run).
                      AND (pending_sha IS NULL OR state = 'blocked' OR updated_at < $4)
                    RETURNING repo_id""",
                repo_id, ref, head, stale_before)
        return row is not None

    async def release_dispatch(self, repo_id: str, ref: str, head: str) -> bool:
        """Un-stick a dispatch whose trigger failed SYNCHRONOUSLY (missing/nonzero/timed-out
        play-review FRONT): force the pending row stale so the NEXT tick re-dispatches
        immediately instead of waiting out PENDING_STALE — while PRESERVING `attempts` so a
        persistently-failing trigger still climbs to the blocked ceiling. Guarded on the
        pending head + dispatching state. Returns True iff released."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor SET updated_at = to_timestamp(0)
                    WHERE repo_id = $1 AND ref = $2 AND pending_sha = $3
                      AND state = 'dispatching'
                    RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def forgive_transient(self, repo_id: str, ref: str, head: str) -> bool:
        """A dispatch aborted for a TRANSIENT infra reason (play-review's canary-stage abort
        marker): release the pending slot for prompt re-dispatch AND refund the attempt, so
        pool/agent flakiness can never climb to the blocked ceiling — only non-transient
        failures count toward poison (the 2026-06-30 wedge: hours of canary flakiness
        hard-blocked the backstop until a new head landed). The refund nets against the
        re-claim's increment, so attempts stay flat across transient cycles. ALSO repairs a
        'blocked' row back to 'dispatching' — the marker-lands-after-the-transient-pass race,
        where mark_blocked won the tick. Unblocking here is SAFE because the marker is written
        only by the LOCAL worker's canary/contention abort — a poison head never writes one —
        so a blocked row WITH a marker is a mis-classified transient, and claim_dispatch's
        `NOT (state='blocked' AND pending_sha=head)` guard would otherwise pin it forever.
        Same CAS shape as release_dispatch (pending head + a known state). Returns True iff
        forgiven."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor
                      SET updated_at = to_timestamp(0),
                          attempts = GREATEST(attempts - 1, 0),
                          state = 'dispatching'
                    WHERE repo_id = $1 AND ref = $2 AND pending_sha = $3
                      AND state IN ('dispatching', 'blocked')
                    RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def advance_cursor(self, repo_id: str, ref: str, head: str) -> bool:
        """Advance the cursor once a review of `head` DELIVERED (play-review's post-delivery
        done-<sha> marker). Guarded on pending_sha = head so it only ever advances the head
        we dispatched. Returns True iff advanced."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor
                      SET reviewed_sha = $3, pending_sha = NULL,
                          state = 'delivered', attempts = 0, updated_at = now()
                    WHERE repo_id = $1 AND ref = $2 AND pending_sha = $3
                    RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def skip_to(self, repo_id: str, ref: str, head: str) -> bool:
        """Advance the cursor straight to `head` WITHOUT a review — used when base..head has
        no net diff (an empty/revert-net-zero commit), which play-review would abort on and
        never mark done, wedging the head to BLOCKED (workflow MAJOR). Clears any pending.
        Guarded on reviewed_sha <> head so it is idempotent. Returns True iff advanced."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor
                      SET reviewed_sha = $3, pending_sha = NULL,
                          state = 'delivered', attempts = 0, updated_at = now()
                    WHERE repo_id = $1 AND ref = $2 AND reviewed_sha <> $3
                    RETURNING repo_id""",
                repo_id, ref, head)
        return row is not None

    async def mark_blocked(self, repo_id: str, ref: str, head: str, max_attempts: int) -> bool:
        """Sticky-block the current pending head after `max_attempts` failed dispatches.
        A CAS (codex M5): guarded on pending_sha = head AND attempts >= max_attempts AND
        state = 'dispatching', so it can NEVER clobber a concurrent new-head claim into
        'blocked' off a stale read. A later NEW head escapes it (claim_dispatch resets on
        a distinct head). Returns True iff this caller blocked the row."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """UPDATE review_cursor SET state = 'blocked', updated_at = now()
                    WHERE repo_id = $1 AND ref = $2 AND pending_sha = $3
                      AND attempts >= $4 AND state = 'dispatching'
                    RETURNING repo_id""",
                repo_id, ref, head, max_attempts)
        return row is not None

    # ---- Inbox Assistant per-account Gmail cursor (docs/inbox-assistant-design.md §3.9) --
    # The morning tick's only durable state: the last historyId whose slice fully
    # PROCESSED, per account (state in the ledger, not files). Same WHERE-guarded
    # CAS discipline as the review cursor above.
    async def inbox_get_cursor(self, account_id: str) -> Optional[dict]:
        """The account's cursor row as a plain dict, or None if unseen (first run —
        the tick then runs the bounded backfill; the row is created by the end-of-tick
        inbox_advance_cursor after FULL success, never eagerly).
        `fallback_since` is part of the tick's pinned shape but not persisted in v1
        (the backfill window is INBOX_BACKFILL_DAYS config, not state) — always None.
        `updated_at` is left as a datetime for staleness comparisons."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """SELECT account_id, history_id, state, attempts, updated_at
                     FROM inbox_cursor WHERE account_id = $1""",
                account_id)
        if row is None:
            return None
        return {**dict(row), "fallback_since": None}

    async def inbox_advance_cursor(self, account_id: str, history_id: str,
                                   expected_history_id: Optional[str]) -> bool:
        """Advance the cursor once the account's slice fully PROCESSED (labels +
        drafts + brief delivered) — never on pull alone, so a crash mid-run re-pulls
        rather than drops threads (design §3.9). TRUE CAS: `expected_history_id` is
        the value read at pull time (None on a rowless first run); the update fires
        ONLY when the row still holds it, so a slower older tick can never rewind a
        newer tick's cursor, and a concurrent first-run insert makes the loser's
        upsert a no-op (False -> that tick's slice re-pulls next time). A CAS-matched
        advance ALWAYS heals: state='active', attempts=0 — including the same-value
        case (quiet mailbox after an 'error'/'stale' tick), which the previous
        history_id-changed-only guard skipped, leaving error rows sticky forever.
        Returns True iff this call owned the advance (insert or CAS-matched update)."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """INSERT INTO inbox_cursor (account_id, history_id)
                   VALUES ($1, $2)
                   ON CONFLICT (account_id) DO UPDATE
                       SET history_id = EXCLUDED.history_id, state = 'active',
                           attempts = 0, updated_at = now()
                     WHERE inbox_cursor.history_id IS NOT DISTINCT FROM $3
                   RETURNING account_id""",
                account_id, history_id, expected_history_id)
        return row is not None

    async def inbox_mark_cursor_error(self, account_id: str, state: str,
                                      expected_history_id: object = _UNBOUND) -> bool:
        """Record a failed tick for the account: 'error' (pull/process failed) or
        'stale' (users.history.list 404 — cursor expired, awaiting the bounded
        backfill re-establish, design §4). Increments attempts so a persistently
        failing account is visible; the next advance resets both. `state` is
        validated here fail-closed (the DB CHECK is the backstop).

        `expected_history_id` (r10 #2 + #3):
        - omitted (_UNBOUND) — legacy unbound mark: UPDATE by account only; no-op False
          on a missing row. Used by pre-pull failure paths that never read the cursor.
        - a str — CAS-BOUND mark: increments ONLY if the row still holds the history_id
          this tick read at pull time. A slow failed tick that lost the race to a newer
          advance must not pollute the new cursor's attempts (the valve would fire
          against a legitimately in-flight slice).
        - None — the tick read NO row (first run): SEED an error row with NULL
          history_id so attempts can accumulate; the pull path treats NULL as no-cursor
          (bounded backfill), so the seed cannot corrupt incremental pulls. On conflict
          the increment stays bound to a still-NULL row (same CAS discipline).
        Returns True iff a row was written."""
        if state not in ("error", "stale"):
            raise ValueError(f"inbox_mark_cursor_error: bad state {state!r} (expected error|stale)")
        async with self._pool.acquire() as con:
            if expected_history_id is _UNBOUND:
                row = await con.fetchrow(
                    """UPDATE inbox_cursor
                          SET state = $2, attempts = attempts + 1, updated_at = now()
                        WHERE account_id = $1
                        RETURNING account_id""",
                    account_id, state)
            elif expected_history_id is None:
                # NULL-seed (or bound-to-seed) path. The canonical 0014 shape has history_id
                # NULLABLE. If a DB ran this migration's interim NOT NULL form (dev only — 0014
                # never shipped to main, and main's additivity lint FORBIDS an ALTER DROP NOT
                # NULL in the migration body) this INSERT raises NotNullViolationError. We FAIL
                # CLOSED (review r12, both reviewers): do NOT swallow it — a silently-dead
                # bounded-loss valve is worse than a loud crash, and the exception propagates
                # to the tick's nonzero exit so the operator SEES it. Repair a stale dev DB by
                # recreating inbox_cursor (it holds only regenerable cursor state).
                row = await con.fetchrow(
                    """INSERT INTO inbox_cursor (account_id, history_id, state, attempts)
                            VALUES ($1, NULL, $2, 1)
                       ON CONFLICT (account_id) DO UPDATE
                            SET state = $2, attempts = inbox_cursor.attempts + 1,
                                updated_at = now()
                          WHERE inbox_cursor.history_id IS NULL
                        RETURNING account_id""",
                    account_id, state)
            else:
                row = await con.fetchrow(
                    """UPDATE inbox_cursor
                          SET state = $2, attempts = attempts + 1, updated_at = now()
                        WHERE account_id = $1
                          AND history_id IS NOT DISTINCT FROM $3
                        RETURNING account_id""",
                    account_id, state, expected_history_id)
        return row is not None

    # ---- review skills ("+learning" rung) — DESIGN v0.3 governing sections --------------
    # Each verb mirrors the CAS/UPSERT discipline above. The pure matching + injection logic
    # lives in runtime.skillmatch (DB-free, unit-tested). The BODY lives in Postgres (the
    # indexer reads it from a trusted merged ref); selection NEVER rehashes disk (codex MAJOR).
    async def index_skills(self, repo_id: str, skills: list[dict]) -> dict:
        """UPSERT the per-repo skill cache from a trusted merged ref's skills/ contents (the
        controller parses + lint-validates each SKILL.md from the OWNED ref, never the worktree).
        One transaction. ON CONFLICT updates only when content_sha drifted (idempotent — a no-op
        tick changes nothing). A skill no longer present on the ref is ARCHIVED (reversible —
        archive-not-delete; re-adding via a PR upserts it back to active). The caller
        pre-validates (slug/desc/body/trigger/injection); the DB CHECKs are the fail-closed
        backstop (a violation raises -> the indexer alerts, never silently indexes a bad row).
        `skills` items: {name, description, body, body_sha, content_sha, path_trigger}."""
        present = [s["name"] for s in skills]
        up = 0
        async with self._pool.acquire() as con:
            async with con.transaction():
                for s in skills:
                    row = await con.fetchrow(
                        """INSERT INTO skill
                               (name, description, body, body_sha, content_sha,
                                repo_scope, path_trigger, provenance, state)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,'promoted','active')
                           ON CONFLICT (repo_scope, name) DO UPDATE
                               SET description = EXCLUDED.description, body = EXCLUDED.body,
                                   body_sha = EXCLUDED.body_sha, content_sha = EXCLUDED.content_sha,
                                   path_trigger = EXCLUDED.path_trigger,
                                   state = 'active'
                             WHERE skill.content_sha IS DISTINCT FROM EXCLUDED.content_sha
                           RETURNING name""",
                        s["name"], s["description"], s["body"], s["body_sha"],
                        s["content_sha"], repo_id, s["path_trigger"])
                    if row is not None:
                        up += 1
                arch = await con.fetch(
                    """UPDATE skill SET state = 'archived'
                        WHERE repo_scope = $1 AND state <> 'archived'
                          AND name <> ALL($2::text[])
                        RETURNING name""",
                    repo_id, present)
        return {"upserted": up, "archived_removed": len(arch), "total": len(skills)}

    async def select_skills(self, repo_id: str, changed_paths: list[str]) -> dict:
        """Pick <=2 ACTIVE skills for `repo_id` whose path_trigger matches any changed path
        (path-SEGMENT match), ordered new-first -> specificity desc -> recency desc. Matching +
        ordering are pure (runtime.skillmatch). A banned/broad trigger or a body_sha-drift row
        (tampered/half-written) is DROPPED here (fail-closed out of selection), drift surfaced
        for a jefe alert. Returns {"skills":[{name,body}], "drift":[name]}."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT name, body, body_sha, path_trigger, last_used_at
                     FROM skill WHERE repo_scope = $1 AND state = 'active'""",
                repo_id)
        cand, drift = [], []
        for r in rows:
            trig = r["path_trigger"]
            if skillmatch.is_banned_trigger(trig):
                continue  # belt: a banned trigger should never have been indexed
            # rank by the alternative that MATCHED, never the whole trigger (#125 r2
            # HIGH-1: a decorative specific alternative must not inflate a broad match)
            ms = skillmatch.match_specificity(trig, changed_paths)
            if ms is None:
                continue
            if hashlib.sha256(r["body"].encode()).hexdigest() != r["body_sha"]:
                drift.append(r["name"])
                continue
            cand.append((r, ms))
        # ORDER: new-first, THEN match-specificity, THEN recency. New-first over specificity
        # is DELIBERATE learning-rung design (kept over a #125 r3 reviewer proposal to invert):
        # a never-used skill must surface to accrue the usage/outcome data the self-learning
        # loop feeds on — specificity-first would starve every new skill behind established
        # ones. The 'flood of broad new skills' risk is bounded upstream: skills enter only
        # via the reviewed promotion gate, and bare-* alternatives are banned at lint.
        cand.sort(key=lambda rm: (
            rm[0]["last_used_at"] is not None,                  # NULL (new) sorts first
            -rm[1],                                             # more specific MATCH first
            -(rm[0]["last_used_at"].timestamp() if rm[0]["last_used_at"] else 0.0),  # recent first
        ))
        cand = [r for r, _ in cand]
        return {"skills": [{"name": r["name"], "body": r["body"]} for r in cand[:2]],
                "drift": drift}

    async def record_skill_use(self, repo_id: str, review_play: str, used: list[dict]) -> None:
        """Debounced usage accounting: bump last_used_at (at most once/hour per skill, so it
        stays off the review hot path) + append an audit row per skill. The CALLER (skillselect)
        swallows any error — a DB hiccup must never block a review (selection fails OPEN).
        `used` items: {name, body_sha}."""
        if not used:
            return
        names = [u["name"] for u in used]
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute(
                    """UPDATE skill SET last_used_at = now()
                        WHERE repo_scope = $1 AND name = ANY($2::text[])
                          AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')""",
                    repo_id, names)
                for u in used:
                    await con.execute(
                        """INSERT INTO skill_use (id, review_play, skill_name, body_sha, repo_scope)
                           VALUES ($1,$2,$3,$4,$5)""",
                        _new_id(), review_play, u["name"], u["body_sha"], repo_id)

    async def prune_skills(self) -> dict:
        """Deterministic, NO-LLM lifecycle prune — status-flip only (never deletes a row/file;
        reversible == fail-closed). active -> stale after SKILL_STALE_DAYS of no use; stale ->
        archived after SKILL_ARCHIVE_DAYS. NO reactivate-on-reuse (a stale skill is never
        selected, so reuse can't reach it — reactivation is human re-arm only, v0.3 #5). The
        `state` guard makes each transition a CAS (resolves a prune-vs-index race). Returns
        {staled, archived}."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                staled = await con.fetch(
                    """UPDATE skill SET state = 'stale'
                        WHERE state = 'active'
                          AND COALESCE(last_used_at, created_at) < now() - $1::interval
                        RETURNING name""",
                    _dt.timedelta(days=self.SKILL_STALE_DAYS))
                archived = await con.fetch(
                    """UPDATE skill SET state = 'archived'
                        WHERE state = 'stale'
                          AND COALESCE(last_used_at, created_at) < now() - $1::interval
                        RETURNING name""",
                    _dt.timedelta(days=self.SKILL_ARCHIVE_DAYS))
        return {"staled": len(staled), "archived": len(archived)}

    # ---- auto-capture ("the proposer") — v0.4 multi-signal recurrence + S6 state machine ----
    # NO LLM decides recurrence: an occurrence is recorded only for an ALLOWLISTED tag that BOTH
    # families flagged (caller enforces cross_family), and a class becomes `ready` only on the pure
    # multi-signal gate (capture.recurrence_ready). The separate proposer (S7) drives ready ->
    # proposing -> proposed; the human merge drives proposed -> promoted|declined.
    async def record_capture(self, repo_id: str, rule_tag: str, path_glob: str | None,
                             commit_sha: str, event_id: str, author: str, cross_family: bool, *,
                             min_recur: int, min_events: int, min_authors: int,
                             repropose_mult: int) -> dict | None:
        """Record ONE cross-family-agreed sighting of `rule_tag` on `repo_id` (deduped per
        (class, commit)), recompute the distinct-signal counts, and RETURN the candidate dict iff
        this call JUST transitioned the class to 'ready' (else None). Fail-closed: a missing/off-
        list tag, a non-cross-family signal, or a signal from skills/** records NOTHING. A 'declined'
        class re-fires only past the (higher) repropose floor; a class already ready/proposing/
        proposed/promoted/stale/error is counted (occurrence inserted) but never re-readied here."""
        if not cross_family or not capture.is_allowed_tag(rule_tag) or capture.slug(rule_tag) is None:
            return None
        if path_glob and path_glob.startswith("skills/"):       # never capture from the corpus itself
            return None
        fp = capture.fingerprint(repo_id, rule_tag)
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute(
                    """INSERT INTO capture_candidate (fingerprint, repo_scope, rule_tag, path_glob)
                           VALUES ($1,$2,$3,$4)
                       ON CONFLICT (fingerprint) DO UPDATE
                           SET last_seen = now(),
                               path_glob = COALESCE(EXCLUDED.path_glob, capture_candidate.path_glob)""",
                    fp, repo_id, rule_tag, path_glob)
                await con.execute(
                    """INSERT INTO capture_occurrence
                           (fingerprint, commit_sha, event_id, author, path_glob)
                           VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (fingerprint, commit_sha) DO NOTHING""",
                    fp, commit_sha, event_id, author, path_glob)
                counts = await con.fetchrow(
                    """SELECT count(*)                  AS commits,
                              count(DISTINCT event_id)  AS events,
                              count(DISTINCT author)    AS authors
                         FROM capture_occurrence WHERE fingerprint = $1""", fp)
                cand = await con.fetchrow(
                    "SELECT state, decline_count, path_glob FROM capture_candidate WHERE fingerprint = $1",
                    fp)
                state, decline = cand["state"], cand["decline_count"]
                # a declined OR TTL-staled class re-fires only past the (higher) repropose floor;
                # 'stale' MUST be re-readyable or a TTL-expired class wedges forever (cross-family).
                eff_recur = (capture.reready_threshold(decline, min_recur=min_recur, mult=repropose_mult)
                             if state in ("declined", "stale") else min_recur)
                ready = capture.recurrence_ready(
                    counts["commits"], counts["events"], counts["authors"],
                    min_recur=eff_recur, min_events=min_events, min_authors=min_authors)
                if state in ("new", "accumulating", "declined", "stale") and ready:
                    await con.execute(
                        "UPDATE capture_candidate SET state = 'ready' WHERE fingerprint = $1", fp)
                    return {"fingerprint": fp, "repo_scope": repo_id, "rule_tag": rule_tag,
                            "path_glob": cand["path_glob"], "decline_count": decline,
                            "commits": counts["commits"], "events": counts["events"],
                            "authors": counts["authors"]}
                if state == "new":
                    await con.execute(
                        "UPDATE capture_candidate SET state = 'accumulating' WHERE fingerprint = $1", fp)
        return None

    async def claim_for_proposing(self, fingerprint: str, branch: str, draft_sha: str) -> bool:
        """CAS 'ready' -> 'proposing', pinning the deterministic branch + draft_sha BEFORE any
        git/gh side effect (S6). Stamps proposed_at as the 'in-flight since' time so a crash mid-
        proposing can be reaped (reap_stuck_proposing). Returns True iff this call won the claim — a
        concurrent proposer tick gets False and must not also open a PR."""
        row = await self._pool.fetchrow(
            """UPDATE capture_candidate
                   SET branch = $2, draft_sha = $3, state = 'proposing', proposed_at = now()
                WHERE fingerprint = $1 AND state = 'ready'
                RETURNING fingerprint""", fingerprint, branch, draft_sha)
        return row is not None

    async def reap_stuck_proposing(self, timeout_minutes: int) -> int:
        """S6 anti-wedge: a proposer that crashed AFTER claiming 'proposing' but BEFORE opening the
        PR would occupy a MAX_OPEN slot forever (expire_stale_captures only touches 'proposed').
        Release any 'proposing' row older than the timeout back to 'ready' (clearing the pinned
        branch/draft_sha) so the next tick retries. Returns the count reaped."""
        rows = await self._pool.fetch(
            """UPDATE capture_candidate SET state = 'ready', branch = NULL, draft_sha = NULL
                WHERE state = 'proposing'
                  AND proposed_at IS NOT NULL
                  AND proposed_at < now() - make_interval(mins => $1)
                RETURNING fingerprint""", timeout_minutes)
        return len(rows)

    async def mark_capture_proposed(self, fingerprint: str, branch: str, draft_sha: str,
                                    pr_number: int) -> bool:
        """CAS 'proposing' -> 'proposed' once the PR is open (S6). FENCED on the claim identity
        (branch + draft_sha): after reap_stuck_proposing releases A's claim and B re-claims, A's
        late mark won't match B's row, so A can't clobber B's live claim / double-open a PR
        (cross-family race). Returns True iff THIS claim transitioned."""
        row = await self._pool.fetchrow(
            """UPDATE capture_candidate SET state = 'proposed', pr_number = $4, proposed_at = now()
                WHERE fingerprint = $1 AND state = 'proposing' AND branch = $2 AND draft_sha = $3
                RETURNING fingerprint""", fingerprint, branch, draft_sha, pr_number)
        return row is not None

    async def release_proposing(self, fingerprint: str, branch: str, draft_sha: str) -> bool:
        """Recovery: a proposer claimed 'proposing' but failed BEFORE opening the PR. CAS back to
        'ready' (clearing the pinned branch/draft_sha). FENCED on the claim identity so a resumed A
        can't release B's later claim (cross-family race). Returns True iff THIS claim released."""
        row = await self._pool.fetchrow(
            """UPDATE capture_candidate SET state = 'ready', branch = NULL, draft_sha = NULL
                WHERE fingerprint = $1 AND state = 'proposing' AND branch = $2 AND draft_sha = $3
                RETURNING fingerprint""", fingerprint, branch, draft_sha)
        return row is not None

    async def resolve_capture(self, fingerprint: str, outcome: str) -> bool:
        """Resolve a 'proposed' candidate from the proposer's RECONCILE/SWEEP (CAS from 'proposed'
        only; idempotent — already-resolved returns False):
          - 'promoted' (PR merged): terminal.
          - 'declined'  (PR closed by the human): increments decline_count + clears proposal state
            so it re-accumulates toward the (higher) exponential repropose floor.
          - 'stale' (TTL: PR sat un-acted past CAPTURE_TTL_DAYS): same clear + decline bump as a
            decline, distinct state name. The proposer calls this ONLY after re-reading the live PR
            and confirming it is NOT merged (A7 — never flip state before the read, or a TTL close
            races a human merge and inverts the learning signal). This supersedes the batch
            expire_stale_captures (which flipped state BEFORE the proposer could re-read)."""
        if outcome == "promoted":
            row = await self._pool.fetchrow(
                """UPDATE capture_candidate SET state = 'promoted'
                    WHERE fingerprint = $1 AND state = 'proposed' RETURNING fingerprint""", fingerprint)
        elif outcome in ("declined", "stale"):
            row = await self._pool.fetchrow(
                """UPDATE capture_candidate
                       SET state = $2, decline_count = decline_count + 1,
                           branch = NULL, draft_sha = NULL, pr_number = NULL, proposed_at = NULL
                    WHERE fingerprint = $1 AND state = 'proposed' RETURNING fingerprint""",
                fingerprint, outcome)
        else:
            raise ValueError(f"resolve_capture: bad outcome {outcome!r}")
        return row is not None

    async def list_proposed(self) -> list[dict]:
        """RECONCILE/SWEEP queue: every OPEN proposal (state='proposed') with its PR + branch + age,
        so the proposer can re-read each PR's LIVE state and resolve it (merged→promoted,
        closed→declined, or TTL-past-and-still-open→stale). READ-ONLY: the state flip is a separate
        resolve_capture CAS the proposer runs AFTER the live read (A7), never before. Oldest first."""
        rows = await self._pool.fetch(
            """SELECT fingerprint, repo_scope, rule_tag, pr_number, branch, proposed_at
                 FROM capture_candidate
                WHERE state = 'proposed' AND pr_number IS NOT NULL
                ORDER BY proposed_at ASC NULLS FIRST""")
        return [dict(r) for r in rows]

    async def count_open_proposals(self) -> int:
        """Open auto-PRs in flight (proposing or proposed) — the proposer gates on this vs
        CAPTURE_MAX_OPEN (S8 anti-fatigue) before claiming the next 'ready' class."""
        return await self._pool.fetchval(
            "SELECT count(*) FROM capture_candidate WHERE state IN ('proposing','proposed')")

    async def list_inflight_fingerprints(self) -> set:
        """The fingerprints count_open_proposals() counts (proposing OR proposed). The proposer's
        suspect resolution dedupes its capacity reservations against this set (ff-r5 P2: a
        surviving 'proposing' claim is already in the count — reserving again for its retained
        suspect double-blocks capacity until reap). READ-ONLY."""
        rows = await self._pool.fetch(
            "SELECT fingerprint FROM capture_candidate WHERE state IN ('proposing','proposed')")
        return {r["fingerprint"] for r in rows}

    async def list_ready_candidates(self, limit: int, after: str = "") -> list[dict]:
        """The proposer's work queue: 'ready' classes in FINGERPRINT keyset order, starting strictly
        AFTER `after` (the proposer persists the last-visited fingerprint as a cursor and wraps to
        "" when a scan comes back empty — kilabz r2 #7: a fixed oldest-first window re-reads the
        same prefix every tick, so persistently-skipped candidates STARVE everything behind them;
        the rotating cursor guarantees every ready row is eventually visited). READ-ONLY — the
        actual claim is a separate CAS (claim_for_proposing), so two reads can never double-propose.
        repo_scope is resolved by EXACT-KEY lookup by the caller; it is NEVER passed to a git/gh
        argv (attack-pass A2)."""
        rows = await self._pool.fetch(
            """SELECT fingerprint, repo_scope, rule_tag, path_glob, decline_count
                 FROM capture_candidate WHERE state = 'ready' AND fingerprint > $2
                ORDER BY fingerprint ASC LIMIT $1""", limit, after)
        return [dict(r) for r in rows]

    async def capture_provenance(self, fingerprint: str, limit: int = 8) -> list[str]:
        """The distinct commit SHAs where this class recurred — the proposer passes them to
        render_skill_md as finding_ids provenance (the human reads those commits to author the skill
        body). HEX-FILTERED in SQL (belt vs a non-hex commit_sha that slipped past the recorder —
        attack-pass A3), so a forged provenance string can never reach the rendered body. commit_sha
        is already unique per fingerprint (occurrence PK), so no DISTINCT needed. Most-recent first,
        capped (render truncates to 8 regardless)."""
        rows = await self._pool.fetch(
            """SELECT commit_sha FROM capture_occurrence
                WHERE fingerprint = $1 AND commit_sha ~ '^[0-9a-f]{7,40}$'
                ORDER BY seen_at DESC, commit_sha ASC LIMIT $2""", fingerprint, limit)
        return [r["commit_sha"] for r in rows]

    async def expire_stale_captures(self, ttl_days: int) -> list[dict]:
        """S8 anti-wedge: mark any 'proposed' class whose PR has sat past the TTL as 'stale' and
        RETURN {fingerprint, pr_number} so the proposer can close the abandoned PR — a garbage flood
        can't permanently occupy the MAX_OPEN slots. INCREMENTS decline_count (so a re-accumulating
        stale class re-fires only past the exponential repropose floor — without this it would
        immediately re-ready on the next sighting, spam-proposing; cross-family CRITICAL) and clears
        the proposal fields like a decline."""
        # CTE captures the OLD pr_number (RETURNING would otherwise yield the cleared NULL) so the
        # proposer can still close the abandoned PR, while we clear the proposal fields like a decline.
        rows = await self._pool.fetch(
            """WITH due AS (
                   SELECT fingerprint, pr_number FROM capture_candidate
                    WHERE state = 'proposed'
                      AND proposed_at IS NOT NULL
                      AND proposed_at < now() - make_interval(days => $1)
                    ORDER BY fingerprint
                    FOR UPDATE
               )
               UPDATE capture_candidate c
                   SET state = 'stale', decline_count = c.decline_count + 1,
                       branch = NULL, draft_sha = NULL, pr_number = NULL, proposed_at = NULL
                FROM due
                WHERE c.fingerprint = due.fingerprint
                RETURNING c.fingerprint, due.pr_number""", ttl_days)
        return [{"fingerprint": r["fingerprint"], "pr_number": r["pr_number"]} for r in rows]

    # ---- outcomes ledger (the per-finding OUTCOME LABEL) — v0.3 append-only state machine --------
    # Append-only: NEVER UPDATE/DELETE a finding_outcome row. Current state is the finding_current
    # view (human-terminal precedence, else latest-by-seq). Idempotency is the unique index
    # (finding_key, reviewer_family, outcome, outcome_source, source_event) + ON CONFLICT DO NOTHING
    # (outcome_source added in migration 0010 — self-labeling fence — so cross-source events never
    # collide/shadow), so re-running a review / sweep / dismissal is a no-op, not a duplicate event.
    # NO LLM decides identity or outcome:
    # the finding_key is a path-scoped line-hash (runtime.outcomes), the CLOSE phase compares STORED
    # hashes to what git shows at tip, and human labels are terminal (design §2 precedence).
    async def record_findings(self, repo_id: str, ref: str, tip_sha: str, play: str,
                              changed_paths: list[str],
                              open_findings: list[dict],
                              present_hashes: Optional[dict[str, set[str]]] = None) -> dict:
        """The per-review recorder: CLOSE phase (runs on EVERY delivered review, incl. PLAY_PASS) +
        OPEN phase (NEEDS-FIX reviews that raised findings). One transaction; idempotent.

        CLOSE — for each currently-'open' finding of THIS repo whose `path` ∈ `changed_paths` AND
        whose ORIGIN ref (the ref its EARLIEST 'open' row was raised on) EXACTLY matches this review's
        ref, if the finding's stored `line_hash` is NO LONGER present in the FILE at `tip_sha`, INSERT
        an 'applied_fixed' row (source_event 'review:<play>', outcome_source auto_fix_landed).

        The present-set is the design's actual CLOSE contract (§2): `present_hashes[path]` = the SET of
        line_hashes actually in that file at tip_sha (the caller computes it via
        outcomes.file_line_hashes per changed path — git OBJECTS, never the worktree). It is NOT "the
        reviewer re-flagged this line": a PASS review, or a review of an unrelated line in the same
        file, raises no `finding:` for the still-real issue, yet the issue's line is STILL in the file
        — so deriving 'present' from re-flags would false-close every open finding in a touched file.
        present_hashes[path] is THREE-STATE (core-audit HIGH): an EMPTY set = the file is CONFIRMED
        absent at tip (deleted/renamed) -> every finding in it closes (design-accepted §6); a populated
        set closes only the findings whose line is gone; and None = presence could NOT be determined (a
        transient git error) -> fail-CLOSED, leave the finding OPEN (a transient failure must never
        fabricate an applied_fixed and poison the ground truth). A path absent from the dict is treated
        as None (don't close). In PR-A tests supply present_hashes directly (keeps the verb DB-only, no
        git in postgres_store); PR-B wiring computes it per path via outcomes.file_line_hashes.

        ORIGIN-ref scoping (not finding_current's drifting latest-row ref): a finding opened on ref A
        must not be closed by a review on ref B even if the line is gone at B's tip. EXACT ref match,
        not default-branch closure — a main review must not close a finding raised on an unrelated
        feature branch whose fix never merged (design §2, v1 scope).

        OPEN — for each finding in `open_findings` INSERT an 'open' row, SKIPPING any (finding_key,
        family) whose current state carries a HUMAN label: dismissed_* (sticky dismissals — a human
        'this reviewer was wrong' suppresses re-detection, which is what the stable key exists FOR)
        and confirmed_real (0012: the human already ruled REAL — a re-detection must not re-ask via
        keys-files). Re-raise after 'expired' or 'applied_fixed' is allowed (a regression).

        `open_findings` is a list of resolved dicts from runtime.outcomes (the wiring layer already ran
        parse_finding_lines + resolve_and_hash, per family), each:
            {"tag", "path", "line_hash", "reviewer_family"}   (reviewer_family ∈ kilabz|oracle)
        Dedup is per (finding_key, reviewer_family): both families flagging the SAME tag/path/line keep
        INDEPENDENT rows (state is per family — precision IS the per-family measurement).
        Returns {"closed": n, "opened": n, "skipped_dismissed": n} for the caller to log. Fail-closed
        on a bad family via the DB CHECK; the caller has already validated tags/paths."""
        present = present_hashes or {}
        # OPEN accumulator keyed by (finding_key, reviewer_family): the same (tag,path,line) from BOTH
        # families is TWO distinct rows, not one (per-family state is the whole measurement — dedup on
        # finding_key alone would silently drop one family's row).
        by_key: dict[tuple[str, str], dict] = {}
        for f in open_findings:
            fam, path, lh = f["reviewer_family"], f["path"], f["line_hash"]
            fk = outcomes.finding_key(repo_id, f["tag"], path, lh)
            by_key[(fk, fam)] = {"finding_key": fk, "tag": f["tag"], "path": path,
                                 "line_hash": lh, "reviewer_family": fam}
        changed = set(changed_paths)
        closed = opened = skipped = 0
        opened_rows: list[dict] = []   # only the rows this call actually INSERTED (kilabz: the
        # follow-up key-file must surface real inserts, not sticky-dismissed/duplicate findings)
        async with self._pool.acquire() as con:
            async with con.transaction():
                # CLOSE: currently-'open' rows for this repo on a changed path whose ORIGIN ref (the
                # ref on the finding's EARLIEST open row) equals this review's ref — scoped to origin,
                # not finding_current's latest-row ref (a re-raise on a different ref must not move the
                # close scope). Then close iff the stored line_hash is gone from the FILE at tip.
                open_rows = await con.fetch(
                    """SELECT fc.finding_key, fc.reviewer_family, fc.rule_tag, fc.path, fc.line_hash
                         FROM finding_current fc
                         JOIN LATERAL (
                             SELECT fo.ref
                               FROM finding_outcome fo
                              WHERE fo.finding_key = fc.finding_key
                                AND fo.reviewer_family = fc.reviewer_family
                                AND fo.outcome = 'open'
                              ORDER BY fo.seq ASC
                              LIMIT 1
                         ) origin ON true
                        WHERE fc.repo_id = $1 AND fc.outcome = 'open'
                          AND fc.path = ANY($3::text[])
                          AND origin.ref = $2""",
                    repo_id, ref, list(changed))
                for r in open_rows:
                    present_in_file = present.get(r["path"])
                    if present_in_file is None:
                        continue                                # presence UNDETERMINED (transient git error,
                                                                # or path not supplied) -> fail-CLOSED: never
                                                                # fabricate a close (core-audit HIGH). A
                                                                # CONFIRMED-absent file is an empty set, which
                                                                # still closes below.
                    if r["line_hash"] in present_in_file:
                        continue                                # line still in the file -> not fixed
                    ins = await con.fetchval(
                        """INSERT INTO finding_outcome
                               (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path,
                                line_hash, source_event, tip_sha, outcome, outcome_source)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'applied_fixed','auto_fix_landed')
                           ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source, source_event)
                               DO NOTHING
                           RETURNING id""",
                        _new_id(), r["finding_key"], repo_id, ref, r["rule_tag"],
                        r["reviewer_family"], r["path"], r["line_hash"], f"review:{play}", tip_sha)
                    if ins is not None:
                        closed += 1
                # OPEN: insert 'open' rows per (finding_key, family), skipping any (key, family) whose
                # current state carries a HUMAN label (sticky): dismissed_* suppresses re-detection
                # (the reviewer was wrong / declined), and confirmed_real (migration 0012 makes it the
                # current winner) suppresses the re-ask — the human already ruled, so a re-detection
                # must not resurface the finding in keys-files or the label queue. One state read per
                # (key, family) via finding_current — both families' rows are independent.
                for (fk, fam), f in by_key.items():
                    cur = await con.fetchrow(
                        """SELECT outcome FROM finding_current
                            WHERE finding_key = $1 AND reviewer_family = $2""",
                        fk, fam)
                    if cur is not None and cur["outcome"] in (
                            "dismissed_false_positive", "dismissed_wontfix", "confirmed_real"):
                        skipped += 1
                        continue
                    ins = await con.fetchval(
                        """INSERT INTO finding_outcome
                               (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path,
                                line_hash, source_event, tip_sha, outcome, outcome_source)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open','review_raised')
                           ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source, source_event)
                               DO NOTHING
                           RETURNING id""",
                        _new_id(), fk, repo_id, ref, f["tag"], fam,
                        f["path"], f["line_hash"], f"review:{play}", tip_sha)
                    if ins is not None:
                        opened += 1
                        opened_rows.append({"finding_key": fk, "reviewer_family": fam,
                                            "rule_tag": f["tag"], "path": f["path"]})
        return {"closed": closed, "opened": opened, "skipped_dismissed": skipped,
                "opened_rows": opened_rows}

    async def human_dismiss(self, finding_key_prefix: str, family_or_all: str, kind: str,
                            *, principal_role: str) -> dict:
        """The human's per-finding dismissal (the fp-vs-wontfix label). GATED to human/admin — it
        writes a human-terminal row that finding_current_human + finding_labelqueue treat as gating +
        queue-terminal, so it takes the SAME principal binding as confirm_outcome (kilabz code-review
        BLOCKER — this must not be an unguarded fourth human-label writer). FAIL-CLOSED on an ambiguous or
        too-short prefix: a <12-hex or non-unique prefix dismisses NOTHING and returns the colliding
        full keys, so grinding a short-prefix collision is an error message, not a mislabel (design §2).

        `kind` ∈ fp|wontfix maps to dismissed_false_positive|dismissed_wontfix. `family_or_all` is
        'kilabz'|'oracle'|'all' — a dismissal writes ONE row per reviewer_family currently 'open' OR
        already-dismissed on that key (a human overrules whichever family raised it, and can CORRECT a
        prior mislabel: fp<->wontfix). DETERMINISTIC source_event 'human:<finding_key12>:<kind>' — the
        kind is IN the event so a correcting DIFFERENT-kind row is a distinct unique-index tuple
        (finding_key, reviewer_family, outcome, outcome_source, source_event) and INSERTS; the human row with the
        higher seq wins in finding_current, so the corrected label takes. Re-issuing the SAME kind is
        an idempotent ON CONFLICT DO NOTHING no-op. (The unique tuple gained outcome_source in
        migration 0010 — the self-labeling fence — but human_dismiss's behavior is unchanged: it
        always mints outcome_source='human_dismiss', so the 5-col conflict is identical to the 4-col.)

        Residual v1 limitation (rare, accepted): re-affirming the ORIGINAL kind after having corrected
        AWAY from it (a flip-flop back, e.g. fp -> wontfix -> fp) won't re-win — the original fp row
        already exists (ON CONFLICT DO NOTHING) and its seq is now stale (lower than the wontfix row),
        so finding_current keeps showing wontfix. A distinct third label would win; a true flip-flop
        back to an exact prior label is the one shape v1 can't re-terminalize. Documented, not fixed.

        Returns {"dismissed": n, "finding_key": <full>} on success (n = rows WRITTEN this call; a
        same-kind re-issue is 0), or {"error": ..., "candidates": [...]}."""
        if principal_role not in self._HUMAN_ROLES:
            raise PermissionError(f"human_dismiss is human/admin only, not {principal_role!r}")
        prefix = (finding_key_prefix or "").strip().lower()
        if len(prefix) < 12 or any(c not in "0123456789abcdef" for c in prefix):
            return {"error": "prefix must be >= 12 hex chars", "candidates": []}
        if kind == "fp":
            outcome = "dismissed_false_positive"
        elif kind == "wontfix":
            outcome = "dismissed_wontfix"
        else:
            raise ValueError(f"human_dismiss: bad kind {kind!r} (expected fp|wontfix)")
        families = (["kilabz", "oracle"] if family_or_all == "all"
                    else [family_or_all])
        if any(fam not in ("kilabz", "oracle") for fam in families):
            raise ValueError(f"human_dismiss: bad family {family_or_all!r}")
        async with self._pool.acquire() as con:
            async with con.transaction():
                # resolve the prefix to EXACTLY ONE full finding_key (fail-closed on 0 or >1). Distinct
                # keys sharing this prefix -> ambiguous -> refuse + surface the colliding keys.
                keys = await con.fetch(
                    """SELECT DISTINCT finding_key FROM finding_outcome
                        WHERE finding_key LIKE $1 || '%'""", prefix)
                if len(keys) == 0:
                    return {"error": "no finding matches that prefix", "candidates": []}
                if len(keys) > 1:
                    return {"error": "ambiguous prefix — refine it",
                            "candidates": [k["finding_key"] for k in keys]}
                fk = keys[0]["finding_key"]
                # kind IS in the source_event so a fp<->wontfix CORRECTION is a distinct unique tuple
                # that inserts; re-issuing the SAME kind is an ON CONFLICT DO NOTHING no-op.
                source_event = f"human:{fk[:12]}:{kind}"
                # write one dismissal per family currently 'open' OR already-dismissed on this key
                # (open -> first label; dismissed_* -> a correction). Idempotent per (family, kind).
                dismissed = 0
                for fam in families:
                    cur = await con.fetchrow(
                        """SELECT repo_id, ref, rule_tag, path, line_hash, tip_sha
                             FROM finding_current
                            WHERE finding_key = $1 AND reviewer_family = $2
                              AND outcome IN ('open','dismissed_false_positive','dismissed_wontfix')""",
                        fk, fam)
                    if cur is None:
                        continue                        # not open/dismissed for this family -> skip
                    ins = await con.fetchval(
                        """INSERT INTO finding_outcome
                               (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path,
                                line_hash, source_event, tip_sha, outcome, outcome_source)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'human_dismiss')
                           ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source, source_event)
                               DO NOTHING
                           RETURNING id""",
                        _new_id(), fk, cur["repo_id"], cur["ref"], cur["rule_tag"], fam,
                        cur["path"], cur["line_hash"], source_event, cur["tip_sha"], outcome)
                    if ins is not None:
                        dismissed += 1
        return {"dismissed": dismissed, "finding_key": fk}

    async def expire_open(self, ttl_days: int) -> int:
        """The TTL sweep (piggybacks the same outcome-record invocation, cheap SQL): tombstone every
        finding STILL AWAITING A HUMAN LABEL past `ttl_days`. Reads finding_labelqueue (no human label
        AND not already expired) — NOT finding_current.outcome='open', because a machine-proposed row
        (panel_*/exec_real_prior) can become the latest finding_current state and hide an unlabeled
        finding from an 'open'-only sweep, stranding it in the queue forever (kilabz code-review
        MEDIUM). Age = the LATEST review_raised detection (max(created_at) over review_raised rows): a
        genuine re-raise after expiry RESETS the clock — else the queue view correctly un-hides the
        re-detected finding and the very next sweep, anchored on the OLD first-raise, instantly
        re-expires it (kilabz r2 HIGH). A machine LABEL row still can't reset it (only review_raised
        counts). `expired` is a LIFECYCLE tombstone, not a label — counts toward neither precision side
        (design §2). DETERMINISTIC source_event 'sweep:<utcday>' so a same-day re-run is an
        index-conflict no-op. Returns the count expired."""
        utcday = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        source_event = f"sweep:{utcday}"
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """INSERT INTO finding_outcome
                       (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path,
                        line_hash, source_event, tip_sha, outcome, outcome_source)
                   SELECT gen_random_uuid(), lq.finding_key, lq.repo_id, lq.ref, lq.rule_tag,
                          lq.reviewer_family, lq.path, lq.line_hash, $1, lq.tip_sha,
                          'expired', 'ttl_sweep'
                     FROM finding_labelqueue lq
                     JOIN (SELECT finding_key, reviewer_family, max(created_at) AS raised_at
                             FROM finding_outcome
                            WHERE outcome_source = 'review_raised'
                            GROUP BY finding_key, reviewer_family) r
                       ON r.finding_key = lq.finding_key AND r.reviewer_family = lq.reviewer_family
                    WHERE r.raised_at < now() - make_interval(days => $2)
                   ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source, source_event) DO NOTHING
                   RETURNING id""",
                source_event, ttl_days)
        return len(rows)

    async def outcome_stats(self) -> dict:
        """The read surface for `mxr outcome-stats` (PR-B) + the morning brain-check: the
        finding_precision_raw view rows (per rule_tag × reviewer_family) + the current open-finding
        count (parser-drift starvation is VISIBLE when this stays 0). Reads the computed views only.
        NB: reads finding_precision_raw (the all-source DIAGNOSTIC, renamed in migration 0010) — this
        is a display surface, NOT an autonomy gate; the gating metric is finding_precision_promoted."""
        async with self._pool.acquire() as con:
            prec = await con.fetch(
                """SELECT rule_tag, reviewer_family, applied_fixed, dismissed_false_positive,
                          volume, precision
                     FROM finding_precision_raw ORDER BY rule_tag, reviewer_family""")
            open_n = await con.fetchval(
                "SELECT count(*) FROM finding_current WHERE outcome = 'open'")
        return {
            "precision": [dict(r) for r in prec],
            "open_count": open_n,
        }

    async def label_queue(self) -> list:
        """Read surface for `mxr labelqueue` (label-throughput PR-A §2c): every finding awaiting a
        human label, joined to its latest RAISE row. The join is on review_raised — NOT the latest
        row's state — because the queue deliberately contains auto-closed (applied_fixed) findings
        awaiting post-hoc human truth, and the browser must not hide them. Request-time join over
        the fence views, never a materialized core view (self-labeling design §4). Read-only."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT lq.rule_tag, lq.reviewer_family, left(lq.finding_key, 12) AS key12,
                          lq.path, r.ref, r.source_event, r.seq AS raise_seq
                     FROM finding_labelqueue lq
                     JOIN LATERAL (SELECT x.ref, x.source_event, x.seq
                                     FROM finding_outcome x
                                    WHERE x.finding_key = lq.finding_key
                                      AND x.reviewer_family = lq.reviewer_family
                                      AND x.outcome_source = 'review_raised'
                                    ORDER BY x.seq DESC LIMIT 1) r ON true
                    ORDER BY lq.rule_tag, lq.reviewer_family, lq.path""")
        return [dict(r) for r in rows]

    # ---- shadow dial (docs/shadow-dial-design.md v0.6 PR-A): MEASURE-ONLY reads + the one
    # opt-in append to the dial's OWN table. The SQL here is a PURE PROJECTION — every threshold
    # lives in the verb (v0.2 D1), and dial_shadow_snapshot is NEVER read by review/prompt/gate
    # code (design m1; its only reader is `mxr dial-shadow --eval`). --------------------------------

    async def dial_shadow_labels(self, cutoff: int | None = None) -> list:
        """One CURRENT human row per (finding, family) from finding_current_human (the admitted-set
        view — machine sources structurally absent), joined to the finding's latest RAISE row for
        provenance (ref + play: the design §2 anti-poisoning signal is that labels span independent
        reviews, not one bulk dismissal). LEFT JOIN + ref fallback: a missing raise row degrades to
        NO play provenance (fail-safe — fewer distinct, never more).

        `cutoff` (the --snapshot path, xreview r1 HIGH): with a cutoff the read is BOUNDED to
        seq <= cutoff (rows AND the provenance join) so a snapshot's cells are exactly consistent
        with its data_cutoff_seq — the verb reads max_finding_seq() FIRST, then labels. A label
        racing in during the snapshot lands seq > cutoff: absent from these cells, present in a
        future eval cohort (seq > cutoff) — never lost, never double-counted. Edge (documented,
        accepted): a CORRECTION racing into that window supersedes its finding's current row in
        the view, so the finding drops from this snapshot's cells entirely and re-enters as
        post-cutoff evidence — consistent, just not an as-of-cutoff reconstruction (which would
        require duplicating the 0010 view SQL here; not worth the drift risk at one-label scale)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT h.rule_tag, h.reviewer_family, h.outcome, h.outcome_source, h.seq,
                          COALESCE(r.ref, h.ref) AS ref, r.source_event AS raise_event
                     FROM finding_current_human h
                     LEFT JOIN LATERAL (SELECT x.ref, x.source_event
                                          FROM finding_outcome x
                                         WHERE x.finding_key = h.finding_key
                                           AND x.reviewer_family = h.reviewer_family
                                           AND x.outcome_source = 'review_raised'
                                           AND ($1::bigint IS NULL OR x.seq <= $1)
                                         ORDER BY x.seq DESC LIMIT 1) r ON true
                    WHERE ($1::bigint IS NULL OR h.seq <= $1)
                    ORDER BY h.rule_tag, h.reviewer_family, h.seq""", cutoff)
        out = []
        for r in rows:
            d = dict(r)
            ev = d.pop("raise_event") or ""
            # play provenance only from a well-formed 'review:<play>' raise event; anything else
            # (missing raise, foreign shape) is None -> counts as NO provenance in the core.
            d["play"] = ev[len("review:"):] if ev.startswith("review:") else None
            out.append(d)
        return out

    async def max_finding_seq(self) -> int:
        """The snapshot's data_cutoff_seq (design §4): max finding_outcome.seq at capture — the
        'labels since' boundary a future eval measures against."""
        async with self._pool.acquire() as con:
            return await con.fetchval("SELECT COALESCE(max(seq), 0) FROM finding_outcome")

    async def dial_shadow_snapshot_append(self, rows: list) -> int:
        """Append fully-formed snapshot rows (the verb builds them — schema M5: every row carries
        its complete policy context). Append-only; one transaction so a snapshot is all-or-nothing."""
        if not rows:
            return 0
        cols = ["data_cutoff_seq", "rule_tag", "reviewer_family", "confirmed_real", "dismissed_fp",
                "n", "precision", "wilson_lo", "wilson_hi", "n_recent", "wilson_recent_lo",
                "wilson_recent_hi", "distinct_refs", "distinct_plays", "would_say", "suppressible",
                "floor", "ceiling", "min_n", "min_refs", "min_plays", "min_fp", "recency_n", "z",
                "stable_snaps", "eval_min_n", "eval_agree", "week_span_rule",
                "suppressible_set_version", "taxonomy_version"]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        sql = (f"INSERT INTO dial_shadow_snapshot ({', '.join(cols)}) VALUES ({placeholders})")
        async with self._pool.acquire() as con:
            async with con.transaction():
                for row in rows:
                    await con.execute(sql, *[row[c] for c in cols])
        return len(rows)

    async def dial_shadow_snapshots(self) -> list:
        """All snapshot rows, cell-grouped order — the --eval read (this table's ONLY reader)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT * FROM dial_shadow_snapshot
                    ORDER BY rule_tag, reviewer_family, captured_at, id""")
        return [dict(r) for r in rows]

    # ---- self-labeling FENCE: the write verbs (docs/self-labeling-design.md v0.4) -----------------
    # Each labeled-write verb SERVER-MINTS outcome_source + source_event (never caller-supplied),
    # hard-codes its exact (source, outcome) PAIRS (§3 legal-pair table + the DB pair-CHECK backs it),
    # and asserts the caller's principal-role against the §5 matrix. Consequence: a MACHINE identity can
    # never mint a human label, no verb can mint an off-pair combination, and (with the promoted/queue
    # views) nothing a machine writes gates precision OR removes a finding from finding_labelqueue.
    #
    # AUTH-BINDING CONTRACT (kilabz code-review HIGH): `principal_role` is a TRUSTED-CALLER binding,
    # NOT untrusted request input. It is set by the AUTHENTICATED caller layer — api.py's
    # Principal.role (client|admin) for an HTTP client, or the local operator for a direct `mxr` op —
    # and MUST be required (no permissive default). PR-1 wires NO machine caller to a human verb; when
    # the labeler/exec-oracle SERVICES land (PR-2/3) their pool-internal code calls ONLY their own verb
    # (record_exec_prior / propose_outcome), and any API exposure derives the role from the
    # authenticated api-key, never the body. The role check here is the belt over that architecture.
    # confirm_outcome + human_dismiss are BOTH gated human writers (the two human-label paths); the
    # #74/#77-hardened human_dismiss stays otherwise byte-identical.
    _HUMAN_ROLES = ("human", "admin")

    async def _finding_fields(self, con, finding_key: str, reviewer_family: str):
        """The repo_id/ref/rule_tag/path/line_hash/tip_sha to stamp a new row for an EXISTING finding,
        read from finding_current (any state). None if the finding was never raised — a verb must not
        conjure a row for a finding that does not exist."""
        return await con.fetchrow(
            """SELECT repo_id, ref, rule_tag, path, line_hash, tip_sha
                 FROM finding_current WHERE finding_key = $1 AND reviewer_family = $2""",
            finding_key, reviewer_family)

    async def confirm_outcome(self, finding_key_prefix: str, family_or_all: str, kind: str,
                              *, principal_role: str) -> dict:
        """HUMAN/admin ONLY — the ONLY writer of a GATING + label-terminal row. kind ∈
        real|fp|wontfix -> (human_confirm/confirmed_real) | (human_dismiss/dismissed_false_positive) |
        (human_dismiss/dismissed_wontfix). Server-mints source_event 'human:<key12>:<kind>'. Same
        fail-closed prefix resolution as human_dismiss (>=12 hex, unique). Returns
        {"written": n, "finding_key": <full>} or {"error": ..., "candidates": [...]}."""
        if principal_role not in self._HUMAN_ROLES:
            raise PermissionError(f"confirm_outcome is human/admin only, not {principal_role!r}")
        pairs = {"real": ("human_confirm", "confirmed_real"),
                 "fp": ("human_dismiss", "dismissed_false_positive"),
                 "wontfix": ("human_dismiss", "dismissed_wontfix")}
        if kind not in pairs:
            raise ValueError(f"confirm_outcome: bad kind {kind!r} (expected real|fp|wontfix)")
        source, outcome = pairs[kind]
        prefix = (finding_key_prefix or "").strip().lower()
        if len(prefix) < 12 or any(c not in "0123456789abcdef" for c in prefix):
            return {"error": "prefix must be >= 12 hex chars", "candidates": []}
        families = (["kilabz", "oracle"] if family_or_all == "all" else [family_or_all])
        if any(fam not in ("kilabz", "oracle") for fam in families):
            raise ValueError(f"confirm_outcome: bad family {family_or_all!r}")
        async with self._pool.acquire() as con:
            async with con.transaction():
                keys = await con.fetch(
                    "SELECT DISTINCT finding_key FROM finding_outcome WHERE finding_key LIKE $1 || '%'",
                    prefix)
                if len(keys) == 0:
                    return {"error": "no finding matches that prefix", "candidates": []}
                if len(keys) > 1:
                    return {"error": "ambiguous prefix — refine it",
                            "candidates": [k["finding_key"] for k in keys]}
                fk = keys[0]["finding_key"]
                source_event = f"human:{fk[:12]}:{kind}"
                written = 0
                for fam in families:
                    f = await self._finding_fields(con, fk, fam)
                    if f is None:
                        continue                        # that family never raised this finding
                    ins = await con.fetchval(
                        """INSERT INTO finding_outcome
                               (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path,
                                line_hash, source_event, tip_sha, outcome, outcome_source)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                           ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source,
                                        source_event) DO NOTHING
                           RETURNING id""",
                        _new_id(), fk, f["repo_id"], f["ref"], f["rule_tag"], fam,
                        f["path"], f["line_hash"], source_event, f["tip_sha"], outcome, source)
                    if ins is not None:
                        written += 1
        return {"written": written, "finding_key": fk}

    async def record_exec_prior(self, finding_key: str, reviewer_family: str,
                                *, principal_role: str, tip_sha: str) -> dict:
        """EXEC-ORACLE service identity ONLY (PR-2 play-fix observe bridge). Mints
        exec_verified/exec_real_prior — a positive red->green REAL PRIOR: never an FP, never gates,
        never removes from the queue. Server-mints source_event 'probe:<utcday>:<tip12>'. Returns
        {"written": 0|1, "finding_key": ...} or {"error": ...}."""
        if principal_role != "exec_oracle":
            raise PermissionError(f"record_exec_prior is exec-oracle only, not {principal_role!r}")
        if not (isinstance(tip_sha, str) and len(tip_sha) >= 12):
            raise ValueError("record_exec_prior: tip_sha must be a full sha")
        utcday = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        source_event = f"probe:{utcday}:{tip_sha[:12]}"
        async with self._pool.acquire() as con:
            async with con.transaction():
                f = await self._finding_fields(con, finding_key, reviewer_family)
                if f is None:
                    return {"error": "no such finding", "finding_key": finding_key}
                ins = await con.fetchval(
                    """INSERT INTO finding_outcome
                           (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path,
                            line_hash, source_event, tip_sha, outcome, outcome_source)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'exec_real_prior','exec_verified')
                       ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source,
                                    source_event) DO NOTHING
                       RETURNING id""",
                    _new_id(), finding_key, f["repo_id"], f["ref"], f["rule_tag"], reviewer_family,
                    f["path"], f["line_hash"], source_event, tip_sha)
        return {"written": 1 if ins is not None else 0, "finding_key": finding_key}

    async def propose_outcome(self, finding_key: str, reviewer_family: str, verdict: str,
                              *, principal_role: str, play: str) -> dict:
        """LABELER service identity ONLY (PR-3 panel sweep). verdict ∈ real|fp ->
        panel_proposed/(panel_real|panel_fp) — a PROPOSAL: never gates, never removes from the queue.
        Server-mints source_event 'panel:<play>'. Returns {"written": 0|1, "finding_key": ...}."""
        if principal_role != "labeler":
            raise PermissionError(f"propose_outcome is labeler only, not {principal_role!r}")
        outcomes = {"real": "panel_real", "fp": "panel_fp"}
        if verdict not in outcomes:
            raise ValueError(f"propose_outcome: bad verdict {verdict!r} (expected real|fp)")
        outcome = outcomes[verdict]
        source_event = f"panel:{play}"
        async with self._pool.acquire() as con:
            async with con.transaction():
                f = await self._finding_fields(con, finding_key, reviewer_family)
                if f is None:
                    return {"error": "no such finding", "finding_key": finding_key}
                ins = await con.fetchval(
                    """INSERT INTO finding_outcome
                           (id, finding_key, repo_id, ref, rule_tag, reviewer_family, path,
                            line_hash, source_event, tip_sha, outcome, outcome_source)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'panel_proposed')
                       ON CONFLICT (finding_key, reviewer_family, outcome, outcome_source,
                                    source_event) DO NOTHING
                       RETURNING id""",
                    _new_id(), finding_key, f["repo_id"], f["ref"], f["rule_tag"], reviewer_family,
                    f["path"], f["line_hash"], source_event, f["tip_sha"], outcome)
        return {"written": 1 if ins is not None else 0, "finding_key": finding_key}

    # ---- knowledge (the curator rung's derived FTS index) — docs/curator-design.md v0.4 ---------
    # Append-only + computed views (finding_outcome's discipline). Files are the source of truth;
    # these verbs keep the DERIVED index in step. Per-scope mutual exclusion = a 2-int advisory
    # lock (constant, hashtext(scope)) so concurrent ingests/curates serialize per corpus, not
    # globally. Idempotency = compare-current-before-insert (NOT a unique index — that would ghost
    # a restore-after-archive of identical content).

    _KNOWLEDGE_LOCK_KEY = 0x6D78724B    # 'mxrK' (int4) — key1 of the (key1, hashtext(scope)) pair

    # The walk fence (0016) = knowledge_scope_gen.gen + MAX(seq), NOT MAX(seq) alone: a seq-based
    # fence only moves when a row is inserted, so an accepted TRUE NO-OP sync (file created then
    # deleted before either walker commits) was invisible to it and a stale walk could resurrect
    # the deleted doc (review 20260910200253 P2). The gen bump runs on EVERY accepted
    # sync/rebuild, inside the same transaction, after the conflict gate — a rejected walk still
    # mutates nothing. WHY THE SUM (r1 kilabz P2, the mixed-deploy window): a pre-0016 writer
    # still running through a rolling cutover advances seq but never touches gen — gen alone is
    # blind to its commits, the sum is not. Both terms are monotonic (gen only bumps;
    # knowledge_doc is append-only, no DELETE path), so any accepted commit from EITHER vintage
    # strictly increases the sum and a stamped fence can never recur. gen starts at 0 (no seed),
    # making the fence VALUE numerically continuous with the old MAX(seq) fence across the
    # upgrade in both directions.
    # ASYMMETRY (r2 oracle LOW): the protection is one-way. A PRE-0016 walker checks MAX(seq)
    # only, so it is blind to a new-code TRUE NO-OP commit (gen bump, no row) and can still
    # commit a stale snapshot — that is exactly the original bug, alive in old code until the
    # old processes exit. Nothing new-side can widen an old binary's check; keep the mixed
    # window short (deploys kickstart serve, so the residual is in-flight pre-pull CLI calls).
    _KNOWLEDGE_GEN_SQL = (
        "SELECT COALESCE((SELECT gen FROM knowledge_scope_gen WHERE scope = $1), 0)"
        " + (SELECT COALESCE(MAX(seq), 0) FROM knowledge_doc WHERE scope = $1)")
    _KNOWLEDGE_GEN_BUMP_SQL = (
        "INSERT INTO knowledge_scope_gen (scope, gen) VALUES ($1, 1) "
        "ON CONFLICT (scope) DO UPDATE SET gen = knowledge_scope_gen.gen + 1")

    @staticmethod
    def _knowledge_date(iso: Optional[str]) -> Optional[_dt.date]:
        """Defensive ISO->date: a filename like 2026-13-99-x.md parses the REGEX but not a real
        date — store NULL rather than crash the ingest."""
        if not iso:
            return None
        try:
            return _dt.date.fromisoformat(iso)
        except ValueError:
            return None

    async def _insert_active_knowledge_doc(self, con, scope: str, d: dict) -> bool:
        """INSERT one active knowledge_doc row inside a SAVEPOINT. Returns True if stored, False if
        SKIPPED because its body yields a >1MB tsvector — the GENERATED tsv column's hard limit, which
        the body BYTE cap does NOT bound for token-dense content. The savepoint stops one such poison
        doc from aborting the WHOLE one-transaction corpus sync (which would re-hit it every re-run and
        permanently wedge the derived index for the scope). ONLY sqlstate 54000 is swallowed; anything
        else re-raises. (A doc that was indexed fine before and only now oversizes keeps its last-good
        active row — stale-but-present beats index-wide failure.)"""
        try:
            async with con.transaction():                     # nested -> SAVEPOINT: rollback here, outer xact lives
                await con.execute(
                    """INSERT INTO knowledge_doc
                           (id, scope, path, title, tags, doc_date, body, content_sha, status, lossy)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active',$9)""",
                    _new_id(), scope, d["path"], d.get("title", ""), d.get("tags", ""),
                    self._knowledge_date(d.get("doc_date")), d.get("body", ""),
                    d["content_sha"], bool(d.get("lossy")))
            return True
        except asyncpg.PostgresError as e:
            if getattr(e, "sqlstate", None) == "54000":       # program_limit_exceeded: tsvector too long
                return False
            raise

    async def knowledge_fence(self, scope: str) -> int:
        """Read the scope's commit fence: generation counter + MAX(seq) (0 = never committed).
        A walker stamps this BEFORE walking the corpus; knowledge_sync (given expect_fence) then
        REJECTS the commit if any other sync/rebuild was ACCEPTED in between — the walked
        snapshot is stale and could resurrect a just-deleted doc (the walk-before-lock TOCTOU).
        The fence moves on every accepted commit INCLUDING true no-ops (which MAX(seq) alone
        missed) AND on row-writing commits from pre-0016 code that never bumps gen (which gen
        alone would miss — the mixed-deploy window). The caller re-walks and retries; a reject
        never mutates anything."""
        async with self._pool.acquire() as con:
            return int(await con.fetchval(self._KNOWLEDGE_GEN_SQL, scope))

    async def knowledge_sync(self, scope: str, docs: list[dict],
                             *, tombstone_missing: bool = True,
                             expect_fence: Optional[int] = None) -> dict:
        """Bring the derived index in step with a walked corpus: INSERT a new event row per
        changed/new doc, tombstone rows for docs gone from disk, skip unchanged (idempotent —
        a re-run inserts nothing). `docs`: knowledge.DocRecord dicts. All appends, in ONE
        transaction under the per-scope advisory xact lock.

        expect_fence: the knowledge_fence() value read BEFORE the corpus walk. If another
        writer committed since (fence moved), returns {"conflict": True} WITHOUT writing —
        arrival order can differ from walk order, and committing a stale walk can resurrect a
        just-deleted doc. None preserves the unfenced behavior (single-writer callers)."""
        inserted = tombstoned = unchanged = 0
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))",
                                  self._KNOWLEDGE_LOCK_KEY, scope)
                if expect_fence is not None:
                    cur_fence = await con.fetchval(self._KNOWLEDGE_GEN_SQL, scope)
                    if int(cur_fence) != int(expect_fence):
                        return {"conflict": True, "inserted": 0, "tombstoned": 0,
                                "unchanged": 0, "skipped_oversize": []}
                # ACCEPTED (fenced or not): bump the scope generation so even a true no-op
                # commit moves the fence — the conflict return above is the only skip.
                await con.execute(self._KNOWLEDGE_GEN_BUMP_SQL, scope)
                cur = {r["path"]: (r["content_sha"], r["status"]) for r in await con.fetch(
                    "SELECT path, content_sha, status FROM knowledge_doc_current WHERE scope = $1",
                    scope)}
                seen: set[str] = set()
                skipped: list[str] = []
                for d in docs:
                    seen.add(d["path"])
                    prev = cur.get(d["path"])
                    if prev == (d["content_sha"], "active"):
                        unchanged += 1
                        continue
                    if await self._insert_active_knowledge_doc(con, scope, d):
                        inserted += 1
                    else:
                        skipped.append(d["path"])             # oversize tsvector: kept OUT of the index, not fatal
                        if prev and prev[1] == "active":
                            # a doc that indexed fine BEFORE and only now oversizes would keep its old
                            # active row and recall would silently serve STALE content (kilabz HIGH).
                            # Archive it so "skipped" genuinely means not-recallable, not stale-recallable.
                            await con.execute(
                                """INSERT INTO knowledge_doc
                                       (id, scope, path, body, content_sha, status)
                                   VALUES ($1,$2,$3,'','absent','archived')""",
                                _new_id(), scope, d["path"])
                            tombstoned += 1
                if tombstone_missing:
                    for path, (_, status) in cur.items():
                        if status == "active" and path not in seen:
                            await con.execute(
                                """INSERT INTO knowledge_doc
                                       (id, scope, path, body, content_sha, status)
                                   VALUES ($1,$2,$3,'','absent','archived')""",
                                _new_id(), scope, path)
                            tombstoned += 1
        return {"inserted": inserted, "tombstoned": tombstoned, "unchanged": unchanged,
                "skipped_oversize": skipped}

    async def knowledge_rebuild(self, scope: str, docs: list[dict],
                                *, expect_fence: Optional[int] = None) -> dict:
        """The admin rebuild (mxr knowledge-rebuild): tombstone + re-ingest under ONE xact lock so
        recall never observes an empty active index between the phases (kilabz MINOR). All appends,
        never TRUNCATE. `docs` are the freshly-walked knowledge.DocRecord dicts. expect_fence:
        same walk-staleness reject as knowledge_sync (rebuild walks before locking too)."""
        tombstoned = inserted = 0
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))",
                                  self._KNOWLEDGE_LOCK_KEY, scope)
                if expect_fence is not None:
                    cur_fence = await con.fetchval(self._KNOWLEDGE_GEN_SQL, scope)
                    if int(cur_fence) != int(expect_fence):
                        return {"conflict": True, "tombstoned": 0, "inserted": 0,
                                "skipped_oversize": []}
                # same accepted-commit bump as knowledge_sync — a no-op rebuild moves the fence
                await con.execute(self._KNOWLEDGE_GEN_BUMP_SQL, scope)
                for r in await con.fetch(
                        "SELECT path FROM knowledge_doc_active WHERE scope = $1", scope):
                    await con.execute(
                        """INSERT INTO knowledge_doc
                               (id, scope, path, body, content_sha, status)
                           VALUES ($1,$2,$3,'','absent','archived')""",
                        _new_id(), scope, r["path"])
                    tombstoned += 1
                skipped: list[str] = []
                for d in docs:
                    if await self._insert_active_knowledge_doc(con, scope, d):
                        inserted += 1
                    else:
                        skipped.append(d["path"])             # oversize tsvector: unindexable, skip (don't wedge)
        return {"tombstoned": tombstoned, "inserted": inserted, "skipped_oversize": skipped}

    async def knowledge_recall_fts(self, scope: str, query: str, k: int) -> list[dict]:
        """Ladder rung 1: websearch_to_tsquery (never raises on arbitrary text — the right entry
        point for LLM-issued queries). ts_rank_cd(...,1) = cover density with mild length damping;
        ts_headline ONLY over the top-k subselect (computing it pre-LIMIT re-parses every row —
        the classic FTS perf bug)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT path, title, doc_date, lossy, rank,
                          ts_headline('english', body, q,
                                      'MaxWords=30, MinWords=10, MaxFragments=2') AS headline
                     FROM (SELECT path, title, doc_date, lossy, body, q,
                                  ts_rank_cd(tsv, q, 1) AS rank
                             FROM knowledge_doc_active,
                                  websearch_to_tsquery('english', $2) AS q
                            WHERE scope = $1 AND tsv @@ q
                            ORDER BY rank DESC, path
                            LIMIT $3) hits""",
                scope, query, k)
        return [dict(r) for r in rows]

    async def knowledge_recall_prefix(self, scope: str, tokens: list[str], k: int) -> list[dict]:
        """Ladder rung 2 (zero FTS hits): prefix-match sanitized tokens — to_tsquery is the only
        parser that can express `tok:*`, and it ONLY ever sees knowledge.prefix_tokens output
        (conservative charset), never raw query text."""
        if not tokens:
            return []
        tsq = " & ".join(f"{t}:*" for t in tokens)
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT path, title, doc_date, lossy, rank,
                          ts_headline('english', body, q,
                                      'MaxWords=30, MinWords=10, MaxFragments=2') AS headline
                     FROM (SELECT path, title, doc_date, lossy, body, q,
                                  ts_rank_cd(tsv, q, 1) AS rank
                             FROM knowledge_doc_active,
                                  to_tsquery('english', $2) AS q
                            WHERE scope = $1 AND tsv @@ q
                            ORDER BY rank DESC, path
                            LIMIT $3) hits""",
                scope, tsq, k)
        return [dict(r) for r in rows]

    async def knowledge_recall_or(self, scope: str, tokens: list[str], k: int) -> list[dict]:
        """BROADEN rung (ask/librarian only): OR the sanitized prefix tokens (`t1:* | t2:* | …`) so a
        doc matching ANY significant term surfaces, ranked by ts_rank_cd cover-density. The precision
        ladder (FTS/prefix AND-match) is right for `mxr recall`; a natural-language QUESTION drops a
        word the doc lacks and AND-misses, so the librarian falls back to OR-recall + LLM relevance
        filtering. Tokens come from knowledge.prefix_tokens (conservative charset), never raw text."""
        if not tokens:
            return []
        tsq = " | ".join(f"{t}:*" for t in tokens)
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """SELECT path, title, doc_date, lossy, rank,
                          ts_headline('english', body, q,
                                      'MaxWords=30, MinWords=10, MaxFragments=2') AS headline
                     FROM (SELECT path, title, doc_date, lossy, body, q,
                                  ts_rank_cd(tsv, q, 1) AS rank
                             FROM knowledge_doc_active,
                                  to_tsquery('english', $2) AS q
                            WHERE scope = $1 AND tsv @@ q
                            ORDER BY rank DESC, path
                            LIMIT $3) hits""",
                scope, tsq, k)
        return [dict(r) for r in rows]

    async def knowledge_recall_ilike(self, scope: str, pattern: str, k: int) -> list[dict]:
        """Ladder rung 3 (still nothing): substring over title+body — catches code tokens/paths
        (`play-review`, `mxr`) that FTS lexemes structurally can't. Pattern comes pre-escaped from
        knowledge.ilike_pattern. Seq scan is fine at this scale; pg_trgm is the recorded upgrade."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                r"""SELECT path, title, doc_date, lossy,
                           NULL::float AS rank, left(body, 200) AS headline
                      FROM knowledge_doc_active
                     WHERE scope = $1
                       AND (title ILIKE $2 ESCAPE '\' OR body ILIKE $2 ESCAPE '\')
                     ORDER BY doc_date DESC NULLS LAST, path
                     LIMIT $3""",
                scope, pattern, k)
        return [dict(r) for r in rows]

    def knowledge_scope_lock(self, scope: str) -> "_KnowledgeScopeLock":
        """Session-scoped per-corpus mutex for the curate PROMOTE window (filesystem + git writes
        coordinate through the SAME advisory key pair the sync verbs use, so a promote and an
        ingest can never interleave). Usage: `async with led.knowledge_scope_lock(scope): ...`.
        NOT held across the LLM wait — design v0.4 lock discipline."""
        return _KnowledgeScopeLock(self._pool, self._KNOWLEDGE_LOCK_KEY, scope)


class _KnowledgeScopeLock:
    """Holds one pooled connection with pg_advisory_lock(key, hashtext(scope)) for the duration
    of the `async with` block; unlock + release are guaranteed on exit."""

    def __init__(self, pool: asyncpg.Pool, key: int, scope: str):
        self._pool, self._key, self._scope = pool, key, scope
        self._con: Optional[asyncpg.Connection] = None

    async def __aenter__(self) -> "_KnowledgeScopeLock":
        self._con = await self._pool.acquire()
        try:
            await self._con.execute("SELECT pg_advisory_lock($1, hashtext($2))",
                                    self._key, self._scope)
        except Exception:
            await self._pool.release(self._con)
            self._con = None
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._con is not None:
            try:
                await self._con.execute("SELECT pg_advisory_unlock($1, hashtext($2))",
                                        self._key, self._scope)
            finally:
                await self._pool.release(self._con)
                self._con = None
