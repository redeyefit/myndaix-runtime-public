"""Concurrent worker pool (DESIGN.md C4) - what the lease/reclaim machinery was
built for. The sequential `worker.drain()` never exercised it; this does.

`size` workers + a janitor all lease off the SAME ledger:
  * FOR UPDATE SKIP LOCKED hands each worker a DIFFERENT job -> no double-process;
  * the janitor periodically `reclaim_expired()`s, so a crashed worker's job is
    requeued and finished by another worker - the pool survives worker death;
  * each worker HEARTBEATS its in-flight job, so a healthy long job is NOT
    reclaimed (only genuinely dead workers lose their lease).

Robustness (a 'dumb supervisor' must survive its workers):
  * a worker NEVER dies on a poison job - any unexpected error is caught, the
    attempt is failed TERMINAL (so it isn't reclaimed forever), and the worker
    keeps going. The runner is also total (spawn/api failures become Results).
  * the janitor survives a transient ledger error (caught + continue).
  * idle is judged against the ledger's real queue depth, not just local counters.
  * shutdown is bounded: stragglers are cancelled after a grace period.

Single-process asyncio (the per-box shape). Horizontal scale = more processes
pointing at the same Postgres - the ledger is the only shared state.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from runtime import worker
from runtime.contracts import ErrorClass, Result, ResultStatus
from runtime.workspace import WorkspaceManager

log = logging.getLogger("runtime.pool")

# Ceiling for the fault backoff below. Without a backoff, a ledger outage at the default
# poll_s=0.02 made 8 workers log ~400 full tracebacks/s — 33 GB of the Mini's serve log
# (2026-09-22). 30s bounds recovery latency once the ledger is back.
FAULT_BACKOFF_CAP_S = 30.0


class WorkerPool:
    def __init__(self, ledger, *, size: int = 4, poll_s: float = 0.02,
                 janitor_interval_s: float = 0.2,
                 heartbeat_interval_s: Optional[float] = None,
                 worktree_sweep_interval_s: float = 300.0,
                 reconcile_interval_s: float = 45.0):
        self.ledger = ledger
        self.size = size
        self.poll_s = poll_s                      # idle worker re-poll delay
        self.janitor_interval_s = janitor_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self.worktree_sweep_interval_s = worktree_sweep_interval_s  # slow GC cadence (PR-1c)
        self.reconcile_interval_s = reconcile_interval_s  # slow per-repo cap reconcile (PR-2)
        # Heartbeat contract (design M4) — two DISTINCT quantities:
        #  (a) HEARTBEAT_SECONDS (the ledger's lease-EXTEND amount) must be >= LEASE_SECONDS
        #      so a single ping fully renews the window, never SHRINKS it; and
        #  (b) heartbeat_interval_s (THIS pool's FIRING cadence) must be <= lease/3 so
        #      >= ~3 pings land per window and one slow/dropped ping can't orphan a live job.
        lease = getattr(ledger, "LEASE_SECONDS", None)
        extend = getattr(ledger, "HEARTBEAT_SECONDS", None)
        if lease and extend is not None and extend < lease:
            raise ValueError(
                f"HEARTBEAT_SECONDS={extend} (lease extend) must be >= LEASE_SECONDS={lease}: "
                "a heartbeat that extends by less than the lease window can't keep a job alive")
        if heartbeat_interval_s and lease and heartbeat_interval_s > lease / 3:
            raise ValueError(
                f"heartbeat_interval_s={heartbeat_interval_s} too large for a {lease}s lease; "
                "fire at <= LEASE_SECONDS/3 so >= ~3 heartbeats land before expiry")
        self.wm = WorkspaceManager()
        self.processed = 0                        # jobs this pool completed
        self.reclaimed = 0                        # leases the janitor reclaimed
        self.worktrees_swept = 0                  # orphan worktrees GC'd by the janitor
        self.worker_faults = 0                    # caught worker errors (none fatal)
        self.truncated = False                    # set if run_until_idle hit the cap
        self.reconciled = 0                       # repo_concurrency rows the janitor healed
        self._stop = asyncio.Event()
        self._inflight = 0
        self._last = 0.0
        self._last_sweep = 0.0
        self._last_reconcile = 0.0
        self._tasks: list = []

    def _touch(self) -> None:
        self._last = time.monotonic()

    async def _queued(self) -> int:
        if hasattr(self.ledger, "count_queued"):
            return await self.ledger.count_queued()
        return 0

    @staticmethod
    def _fault_backoff(base_s: float, streak: int) -> float:
        # No streak = the configured cadence, untouched (a janitor_interval_s above the cap must not
        # be sped up when healthy). In a streak, never retry FASTER than the healthy cadence.
        # Exponent clamped: 2**huge * float raises OverflowError, and 2**30 already exceeds the cap.
        if streak <= 0:
            return base_s
        return max(base_s, min(base_s * (2 ** min(streak, 30)), FAULT_BACKOFF_CAP_S))

    async def _pause(self, delay_s: float) -> None:
        """Sleep that wakes early on stop(), so a capped backoff can't stall a graceful shutdown."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay_s)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _log_fault(streak: int, exc: BaseException, msg: str, *args) -> None:
        # Full traceback on the FIRST fault of a streak only; repeats are one line, because the
        # traceback is already in the log and repeating it per retry is what flooded the disk.
        if streak == 1:
            log.error(msg, *args, exc_info=exc)
        else:
            log.warning(msg + " (%d in a row; %s: %s)", *args, streak, type(exc).__name__, exc)

    async def _worker(self, worker_id: str) -> None:
        lease_faults = 0                          # consecutive lease_job failures (ledger down)
        while not self._stop.is_set():
            attempt_id = None
            try:
                attempt_id = await self.ledger.lease_job(worker_id, [])
                if lease_faults:
                    log.warning("pool worker %s: ledger lease recovered after %d failure(s)",
                                worker_id, lease_faults)
                    lease_faults = 0
                if attempt_id is None:
                    await asyncio.sleep(self.poll_s)
                    continue
                self._inflight += 1
                self._touch()
                try:
                    status = await worker.process_attempt(
                        self.ledger, attempt_id, self.wm, self.heartbeat_interval_s)
                    if status is not None:
                        self.processed += 1
                finally:
                    self._inflight -= 1
                    self._touch()
            except asyncio.CancelledError:
                raise  # shutdown cancellation - let the task end
            except Exception as exc:
                # a poison job (bad adapter, transient ledger error) must NEVER kill
                # the worker. Fail the attempt TERMINAL so it isn't reclaimed forever,
                # then carry on - one bad job can't decimate the fleet.
                self.worker_faults += 1
                if attempt_id is None:
                    # The LEASE itself failed, so nothing was leased and there is no attempt
                    # to fail: the ledger is down or unreachable. Back off and log one
                    # traceback per streak. Poison jobs (attempt_id set) keep the full
                    # traceback each time below — each is a distinct job worth a record.
                    lease_faults += 1
                    self._log_fault(lease_faults, exc,
                                    "pool worker %s: lease failed; backing off", worker_id)
                    await self._pause(self._fault_backoff(self.poll_s, lease_faults))
                    continue
                log.exception("pool worker %s: unexpected error; continuing", worker_id)
                try:
                    await self.ledger.fail_attempt(attempt_id, Result(
                        status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                        text="worker caught an unexpected error"))
                except Exception:
                    log.exception("pool worker %s: could not fail attempt %s",
                                  worker_id, attempt_id)
                await asyncio.sleep(self.poll_s)

    async def _janitor(self) -> None:
        if not hasattr(self.ledger, "reclaim_expired"):
            return
        faults = 0                                # consecutive reclaim_expired failures
        while not self._stop.is_set():
            try:
                n = await self.ledger.reclaim_expired()
                if faults:
                    log.warning("pool janitor: reclaim recovered after %d failure(s)", faults)
                    faults = 0
                if n:
                    self.reclaimed += n
                    self._touch()              # reclaim requeued work; keep the pool alive
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # a transient ledger error must NOT permanently disable reclaim
                # (that would silently revoke the crash-recovery guarantee).
                faults += 1
                self._log_fault(faults, exc, "pool janitor: reclaim_expired failed; continuing")
            await self._maybe_sweep_worktrees()
            await self._maybe_reconcile_cap()
            # Deliberately NO backoff here, unlike the worker: run_until_idle's guard only
            # promises janitor_interval_s < quiet_s, so a backed-off retry could land after the
            # idle exit and strand a crash-recovery reclaim (review 51857). One traceback per
            # streak already cuts the janitor's outage log to one short line per interval.
            await asyncio.sleep(self.janitor_interval_s)

    async def _maybe_sweep_worktrees(self) -> None:
        """Slow-cadence GC of hard-crash orphan worktrees (PR-1c) — runs at most every
        worktree_sweep_interval_s, off the same janitor loop. The blocking git/fs work
        runs in an executor so it never stalls the event loop. Best-effort: a sweep
        error never disables reclaim (the crash-recovery guarantee)."""
        if not hasattr(self.ledger, "reapable_attempt_ids"):
            return
        now = time.monotonic()
        if now - self._last_sweep < self.worktree_sweep_interval_s:
            return
        self._last_sweep = now
        try:
            # 'reapable' = attempts CLOSED for at least one lease — never an open or
            # just-closed lease, so a worktree whose worker may still be writing is safe.
            min_age = float(getattr(self.ledger, "LEASE_SECONDS", 600))
            reapable = await self.ledger.reapable_attempt_ids(min_age)
            n = await asyncio.get_running_loop().run_in_executor(
                None, self.wm.sweep, reapable)
            if n:
                self.worktrees_swept += n
                log.info("pool janitor: swept %d orphan worktree(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pool janitor: worktree sweep failed; continuing")

    async def _maybe_reconcile_cap(self) -> None:
        """Slow-cadence reconcile of the per-repo cap counter (PR-2) — heals
        repo_concurrency.active soft-cache drift back to truth, off the same janitor
        loop. Best-effort + correctness-neutral: the hard COUNT under the rc lock is the
        cap authority, so a reconcile error (or skipping it) never affects the cap, only
        soft-filter fairness."""
        if not hasattr(self.ledger, "reconcile_repo_concurrency"):
            return
        now = time.monotonic()
        if now - self._last_reconcile < self.reconcile_interval_s:
            return
        self._last_reconcile = now
        try:
            n = await self.ledger.reconcile_repo_concurrency()
            if n:
                self.reconciled += n
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pool janitor: cap reconcile failed; continuing")

    async def _shutdown(self, grace_s: float) -> None:
        self._stop.set()
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=grace_s)
        for t in pending:                          # stragglers (a wedged invoke) -> cancel
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for t in done:                             # surface, never swallow, real errors
            if not t.cancelled() and t.exception() is not None:
                log.error("pool task exited with an unexpected error", exc_info=t.exception())
        self._tasks = []

    async def run_until_idle(self, *, quiet_s: float = 0.5, max_runtime_s: float = 30.0,
                             grace_s: float = 5.0) -> int:
        """Spin `size` workers + a janitor, drain the queue concurrently, and stop
        when nothing is in flight, the ledger queue is empty, and there's been no
        activity for `quiet_s`. Returns jobs processed. If `max_runtime_s` fires
        with work remaining, `self.truncated` is set (busy != done)."""
        if any(not t.done() for t in self._tasks):
            raise RuntimeError("pool already running")
        if self.poll_s >= quiet_s:
            raise ValueError(f"poll_s ({self.poll_s}) must be < quiet_s ({quiet_s}) or "
                             "the pool can declare idle during a worker's poll gap")
        if self.janitor_interval_s >= quiet_s:
            raise ValueError(f"janitor_interval_s ({self.janitor_interval_s}) must be < "
                             f"quiet_s ({quiet_s}) or a reclaimable crashed job may be "
                             "missed at the idle check")
        self._stop.clear()
        self._touch()
        self.truncated = False
        self._tasks = [asyncio.ensure_future(self._worker(f"w{i}")) for i in range(self.size)]
        self._tasks.append(asyncio.ensure_future(self._janitor()))
        started = time.monotonic()
        try:
            while True:
                await asyncio.sleep(min(quiet_s / 3, 0.1))
                queued = await self._queued()
                if (self._inflight == 0 and queued == 0
                        and (time.monotonic() - self._last) > quiet_s):
                    break
                if (time.monotonic() - started) > max_runtime_s:
                    self.truncated = True
                    log.warning("pool run_until_idle hit max_runtime_s=%ss with work "
                                "remaining (inflight=%s queued=%s)", max_runtime_s,
                                self._inflight, queued)
                    break
        finally:
            await self._shutdown(grace_s)
        return self.processed

    async def start(self) -> None:
        """Long-running mode: workers + janitor run until stop() (a real service)."""
        if any(not t.done() for t in self._tasks):
            raise RuntimeError("pool already started")
        self._stop.clear()
        self._touch()
        self._tasks = [asyncio.ensure_future(self._worker(f"w{i}")) for i in range(self.size)]
        self._tasks.append(asyncio.ensure_future(self._janitor()))

    async def stop(self, grace_s: float = 5.0) -> None:
        """Graceful shutdown: stop leasing, let in-flight jobs finish within
        grace_s, then cancel stragglers (their process groups are killed)."""
        await self._shutdown(grace_s)
