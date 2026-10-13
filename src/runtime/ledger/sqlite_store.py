"""SQLite-backed ledger for the runnable DEMO (zero external deps).

Production target is Postgres (postgres_store.py). Both stores expose the SAME
async worker-facing surface - `lease_job -> attempt_id`, `get_attempt_job`,
`complete_attempt(attempt_id)`, `fail_attempt(attempt_id)` - so ONE worker
(worker.py) drives either. That is the 'swap persistence behind the contract'
claim, made literal. Methods are `async` purely for interface parity; SQLite
itself is synchronous + in-process underneath (fine for a sequential demo drain).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Optional

from runtime.contracts import Job, LostLease, Result

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
  id TEXT PRIMARY KEY, parent_id TEXT, to_agent TEXT NOT NULL, body TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '{}',
  reply_target TEXT NOT NULL DEFAULT 'demo', repo_id TEXT, base_ref TEXT, artifact_ref TEXT,
  status TEXT NOT NULL DEFAULT 'queued', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS attempt (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, worker_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', result TEXT, error_class TEXT
);
CREATE TABLE IF NOT EXISTS outbound (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, reply_target TEXT NOT NULL,
  body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
);
"""


def _id() -> str:
    return str(uuid.uuid4())


class Ledger:
    """The demo ledger - the same async worker-facing verbs as PostgresLedger."""

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add a column to a PRE-EXISTING demo db file,
        # so self-heal the one column added after this store first shipped. (No-op on a
        # fresh/in-memory db where _SCHEMA already created it.)
        try:
            self.db.execute("ALTER TABLE job ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
            self.db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    # -- dispatch --
    async def submit_job(self, to_agent: str, prompt: str, *, reply_target: str = "demo",
                         context: Optional[dict] = None,
                         parent_id: Optional[str] = None, repo_id: Optional[str] = None,
                         base_ref: Optional[str] = None) -> str:
        jid = _id()
        self.db.execute(
            "INSERT INTO job(id,parent_id,to_agent,body,context,reply_target,repo_id,base_ref) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (jid, parent_id, to_agent, prompt, json.dumps(context or {}),
             reply_target, repo_id, base_ref))
        self.db.commit()
        return jid

    # -- worker lifecycle (the shared WorkerLedger surface) --
    async def lease_job(self, worker_id: str, capabilities: Optional[list[str]] = None) -> Optional[str]:
        """Lease one queued job + open its attempt; return the attempt id (or None)."""
        row = self.db.execute(
            "SELECT id FROM job WHERE status='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
        if row is None:
            return None
        aid = _id()
        self.db.execute("UPDATE job SET status='leased' WHERE id=?", (row["id"],))
        self.db.execute("INSERT INTO attempt(id,job_id,worker_id) VALUES(?,?,?)",
                        (aid, row["id"], worker_id))
        self.db.commit()
        return aid

    async def get_attempt_job(self, attempt_id: str) -> Optional[Job]:
        """The Job a worker should run for this attempt; None if the lease is gone."""
        row = self.db.execute(
            "SELECT j.id, j.to_agent, j.body, j.context, j.repo_id, j.base_ref "
            "FROM job j JOIN attempt a ON a.job_id=j.id "
            "WHERE a.id=? AND a.status='open' AND j.status='leased'", (attempt_id,)).fetchone()
        if row is None:
            return None
        return Job(id=uuid.UUID(row["id"]), to_agent=row["to_agent"], prompt=row["body"],
                   context=json.loads(row["context"] or "{}"),
                   repo_id=row["repo_id"], base_ref=row["base_ref"])

    async def complete_attempt(self, attempt_id: str, result: Result) -> None:
        """attempt open->ok, job->done (+artifact); enqueue the reply. Raises
        LostLease if the attempt is already closed (reclaimed) - same contract as
        the Postgres store, so the worker handles both identically."""
        att = self.db.execute(
            "SELECT job_id FROM attempt WHERE id=? AND status='open'", (attempt_id,)).fetchone()
        if att is None:
            raise LostLease(f"complete: attempt {attempt_id} no longer open")
        job_id = att["job_id"]
        self.db.execute("UPDATE attempt SET status='ok', result=? WHERE id=?",
                        (result.model_dump_json(), attempt_id))
        self.db.execute("UPDATE job SET status='done', artifact_ref=? WHERE id=?",
                        (result.artifact_ref, job_id))
        rt = self.db.execute("SELECT reply_target FROM job WHERE id=?", (job_id,)).fetchone()
        self.db.execute("INSERT INTO outbound(id,job_id,reply_target,body) VALUES(?,?,?,?)",
                        (_id(), job_id, rt["reply_target"], result.text))
        self.db.commit()

    async def fail_attempt(self, attempt_id: str, result: Result) -> None:
        """attempt open->failed, job->failed. (The demo store does not auto-retry;
        authority-gated retry is the Postgres store's job.) Raises LostLease if the
        attempt is already closed (reclaimed) - symmetric with complete_attempt."""
        att = self.db.execute(
            "SELECT job_id FROM attempt WHERE id=? AND status='open'", (attempt_id,)).fetchone()
        if att is None:
            raise LostLease(f"fail: attempt {attempt_id} no longer open")
        ec = result.error_class.value if result.error_class else None
        self.db.execute("UPDATE attempt SET status='failed', result=?, error_class=? WHERE id=?",
                        (result.model_dump_json(), ec, attempt_id))
        self.db.execute("UPDATE job SET status='failed' WHERE id=?", (att["job_id"],))
        self.db.commit()

    # -- outbox / status --
    async def pending_outbound(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbound WHERE status='pending'").fetchall()

    async def mark_sent(self, outbound_id: str) -> None:
        self.db.execute("UPDATE outbound SET status='sent' WHERE id=?", (outbound_id,))
        self.db.commit()

    async def status(self, job_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()

    async def count_queued(self) -> int:
        return self.db.execute("SELECT count(*) FROM job WHERE status='queued'").fetchone()[0]
