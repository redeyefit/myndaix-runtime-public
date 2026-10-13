"""Terminal transport (C3) - the dumb-pipe boundary, against real Postgres + the
worker pool (the production C3 path). Proves: inbound only queues (never blocks on
the agent); replies are delivered after completion; and - the headline - a slow
agent never blocks the pipe (a fast reply arrives before a slow job finishes).

Setup:  brew services start postgresql@16 && createdb runtime_test
Run:    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
            PYTHONPATH=src python3 tests/test_terminal.py
"""
import asyncio
import inspect
import io
import os

from runtime.contracts import Authority, Reach
from runtime.ledger.postgres_store import PostgresLedger
from runtime.pool import WorkerPool
from runtime.registry import REGISTRY, AgentSpec
from runtime.transport.terminal import TerminalTransport

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")


def _register():
    REGISTRY["term-fast"] = AgentSpec(
        agent_id="term-fast", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="fast responder",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "FAST:%s"]})
    REGISTRY["term-slow"] = AgentSpec(
        agent_id="term-slow", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow responder",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 0.6; printf SLOW"]})


async def _fresh() -> PostgresLedger:
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    return led


async def test_ingest_only_queues_never_blocks():
    _register()
    led = await _fresh()
    try:
        t = TerminalTransport(led, out=io.StringIO())
        jid = await t.ingest("hello", to_agent="term-fast")
        st = await led.get_status(jid)
        assert st["status"] == "queued", f"ingest should only queue, got {st['status']}"
        assert await t.deliver_once() == 0, "nothing should be deliverable before a worker runs"
    finally:
        await led.close()


async def test_delivers_reply_after_completion():
    _register()
    led = await _fresh()
    try:
        out = io.StringIO()
        t = TerminalTransport(led, out=out)
        await t.ingest("ping", to_agent="term-fast")
        await WorkerPool(led, size=2).run_until_idle()       # the pool processes it
        delivered = await t.deliver_once()
        assert delivered == 1, f"expected 1 reply delivered, got {delivered}"
        assert "FAST:ping" in out.getvalue(), f"reply not emitted: {out.getvalue()!r}"
        async with led._pool.acquire() as con:
            sent = await con.fetchval("SELECT count(*) FROM outbound WHERE status='sent'")
        assert sent == 1
    finally:
        await led.close()


async def test_slow_agent_does_not_block_the_pipe():
    """A slow job ingested FIRST must not delay a fast job's reply - the transport
    delivers as work completes, never in submission order. THE C3 property."""
    _register()
    led = await _fresh()
    try:
        out = io.StringIO()
        t = TerminalTransport(led, out=out)
        await t.ingest("slow", to_agent="term-slow")          # 0.6s
        await t.ingest("quick", to_agent="term-fast")         # instant
        stop = asyncio.Event()
        delivery = asyncio.ensure_future(t.run_delivery(stop, poll_s=0.02))
        await WorkerPool(led, size=2).run_until_idle()
        stop.set()
        await delivery
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2, f"expected 2 replies, got {lines}"
        assert "FAST:quick" in lines[0], f"fast reply did not arrive first: {lines}"
        assert "SLOW" in lines[1], f"slow reply not second: {lines}"
    finally:
        await led.close()


async def test_restart_does_not_dedupe_collide():
    """A new TerminalTransport instance (simulating a restart) must not collide its
    dedupe keys with the prior lifetime - two different messages = two events/jobs,
    never a silent dedupe that would misroute a reply (the C3 failure class)."""
    _register()
    led = await _fresh()
    try:
        t1 = TerminalTransport(led, out=io.StringIO())
        await t1.ingest("first", to_agent="term-fast")
        t2 = TerminalTransport(led, out=io.StringIO())   # "restart"
        await t2.ingest("second different", to_agent="term-fast")
        async with led._pool.acquire() as con:
            events = await con.fetchval("SELECT count(*) FROM inbound_event")
            jobs = await con.fetchval("SELECT count(*) FROM job")
        assert events == 2, f"two different messages must be two events, got {events}"
        assert jobs == 2, f"two messages must be two jobs, got {jobs}"
    finally:
        await led.close()


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
