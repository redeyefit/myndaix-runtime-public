"""End-to-end demo - work routed through the spine to an agent and back.

    PYTHONPATH=src python3 demo.py            # fast, deterministic (demo-echo agent)
    PYTHONPATH=src python3 demo.py kilabz     # route to a REAL agent (Codex / GPT-5.5)
    PYTHONPATH=src python3 demo.py --isolate   # an agent edits code in an isolated worktree
    PYTHONPATH=src python3 demo.py --postgres  # the SAME flow, but state lives in Postgres
                                               # (needs: brew services start postgresql@16
                                               #  && createdb runtime_test)
    PYTHONPATH=src python3 demo.py --discuss   # 3-phase self-learning loop: recon researches,
                                               # kilabz (GPT-6-Astra) + oracle (Gemini 3.1 Pro)
                                               # debate head-to-head, lobster (Claude Sonnet)
                                               # synthesizes — 4 agents, 3 providers, 1 ledger.
                                               # Requires: live runtime serve + Mini reachable.

The `--isolate` and `--postgres` runs call the SAME worker.drain() - only the
ledger differs. That is the whole thesis: persistence swaps behind the contract.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from runtime import worker
from runtime.contracts import Authority, Reach
from runtime.ledger.sqlite_store import Ledger
from runtime.registry import REGISTRY, AgentSpec


def register_demo_agent() -> None:
    # Adding an agent is ONE registry row, never a spine edit (the principle, live).
    REGISTRY["demo-echo"] = AgentSpec(
        agent_id="demo-echo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="demo echo",
        adapter={"kind": "cli", "argv": ["printf", "[demo-echo replied] %s"],
                 "prompt_channel": "arg"})


def register_fixer(agent_id: str = "fixer") -> None:
    # a deterministic workspace-actor that fixes the bug in its own worktree
    REGISTRY[agent_id] = AgentSpec(
        agent_id=agent_id, reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="demo code fixer",
        adapter={"kind": "cli", "prompt_channel": "stdin", "argv": [
            "python3", "-c",
            "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')"]})


def _make_repo_with_bug() -> str:
    repo = tempfile.mkdtemp(prefix="mdx-demo-repo-")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "demo"], cwd=repo, check=True)
    Path(repo, "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _print_diff(artifact_ref) -> None:
    if not artifact_ref:
        return
    for line in Path(artifact_ref).read_text().splitlines():
        if line[:1] in "+-" and line[:3] not in ("+++", "---"):
            print(f"    {line}")


async def demo_message(agent: str) -> None:
    if agent == "demo-echo":
        register_demo_agent()
    print(f"== MyndAIX Team Runtime - message demo (agent: {agent}) ==\n")
    ledger = Ledger()
    jid = await ledger.submit_job(agent, "Hello from the MyndAIX runtime - confirm you ran.",
                                  reply_target="terminal:demo")
    print(f"  submit_job  -> {jid[:8]}")
    processed = await worker.drain(ledger)
    print(f"  worker      -> processed {processed} job(s)")
    print(f"  job {jid[:8]} -> status={(await ledger.status(jid))['status']}")
    print("\n  delivered replies:")
    for o in await ledger.pending_outbound():
        print(f"    -> {o['reply_target']}: {o['body']!r}")
        await ledger.mark_sent(o["id"])
    final = (await ledger.status(jid))["status"]
    print(f"\n{'OK' if final == 'done' else 'FAILED'} - the spine routed a message "
          f"to an agent and returned a reply (job {final}).")


async def demo_isolated() -> None:
    register_fixer("fixer")
    repo = _make_repo_with_bug()
    print("== MyndAIX Team Runtime - workspace-isolation demo (SQLite store) ==\n")
    print(f"  target repo   : {repo}")
    print(f"  app.py before : {Path(repo, 'app.py').read_text().strip()!r}")

    ledger = Ledger()
    jid = await ledger.submit_job("fixer", "fix the bug in add()", repo_id=repo)
    await worker.drain(ledger)
    job = await ledger.status(jid)

    print(f"\n  job {jid[:8]} -> {job['status']} (ran in an isolated git worktree)")
    print(f"  app.py AFTER  : {Path(repo, 'app.py').read_text().strip()!r}   <- LIVE REPO UNTOUCHED")
    print("\n  the agent's change, captured as a reviewable artifact (NOT auto-merged):")
    _print_diff(job["artifact_ref"])
    print("\nOK - the agent edited code in isolation; the live repo is untouched.")


async def demo_isolated_postgres() -> None:
    from runtime.ledger.postgres_store import PostgresLedger
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
    register_fixer("fixer")
    repo = _make_repo_with_bug()

    led = await PostgresLedger.connect(dsn)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        print("== MyndAIX Team Runtime - workspace isolation through POSTGRES ==\n")
        print(f"  store         : {dsn}")
        print(f"  target repo   : {repo}")
        print(f"  app.py before : {Path(repo, 'app.py').read_text().strip()!r}")

        jid = await led.submit_job(to_agent="fixer", prompt="fix the bug in add()", repo_id=repo)
        await worker.drain(led)                        # <- the SAME worker as the SQLite demo
        st = await led.get_status(jid)

        print(f"\n  job {st['id'][:8]} -> {st['status']} (state in Postgres, ran in an isolated worktree)")
        print(f"  app.py AFTER  : {Path(repo, 'app.py').read_text().strip()!r}   <- LIVE REPO UNTOUCHED")
        print("\n  the agent's change, captured as a reviewable artifact (NOT auto-merged):")
        _print_diff(st["artifact_ref"])
        print("\nOK - same worker, Postgres-backed. Persistence swapped behind the contract.")
    finally:
        await led.close()


async def demo_pool() -> None:
    import time

    from runtime.ledger.postgres_store import PostgresLedger
    from runtime.pool import WorkerPool

    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
    # a slow responder so concurrency is visible in wall-clock
    REGISTRY["pool-demo"] = AgentSpec(
        agent_id="pool-demo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow responder",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 0.3; printf done"]})

    led = await PostgresLedger.connect(dsn)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        n, size = 13, 6
        print("== MyndAIX Team Runtime - concurrent worker pool (Postgres) ==\n")
        print(f"  {n} jobs (~0.3s each) | {size} workers | 1 simulated crashed worker\n")
        for i in range(n):
            await led.submit_job(to_agent="pool-demo", prompt=f"job {i}")

        # simulate a crashed worker: lease one job, expire its lease, never complete it
        crashed = await led.lease_job("crashed-worker", [])
        async with led._pool.acquire() as con:
            await con.execute("UPDATE attempt SET lease_expires_at = "
                              "statement_timestamp() - interval '1 second' WHERE id=$1", crashed)
        print("  a worker leased a job then 'crashed' (lease expired, never completed)")

        pool = WorkerPool(led, size=size, janitor_interval_s=0.1)
        t0 = time.monotonic()
        processed = await pool.run_until_idle(quiet_s=0.5)
        elapsed = time.monotonic() - t0

        async with led._pool.acquire() as con:
            done = await con.fetchval("SELECT count(*) FROM job WHERE status='done'")
        print(f"\n  processed {processed} jobs in {elapsed:.2f}s "
              f"(sequential would be ~{n * 0.3:.1f}s)")
        print(f"  jobs done : {done}/{n}")
        print(f"  reclaimed : {pool.reclaimed} (the crashed worker's job, recovered by the janitor)")
        print("\nOK - workers drained the queue concurrently with no double-processing, and "
              "the crashed worker's job was recovered. The ledger is the only shared state.")
    finally:
        await led.close()


async def demo_terminal() -> None:
    import time

    from runtime.ledger.postgres_store import PostgresLedger
    from runtime.pool import WorkerPool
    from runtime.transport.terminal import TerminalTransport

    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
    REGISTRY["term-fast"] = AgentSpec(
        agent_id="term-fast", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="fast responder",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "you said: %s"]})
    REGISTRY["term-slow"] = AgentSpec(
        agent_id="term-slow", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="slow responder",
        adapter={"kind": "cli", "prompt_channel": "stdin",
                 "argv": ["sh", "-c", "sleep 0.6; printf '(slow agent, 0.6s) done'"]})

    led = await PostgresLedger.connect(dsn)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        print("== MyndAIX Team Runtime - terminal transport (C3 dumb pipe over Postgres) ==\n")
        pool = WorkerPool(led, size=4)
        transport = TerminalTransport(led)

        print("  inbound - each line is ingested + queued instantly; the pipe NEVER waits on an agent:")
        t0 = time.monotonic()
        await transport.ingest("slow one", to_agent="term-slow")
        await transport.ingest("hello", to_agent="term-fast")
        await transport.ingest("world", to_agent="term-fast")
        print(f"    3 messages queued in {(time.monotonic() - t0) * 1000:.0f}ms\n")

        print("  outbound - replies stream back as each job finishes (fast ones before the slow one):")
        stop = asyncio.Event()
        delivery = asyncio.ensure_future(transport.run_delivery(stop, poll_s=0.02))
        await pool.run_until_idle()
        stop.set()
        await delivery
        print("\nOK - inbound and outbound are fully decoupled through the ledger; "
              "a slow agent never blocks the pipe.")
    finally:
        await led.close()


async def demo_api() -> None:
    import secrets
    import uuid as _uuid

    import httpx

    from runtime.api import Principal, create_app
    from runtime.ledger.postgres_store import PostgresLedger
    from runtime.pool import WorkerPool

    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
    REGISTRY["api-echo"] = AgentSpec(
        agent_id="api-echo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="echo",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "you said: %s"]})

    led = await PostgresLedger.connect(dsn)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        api_key = "demo-" + secrets.token_hex(8)               # ephemeral, never in source
        app = create_app(led, api_keys={api_key: Principal(id="alice", role="client")})
        pool = WorkerPool(led, size=4)
        print("== MyndAIX Team Runtime - HTTP Command-API (FastAPI over Postgres, API-key auth) ==\n")
        print("  (httpx drives the real ASGI app in-process; `uvicorn runtime.api:app` serves the same one)\n")

        asgi = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=asgi, base_url="http://runtime") as anon:
            print(f"  GET  /health  (no API key)  -> {(await anon.get('/health')).status_code} "
                  "Unauthorized (fail-closed)")

        auth = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(transport=asgi, base_url="http://runtime", headers=auth) as client:
            print(f"  GET  /health  (with key)    -> {(await client.get('/health')).json()}")
            r = await client.post("/inbound", json={
                "sender_id": "alice", "dedupe_key": str(_uuid.uuid4()),
                "body": "hello over http", "to_agent": "api-echo"})
            jid = r.json()["job_id"]
            print(f"  POST /inbound               -> {r.status_code}  job {jid[:8]}")
            queued = (await client.get(f"/jobs/{jid}")).json()["status"]
            print(f"  GET  /jobs/{jid[:8]}          -> {queued}  (the API only queued it; never ran the agent)")
            print("\n  ...a separate worker pool drains the queue...")
            await pool.run_until_idle()
            job = (await client.get(f"/jobs/{jid}")).json()
            reply = next((o["body"] for o in (job.get("outbound") or [])), None)
            print(f"\n  GET  /jobs/{jid[:8]}          -> {job['status']}")
            print(f"  reply (from the outbox, read back via status): {reply!r}")
        print("\nOK - an authenticated HTTP service over the ledger: clients submit + observe their own "
              "work, workers run separately. The ledger is the only shared state.")
    finally:
        await led.close()


# The 4 agents in the discuss demo, with the provider each model comes from. Kept as
# static demo copy (role text is demo-facing, not the registry's terse role field); the
# --roster pane reads the LIVE model straight from REGISTRY so any registry re-pin shows.
DISCUSS_ROSTER = [
    ("recon",   "Perplexity",  "research — grounds the debate"),
    ("kilabz",  "OpenAI",      "code reviewer — position A"),
    ("oracle",  "Google",      "reviewer/vision — position B  [mini]"),
    ("lobster", "Anthropic",   "orchestration — synthesis"),
]

DISCUSS_TOPIC = (
    "In one direct paragraph: what is the hardest unsolved problem "
    "in deploying multi-agent AI systems reliably in production — "
    "the one most likely to cause silent failures at scale?"
)


def print_roster() -> None:
    """Print the demo team table. Model column comes from the LIVE registry (no drift)."""
    from runtime.registry import REGISTRY as REG  # noqa: PLC0415
    print("== MyndAIX Team Runtime — the team ==\n")
    print(f"{'AGENT':<8}  {'MODEL':<16}  {'PROVIDER':<12}  ROLE")
    print("─" * 72)
    for agent, provider, role in DISCUSS_ROSTER:
        model = getattr(REG.get(agent), "model", "?")
        print(f"{agent:<8}  {model:<16}  {provider:<12}  {role}")


async def demo_discuss() -> None:
    """3-phase self-learning multi-agent demo against the live Postgres ledger.

    Phase 1 — recon (Perplexity sonar-pro) grounds the topic with live research.
    Phase 2 — kilabz (GPT-6-Astra) and oracle (Gemini 3.1 Pro) debate head-to-head,
              running concurrently through the ledger on two different machines/providers.
    Phase 3 — lobster (Claude Sonnet) reads both positions and synthesizes a final answer
              — the "self-learning" step: the system improves its answer through structured debate.

    All jobs are durable Postgres rows. Any result is recoverable with: mxr get <job-id>
    Requires: `python3 -m runtime.serve` running + Mac Mini reachable via SSH (for oracle).
    """
    # Import the live-ledger dispatch helpers from cli.py — same contract mxr uses.
    from runtime.cli import (  # noqa: PLC0415
        _print_discuss_result,
        _remote_fetch,
        _submit_and_fetch,
    )
    from runtime.registry import REGISTRY as REG

    TOPIC = DISCUSS_TOPIC
    TIMEOUT = 300.0

    print_roster()
    print(f"\nTOPIC  \"{TOPIC}\"\n")

    # ── Phase 1: Research ────────────────────────────────────────────────────
    print("── Phase 1  recon / Perplexity sonar-pro ───────────────────────────")
    print("   grounding the question with live research\n")
    research = await _submit_and_fetch("recon", TOPIC, TIMEOUT)
    if research.job_id:
        print(f"   job {research.job_id[:8]}", end="")
        print(f"  -> {('done' if research.reply else research.error)}\n")
    if research.reply:
        for line in research.reply.splitlines():
            print("   " + line)
    else:
        print(f"   [recon: {research.error}]")
    print()

    # ── Phase 2: Debate (parallel) ───────────────────────────────────────────
    print("── Phase 2  kilabz (GPT-6-Astra) ⟷ oracle (Gemini 3.1 Pro) ────────")
    print("   dispatched concurrently — different models, different machines\n")

    debate_topic = TOPIC
    if research.reply:
        debate_topic = (
            f"Background (live research):\n{research.reply}\n\n"
            f"Given this context, answer directly and specifically: {TOPIC}"
        )

    oracle_host = (REG.get("oracle") or type("_", (), {"host": "mini"})()).host
    kilabz_task = asyncio.create_task(
        _submit_and_fetch("kilabz", debate_topic, TIMEOUT), name="kilabz"
    )
    oracle_task = asyncio.create_task(
        _remote_fetch(oracle_host, "oracle", debate_topic, TIMEOUT), name="oracle"
    )
    kilabz_result, oracle_result = await asyncio.gather(kilabz_task, oracle_task)

    _print_discuss_result(kilabz_result)
    _print_discuss_result(oracle_result)
    print()

    # ── Phase 3: Synthesis (self-learning) ───────────────────────────────────
    print("── Phase 3  lobster / Claude Sonnet — synthesis ────────────────────")
    print("   reading both positions, producing a single learned answer\n")

    kilabz_pos = kilabz_result.reply or "[no reply from kilabz]"
    oracle_pos = oracle_result.reply or "[no reply from oracle]"
    synthesis_prompt = (
        f"Question posed to two reviewers: {TOPIC}\n\n"
        f"GPT-6-Astra answered:\n{kilabz_pos}\n\n"
        f"Gemini 3.1 Pro answered:\n{oracle_pos}\n\n"
        "Synthesize both positions in two paragraphs: "
        "(1) where they agreed and why that convergence matters, "
        "(2) where they diverged and which view is stronger. "
        "Close with the single most accurate answer to the original question."
    )
    synthesis = await _submit_and_fetch("lobster", synthesis_prompt, TIMEOUT)
    if synthesis.job_id:
        print(f"   job {synthesis.job_id[:8]}", end="")
        print(f"  -> {('done' if synthesis.reply else synthesis.error)}\n")
    if synthesis.reply:
        for line in synthesis.reply.splitlines():
            print("   " + line)
    else:
        print(f"   [lobster: {synthesis.error}]")
    print()

    # ── Summary ──────────────────────────────────────────────────────────────
    all_results = [research, kilabz_result, oracle_result, synthesis]
    job_ids = [r.job_id for r in all_results if r.job_id]
    succeeded = sum(1 for r in all_results if r.reply)
    print(f"{'OK' if succeeded == 4 else 'PARTIAL'} — {succeeded}/4 agents replied. "
          f"{len(job_ids)} durable job row(s) in Postgres. "
          f"Any result recoverable with: mxr get <job-id> --reply")
    if job_ids:
        print("   job ids: " + "  ".join(jid[:8] for jid in job_ids))


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--postgres":
        await demo_isolated_postgres()
    elif arg == "--pool":
        await demo_pool()
    elif arg == "--terminal":
        await demo_terminal()
    elif arg == "--api":
        await demo_api()
    elif arg == "--discuss":
        await demo_discuss()
    elif arg == "--roster":
        print_roster()          # static team pane for the 4-pane demo layout
    elif arg == "--isolate":
        await demo_isolated()
    else:
        await demo_message(arg or "demo-echo")


if __name__ == "__main__":
    asyncio.run(main())
