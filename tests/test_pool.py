"""Concurrent worker pool proofs (against real Postgres). This is what the
lease/reclaim machinery was built for:

  1. N workers drain a queue with NO double-processing (one OK attempt per job).
  2. A crashed worker's job is recovered by the janitor and finished by another.
  3. A healthy long job survives via heartbeats (NOT reclaimed).

Setup:  brew services start postgresql@16 && createdb runtime_test
Run:    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
            PYTHONPATH=src python3 tests/test_pool.py
"""
import asyncio
import inspect
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from runtime.contracts import Authority, Reach
from runtime.ledger.postgres_store import PostgresLedger
from runtime.pool import FAULT_BACKOFF_CAP_S, WorkerPool
from runtime.registry import REGISTRY, AgentSpec

# isolate the (now stable, shared-by-default) worktree GC root for this suite
os.environ.setdefault("MYNDAIX_WORKTREE_ROOT", tempfile.mkdtemp(prefix="mdx-test-wt-"))

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")


def _register():
    REGISTRY["pool-fast"] = AgentSpec(
        agent_id="pool-fast", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="fast responder",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "done %s"]})
    REGISTRY["pool-slow"] = AgentSpec(
        agent_id="pool-slow", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow responder",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 0.8; printf done"]})


async def _fresh() -> PostgresLedger:
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    return led


async def test_pool_concurrent_exactly_once():
    _register()
    led = await _fresh()
    try:
        n = 40
        for i in range(n):
            await led.submit_job(to_agent="pool-fast", prompt=f"job {i}")
        pool = WorkerPool(led, size=8)
        processed = await pool.run_until_idle()
        assert processed == n, f"processed {processed}, expected {n}"
        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
            ok = await con.fetchval("SELECT count(*) FROM attempt WHERE status='ok'")
            workers = await con.fetchval("SELECT count(DISTINCT worker_id) FROM attempt")
        assert done == n, f"done={done}, expected {n}"
        assert ok == n, f"ok attempts={ok}, expected {n} (no double-processing)"
        assert workers > 1, f"expected concurrent workers, only {workers} did work"
    finally:
        await led.close()


async def test_pool_cap_binds_at_size_8():
    """PR-3 gate: with pool=8 > MAX_PER_REPO, the per-repo cap actually BINDS. A single
    flooded repo never runs more than the cap of the 8 workers AT ONCE (sampled LIVE
    during the drain, not just post-hoc), while a cold repo still drains (not starved).
    This is what the cap buys that pool=4 (<= cap) couldn't exercise."""
    _register()
    led = await _fresh()
    led.MAX_PER_REPO = 3
    try:
        for i in range(12):
            await led.submit_job(to_agent="pool-slow", prompt=f"hot {i}", repo_id="hot")
        for i in range(4):
            await led.submit_job(to_agent="pool-slow", prompt=f"cold {i}", repo_id="cold")
        pool = WorkerPool(led, size=8, janitor_interval_s=0.1)
        await pool.start()
        max_hot = 0
        try:
            for _ in range(500):                          # sample up to ~10s
                async with led._pool.acquire() as con:
                    hot_open = await con.fetchval(
                        "SELECT count(*) FROM attempt a JOIN job j ON j.id=a.job_id "
                        "WHERE a.status='open' AND j.repo_id='hot' "
                        "AND j.status IN ('leased','running')")
                    remaining = await con.fetchval(
                        "SELECT count(*) FROM job WHERE status IN ('queued','leased','running')")
                max_hot = max(max_hot, hot_open)
                if remaining == 0:
                    break
                await asyncio.sleep(0.02)
        finally:
            await pool.stop()
        assert max_hot <= 3, f"CAP BREACHED at pool=8: saw {max_hot} concurrent on 'hot' (max 3)"
        assert max_hot >= 2, f"cap never engaged (only {max_hot} concurrent on 'hot') — pool not 8-wide?"
        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
            cold_done = await con.fetchval(
                "SELECT count(*) FROM job WHERE repo_id='cold' AND status='done'")
            workers = await con.fetchval("SELECT count(DISTINCT worker_id) FROM attempt")
        assert done == 16, f"not all jobs drained: {done}/16"
        assert cold_done == 4, f"cold repo starved by the capped hot repo: {cold_done}/4 done"
        assert workers > 3, f"pool didn't use >cap workers ({workers}) — the cap can't have bound"
    finally:
        await led.close()


async def test_pool_recovers_crashed_worker():
    _register()
    led = await _fresh()
    try:
        jid = await led.submit_job(to_agent="pool-fast", prompt="recover me")
        # a worker leases it then 'crashes' (never completes); force its lease expired
        att = await led.lease_job("crasher", [])
        assert att is not None
        async with led._pool.acquire() as con:
            await con.execute(
                "UPDATE attempt SET lease_expires_at = statement_timestamp() - interval '1 second' "
                "WHERE id=$1", att)
        # the pool's janitor reclaims it -> requeues -> a healthy worker finishes it
        pool = WorkerPool(led, size=2, janitor_interval_s=0.1)
        await pool.run_until_idle(quiet_s=0.6)
        st = await led.get_status(jid)
        assert st["status"] == "done", f"crashed job not recovered: {st['status']}"
        assert pool.reclaimed >= 1, f"janitor should have reclaimed >=1, got {pool.reclaimed}"
    finally:
        await led.close()


async def test_pool_heartbeat_keeps_long_job():
    _register()
    led = await _fresh()
    led.LEASE_SECONDS = 0.4         # short lease: a 0.8s job WOULD be reclaimed...
    led.HEARTBEAT_SECONDS = 0.6     # ...but each heartbeat extends it
    try:
        jid = await led.submit_job(to_agent="pool-slow", prompt="long job")
        pool = WorkerPool(led, size=1, janitor_interval_s=0.1, heartbeat_interval_s=0.12)
        await pool.run_until_idle(quiet_s=0.5)
        st = await led.get_status(jid)
        assert st["status"] == "done", f"long job status={st['status']}"
        async with led._pool.acquire() as con:
            attempts = await con.fetchval("SELECT count(*) FROM attempt WHERE job_id=$1", jid)
        assert attempts == 1, f"heartbeat should keep ONE attempt, got {attempts}"
        assert pool.reclaimed == 0, f"heartbeat should prevent reclaim, got {pool.reclaimed}"
    finally:
        await led.close()


def _init_repo() -> str:
    d = tempfile.mkdtemp(prefix="mdx-poolrepo-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    Path(d, "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


# -- a poison job (bad binary) must NOT kill the fleet (the P0 the suite missed) --
async def test_pool_survives_bad_argv():
    _register()
    REGISTRY["pool-badbin"] = AgentSpec(
        agent_id="pool-badbin", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="bad binary",
        adapter={"kind": "cli", "prompt_channel": "arg",
                 "argv": ["mdx-nonexistent-binary-zzz"]})
    led = await _fresh()
    try:
        good = 5
        for i in range(good):
            await led.submit_job(to_agent="pool-fast", prompt=f"good {i}")
        poison = await led.submit_job(to_agent="pool-badbin", prompt="boom")
        pool = WorkerPool(led, size=4)
        await pool.run_until_idle()
        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
        assert done == good, f"good jobs not all done ({done}/{good}) - workers died?"
        assert (await led.get_status(poison))["status"] == "failed"
        assert pool.worker_faults == 0, "a bad binary is the runner's job, not the backstop's"
    finally:
        await led.close()


# -- an exception INSIDE process_attempt (poison adapter) hits the worker backstop --
async def test_pool_survives_poison_adapter():
    _register()
    REGISTRY["pool-poison"] = AgentSpec(
        agent_id="pool-poison", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="malformed adapter",
        adapter={"kind": "cli", "prompt_channel": "arg"})  # no 'argv' -> KeyError
    led = await _fresh()
    try:
        good = 5
        for i in range(good):
            await led.submit_job(to_agent="pool-fast", prompt=f"good {i}")
        poison = await led.submit_job(to_agent="pool-poison", prompt="boom")
        pool = WorkerPool(led, size=4)
        await pool.run_until_idle()
        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
        assert done == good, f"workers died on a poison adapter ({done}/{good})"
        assert (await led.get_status(poison))["status"] == "failed"
        assert pool.worker_faults >= 1, "the backstop should have caught the KeyError"
    finally:
        await led.close()


# -- a workspace-actor that raises after worktree creation must NOT leak the worktree --
async def test_pool_worktree_cleaned_on_failure():
    REGISTRY["pool-bad-ws"] = AgentSpec(
        agent_id="pool-bad-ws", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="broken workspace actor",
        adapter={"kind": "cli", "prompt_channel": "stdin"})  # no argv -> KeyError post-worktree
    led = await _fresh()
    repo = _init_repo()
    try:
        jid = await led.submit_job(to_agent="pool-bad-ws", prompt="edit", repo_id=repo)
        pool = WorkerPool(led, size=2)
        await pool.run_until_idle()
        assert (await led.get_status(jid))["status"] == "failed"
        out = subprocess.run(["git", "worktree", "list"], cwd=repo,
                             capture_output=True, text=True)
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"orphan worktree leaked into the live repo: {out.stdout!r}"
    finally:
        await led.close()


# -- misconfig is rejected loudly, not silently defeated --
async def test_pool_rejects_bad_config():
    led = await _fresh()
    try:
        led.LEASE_SECONDS = 1.0
        bad_hb = False
        try:
            WorkerPool(led, heartbeat_interval_s=0.9)  # > lease/2
        except ValueError:
            bad_hb = True
        assert bad_hb, "should reject heartbeat_interval_s > LEASE_SECONDS/2"
        bad_poll = False
        try:
            await WorkerPool(led, poll_s=0.5).run_until_idle(quiet_s=0.4)  # poll_s >= quiet_s
        except ValueError:
            bad_poll = True
        assert bad_poll, "should reject poll_s >= quiet_s"
    finally:
        await led.close()


async def test_janitor_sweeps_orphan_worktrees_on_cadence():
    """The janitor GCs orphan worktrees off the same loop: it passes the ledger's open
    attempt set + the lease as the age floor, and respects the slow sweep cadence."""
    class FakeLedger:
        LEASE_SECONDS = 120
        async def reapable_attempt_ids(self, min_age_s):
            assert min_age_s == 120.0          # lease forwarded as the grace window
            return {"reap-me"}

    calls = []
    pool = WorkerPool(FakeLedger(), size=1, worktree_sweep_interval_s=0.0)
    pool.wm.sweep = lambda reapable: (calls.append(set(reapable)) or 2)
    await pool._maybe_sweep_worktrees()
    assert calls == [{"reap-me"}], calls         # the ledger's reapable set forwarded to sweep
    assert pool.worktrees_swept == 2
    # within the cadence window it must NOT sweep again
    pool.worktree_sweep_interval_s = 999.0
    calls.clear()
    await pool._maybe_sweep_worktrees()
    assert calls == [], "should not re-sweep before the interval elapses"


async def test_janitor_sweep_skips_ledger_without_support():
    """A ledger lacking reapable_attempt_ids (the sqlite demo store) just skips the sweep."""
    class MinimalLedger:
        pass
    pool = WorkerPool(MinimalLedger(), size=1, worktree_sweep_interval_s=0.0)
    await pool._maybe_sweep_worktrees()              # must not raise
    assert pool.worktrees_swept == 0


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture_pool_log():
    lg = logging.getLogger("runtime.pool")
    cap = _Capture()
    lg.addHandler(cap)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    return cap, lambda: (lg.removeHandler(cap), lg.setLevel(old))


async def test_worker_lease_outage_backs_off_one_traceback_per_streak():
    """A ledger outage must not flood the log (2026-09-22: 33 GB of per-poll tracebacks on the
    Mini). Consecutive LEASE failures back off from poll_s (doubling, capped) and log the full
    traceback only on the first failure of a streak; a clean lease resets the streak, so the
    NEXT outage gets its own traceback."""
    class FlakyLedger:
        # 3 failures, 1 clean lease, 2 failures, then a clean lease that stops the pool
        script = ["err", "err", "err", "ok", "err", "err", "stop"]

        async def lease_job(self, worker_id, caps):
            step = self.script.pop(0)
            if step == "err":
                raise OSError(49, "Can't assign requested address")
            if step == "stop":
                pool._stop.set()
            return None

    pool = WorkerPool(FlakyLedger(), size=1, poll_s=0.01)
    delays = []

    async def fake_pause(d):
        delays.append(round(d, 4))
    pool._pause = fake_pause
    cap, restore = _capture_pool_log()
    try:
        await pool._worker("w0")
    finally:
        restore()
    tracebacks = [r for r in cap.records if r.exc_info]
    assert len(tracebacks) == 2, f"one traceback per outage streak, got {len(tracebacks)}"
    assert delays == [0.02, 0.04, 0.08, 0.02, 0.04], delays   # doubles; resets after recovery
    repeats = [r.getMessage() for r in cap.records if "in a row" in r.getMessage()]
    assert len(repeats) == 3 and all("OSError" in m for m in repeats), repeats
    assert sum("recovered" in r.getMessage() for r in cap.records) == 2
    assert pool.worker_faults == 5


async def test_janitor_reclaim_outage_logs_one_traceback_per_streak():
    """Flood guard for the janitor's reclaim loop: one full traceback per outage streak, one-line
    repeats, one recovery line. (No backoff here by design — see the next test.)"""
    class DownLedger:
        failures = 4

        async def reclaim_expired(self):
            if self.failures:
                self.failures -= 1
                raise OSError(49, "Can't assign requested address")
            pool._stop.set()
            return 0

    pool = WorkerPool(DownLedger(), size=1, janitor_interval_s=0.01)
    cap, restore = _capture_pool_log()
    try:
        await pool._janitor()
    finally:
        restore()
    assert len([r for r in cap.records if r.exc_info]) == 1
    assert sum("in a row" in r.getMessage() for r in cap.records) == 3
    assert sum("recovered" in r.getMessage() for r in cap.records) == 1


async def test_fault_backoff_caps_and_never_overflows():
    assert WorkerPool._fault_backoff(0.02, 1) == 0.04
    assert WorkerPool._fault_backoff(0.02, 20) == FAULT_BACKOFF_CAP_S
    assert WorkerPool._fault_backoff(0.02, 10 ** 6) == FAULT_BACKOFF_CAP_S   # no OverflowError
    # review 51857 P2: no streak = the configured cadence, even above the cap (a healthy 300s
    # janitor must not be sped up to 30s), and a streak never retries FASTER than healthy
    assert WorkerPool._fault_backoff(300.0, 0) == 300.0
    assert WorkerPool._fault_backoff(300.0, 3) == 300.0
    assert WorkerPool._fault_backoff(0.2, 0) == 0.2


async def test_run_until_idle_janitor_keeps_cadence_through_faults():
    """Review 51857 P1: a backed-off janitor retry could land after quiet_s — an expired lease is
    invisible to _inflight and the queue count, so the pool declared "done" before the recovering
    reclaim ran. The janitor therefore keeps janitor_interval_s (< quiet_s) through a fault
    streak: two transient failures must not outlast the idle window."""
    class Ledger:
        def __init__(self, failures):
            self.failures = failures
            self.reclaims_ok = 0

        async def lease_job(self, worker_id, caps):
            return None

        async def count_queued(self):
            return 0

        async def reclaim_expired(self):
            if self.failures:
                self.failures -= 1
                raise OSError(49, "Can't assign requested address")
            self.reclaims_ok += 1
            return 0

    led = Ledger(failures=2)                   # at cadence: fail @0, fail @0.2, succeed @0.4
    pool = WorkerPool(led, size=1, janitor_interval_s=0.2)
    await pool.run_until_idle(quiet_s=0.5, max_runtime_s=10.0)
    assert led.reclaims_ok >= 1, "declared idle before the janitor's reclaim recovered"
    assert not pool.truncated


async def test_pause_wakes_on_stop():
    """A capped backoff must not stall shutdown: _pause returns as soon as stop is set."""
    class Nothing:
        pass
    pool = WorkerPool(Nothing(), size=1)
    asyncio.get_running_loop().call_later(0.05, pool._stop.set)
    t0 = asyncio.get_running_loop().time()
    await pool._pause(FAULT_BACKOFF_CAP_S)
    assert asyncio.get_running_loop().time() - t0 < 1.0


async def _main():
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and inspect.iscoroutinefunction(_fn):
            await _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(_main())
