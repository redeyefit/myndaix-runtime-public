"""Run the MyndAIX worker pool as an always-on service.

This is the operational half of the runtime: a pool of workers that continuously
leases jobs from the Postgres ledger and runs them through the real agent CLIs,
plus a janitor that reclaims crashed leases. Submit work to it with `runtime.cli`
(the `mxr` command). It replaces the prior runtime's agent-dispatch loop with a
durable, crash-recoverable one that can't be wedged by a slow agent.

    createdb runtime && psql runtime < src/runtime/ledger/schema.sql   # one-time
    MYNDAIX_DSN=postgresql://localhost/runtime PYTHONPATH=src python3 -m runtime.serve
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

from runtime.ledger.postgres_store import PostgresLedger
from runtime.pool import WorkerPool

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")


async def serve(size: int = 8) -> None:
    led = await PostgresLedger.connect(DSN)
    # Apply pending migrations BEFORE serving, so a deploy can never start workers
    # against a stale schema (the 2026-06-24 dispatch outage). Idempotent + advisory-
    # locked; raises (and we never come up) if a migration is broken.
    applied = await led.migrate()
    if applied:
        print(f"[serve] schema migrations ensured (idempotent): {', '.join(applied)}",
              file=sys.stderr, flush=True)
    pool = WorkerPool(led, size=size, heartbeat_interval_s=30.0)
    await pool.start()
    print(f"[serve] MyndAIX runtime up: {size}-worker pool draining {DSN}", file=sys.stderr, flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    print("[serve] draining + shutting down...", file=sys.stderr, flush=True)
    await pool.stop()
    await led.close()


def main() -> None:
    size = 8                       # PR-3: pool=8 (the per-repo cap now keeps one repo
                                   # from monopolizing all workers). Override with --size.
    if "--size" in sys.argv:
        size = int(sys.argv[sys.argv.index("--size") + 1])
    asyncio.run(serve(size))


if __name__ == "__main__":
    main()
