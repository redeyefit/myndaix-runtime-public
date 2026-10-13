"""End-to-end through POSTGRES - the proof that the ledger is WIRED, not just a
tested island. A workspace-actor job flows the full production path:

    ingest_inbound -> submit_job -> worker.drain (lease -> get_attempt_job ->
    isolated git worktree -> runner -> capture diff -> complete_attempt)
    -> enqueue_outbound -> claim_outbound -> mark_outbound_sent

with ALL state in Postgres and the live repo untouched. It uses the SAME
worker.drain() that drives the SQLite demo (test_worker.py) - so this also proves
'swap persistence behind the contract' end-to-end, not just verb-by-verb.

Setup (once):
    brew services start postgresql@16 && createdb runtime_test
Run:
    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
        PYTHONPATH=src python3 tests/test_postgres_e2e.py
"""
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from runtime import worker
from runtime.contracts import Authority, Reach, TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger
from runtime.registry import REGISTRY, AgentSpec

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")


def _init_repo() -> str:
    d = tempfile.mkdtemp(prefix="mdx-e2e-repo-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    Path(d, "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


def _register_fixer():
    REGISTRY["e2e-fixer"] = AgentSpec(
        agent_id="e2e-fixer", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
        model="none", role="deterministic code fixer",
        adapter={"kind": "cli", "prompt_channel": "stdin", "argv": [
            "python3", "-c",
            "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')"]})


async def test_workspace_job_through_postgres():
    _register_fixer()
    repo = _init_repo()
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    try:
        # transport-originated work: ingest the event, submit a job linked to it
        env = TransportEnvelope(transport="terminal", account="acct", sender_id="u1",
                                reply_target="terminal:demo", dedupe_key="e2e-1")
        ev = await led.ingest_inbound(env, "fix the bug in add()")
        jid = await led.submit_job(to_agent="e2e-fixer", prompt="fix the bug in add()",
                                   repo_id=repo, inbound_event_id=ev)

        # the SAME worker that drives the SQLite store
        processed = await worker.drain(led)
        assert processed == 1, f"expected 1 job processed, got {processed}"

        st = await led.get_status(jid)
        assert st["status"] == "done", f"job status={st['status']}"
        # the agent's change is a captured artifact, never merged
        assert st["artifact_ref"] and Path(st["artifact_ref"]).exists()
        assert "return a + b" in Path(st["artifact_ref"]).read_text()
        # the LIVE repo is untouched - the agent worked in an isolated worktree
        assert "return a - b" in Path(repo, "app.py").read_text()

        # the reply was auto-queued by complete (transactional outbox); deliver it
        claimed = await led.claim_outbound("terminal")
        assert claimed is not None, "the reply should be claimable"
        await led.mark_outbound_sent(claimed["id"], "e2e-msg-1")
        st2 = await led.get_status(jid)
        assert any(o["status"] == "sent" for o in (st2["outbound"] or [])), "reply not sent"
    finally:
        await led.close()


if __name__ == "__main__":
    asyncio.run(test_workspace_job_through_postgres())
    print("PASS test_workspace_job_through_postgres")
    print("ALL PASS (1)")
