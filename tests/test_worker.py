"""Worker + C5 isolation over the SQLite store - a workspace-actor job runs in an
isolated worktree, its change is captured as the job's artifact, and the LIVE repo
is untouched. Deterministic (a python-writer agent), no real LLM. Uses the SAME
worker.drain() the Postgres e2e test uses (test_postgres_e2e.py).

Run: PYTHONPATH=src python3 tests/test_worker.py
"""
import asyncio
import inspect
import os
import subprocess
import tempfile
from pathlib import Path

from runtime import worker
from runtime.contracts import Authority, Reach
from runtime.ledger.sqlite_store import Ledger
from runtime.registry import REGISTRY, AgentSpec

# isolate the worktree root: the worker uses WorkspaceManager() with no explicit root,
# whose default is now a STABLE shared dir — give this suite its own so a fixed test
# attempt_id can't collide across runs.
os.environ.setdefault("MYNDAIX_WORKTREE_ROOT", tempfile.mkdtemp(prefix="mdx-test-wt-"))


def _init_repo() -> str:
    d = tempfile.mkdtemp(prefix="mdx-workrepo-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    Path(d, "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


def _register_fixer():
    REGISTRY["test-fixer"] = AgentSpec(
        agent_id="test-fixer", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="deterministic code fixer",
        adapter={"kind": "cli", "prompt_channel": "stdin", "argv": [
            "python3", "-c",
            "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')"]})


async def test_worker_isolates_a_workspace_actor():
    _register_fixer()
    repo = _init_repo()
    ledger = Ledger()

    jid = await ledger.submit_job("test-fixer", "fix the bug in add()", repo_id=repo)
    processed = await worker.drain(ledger)
    assert processed == 1

    job = await ledger.status(jid)
    assert job["status"] == "done"

    artifact = job["artifact_ref"]
    assert artifact and Path(artifact).exists()
    assert "return a + b" in Path(artifact).read_text()           # the agent's change, captured
    assert "return a - b" in Path(repo, "app.py").read_text()      # live repo untouched


async def test_heartbeat_lost_lease_cancels_and_drops():
    """If a heartbeat finds the lease gone mid-run, the agent is cancelled and the
    job is dropped (return None) - never finalized, no double-mutation. No Postgres:
    a fake ledger whose heartbeat reports the lease lost on the first beat."""
    import uuid as _uuid

    from runtime.contracts import Job, LostLease

    REGISTRY["slow-sleeper"] = AgentSpec(
        agent_id="slow-sleeper", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 5; printf done"]})

    class FakeLedger:
        LEASE_SECONDS = 0.2

        def __init__(self):
            self.finalized = False

        async def get_attempt_job(self, attempt_id):
            return Job(id=_uuid.uuid4(), to_agent="slow-sleeper", prompt="x")

        async def heartbeat_attempt(self, attempt_id):
            raise LostLease("reclaimed")

        async def complete_attempt(self, attempt_id, result):
            self.finalized = True

        async def fail_attempt(self, attempt_id, result):
            self.finalized = True

    led = FakeLedger()
    status = await worker.process_attempt(led, "att-1", heartbeat_interval_s=0.05)
    assert status is None, "a lost lease mid-run must return None"
    assert not led.finalized, "must NOT finalize a job whose lease was lost"


async def test_invoke_never_leaks_the_agent_task():
    """_invoke must cancel + reap the heartbeat-wrapped agent on EVERY exit, not
    just LostLease - a non-LostLease heartbeat error (or shutdown cancel) must not
    orphan the subprocess. (Regression for a leak a fix introduced + review caught.)"""
    import uuid as _uuid

    from runtime import worker as _w
    from runtime.contracts import Job

    state = {"cancelled": False}

    async def fake_invoke(agent_id, job):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    class BadLedger:  # heartbeat raises a NON-LostLease error
        async def heartbeat_attempt(self, attempt_id):
            raise RuntimeError("db down")

    orig = _w.runner.invoke
    _w.runner.invoke = fake_invoke
    try:
        job = Job(id=_uuid.uuid4(), to_agent="x", prompt="x")
        raised = False
        try:
            await _w._invoke(BadLedger(), "att", job, 0.01)
        except RuntimeError:
            raised = True
        assert raised, "a non-LostLease heartbeat error should propagate"
        await asyncio.sleep(0.02)
        assert state["cancelled"], "the agent task must be cancelled (not leaked)"
    finally:
        _w.runner.invoke = orig


async def test_sqlite_context_round_trips_to_job():
    """The plumbing that lets a media agent work: Job.context submitted -> persisted ->
    rebuilt for the worker, so the runner can read job.context['image_url']."""
    led = Ledger()
    ctx = {"image_url": "http://example.com/cat.png", "application": "/higgsfield-ai/dop/lite"}
    await led.submit_job("recon", "gen a clip", context=ctx)
    aid = await led.lease_job("w1")
    job = await led.get_attempt_job(aid)
    assert job is not None and job.context == ctx

    # a job submitted with NO context gets an empty dict (never None) - the runner's
    # `job.context.get("image_url")` then returns a clean None, not an AttributeError.
    await led.submit_job("recon", "no media")
    aid2 = await led.lease_job("w1")
    job2 = await led.get_attempt_job(aid2)
    assert job2 is not None and job2.context == {}


async def test_workspace_ops_do_not_block_the_event_loop():
    # core-audit HIGH: the worktree git ops are BLOCKING and used to run on the event loop, freezing
    # every worker + the janitor + all heartbeats until git returned. They now run via asyncio.to_thread
    # — prove a BLOCKING wm.create lets a concurrent coroutine keep progressing (the loop is NOT frozen).
    import time
    _register_fixer()
    repo = _init_repo()
    ledger = Ledger()
    await ledger.submit_job("test-fixer", "x", repo_id=repo)
    attempt_id = await ledger.lease_job("w1", [])

    real = worker.WorkspaceManager()

    class BlockingWM:                                 # a WEDGED git: create() blocks its thread ~0.4s
        def worktree_path(self, *a):
            return real.worktree_path(*a)

        def create(self, *a):
            time.sleep(0.4)
            return real.create(*a)

        def capture_diff(self, *a):
            return real.capture_diff(*a)

        def cleanup(self, *a):
            return real.cleanup(*a)

    ticks = [0]

    async def ticker():
        for _ in range(60):
            ticks[0] += 1
            await asyncio.sleep(0.01)

    t = asyncio.ensure_future(ticker())
    await worker.process_attempt(ledger, attempt_id, wm=BlockingWM())
    ticks_during = ticks[0]
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    # If create() had blocked the loop for its 0.4s, the 0.01s ticker would have made ~0 progress
    # during it. With to_thread the loop kept running -> the ticker advanced well past a handful.
    assert ticks_during >= 10, f"event loop was blocked during the worktree op (ticks={ticks_during})"


async def test_create_failure_still_cleans_up():
    # kilabz: create is now INSIDE the try/finally, so a git failure/timeout during `worktree add`
    # still reaches cleanup — and cleanup gets the DETERMINISTIC path even though create never returned.
    _register_fixer()
    repo = _init_repo()
    ledger = Ledger()
    await ledger.submit_job("test-fixer", "x", repo_id=repo)
    attempt_id = await ledger.lease_job("w1", [])
    real = worker.WorkspaceManager()
    cleaned = []

    class FailingCreateWM:
        def worktree_path(self, aid):
            return real.worktree_path(aid)

        def create(self, *a):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)   # wedged git

        def capture_diff(self, *a):
            return None

        def cleanup(self, repo_path, wt):
            cleaned.append(wt)

    raised = False
    try:
        await worker.process_attempt(ledger, attempt_id, wm=FailingCreateWM())
    except subprocess.TimeoutExpired:
        raised = True
    assert raised, "a create TimeoutExpired propagates out of process_attempt"
    assert cleaned == [real.worktree_path(str(attempt_id))], \
        "cleanup ran with the deterministic worktree path despite create failing"


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
