"""The Command API - the SOLE writer to the ledger (DESIGN.md S4b).

Transports, workers, controllers, and interfaces call these verbs; NOBODY
writes raw tables. Each verb is exactly one transaction. The signatures encode
the state-transition table from the design. Implementation (asyncpg + FastAPI)
is the next build phase - this file is the contract the implementation must meet.
"""
from __future__ import annotations

from typing import Optional, Protocol
from uuid import UUID

from runtime.contracts import Result, TransportEnvelope


class CommandAPI(Protocol):
    # -- ingest / dispatch --
    async def ingest_inbound(self, envelope: TransportEnvelope, body: str) -> UUID:
        """transport: create an inbound_event. Dedupe on envelope.dedupe_key."""
        ...

    async def submit_job(
        self, *, to_agent: str, prompt: str,
        context: Optional[dict] = None,
        parent_id: Optional[UUID] = None,
        inbound_event_id: Optional[UUID] = None,
        created_by: str = "human",
        repo_id: Optional[str] = None, base_ref: Optional[str] = None,
        priority: int = 0,
    ) -> UUID:
        """controller|interface: run admission checks (max_depth / max_children /
        cost_budget(root) / chain_ttl) then queue a job. Rejected -> dead_letter.
        `inbound_event_id` links a transport-originated job to its event so any
        descendant's reply can resolve transport+reply_target; `created_by` is the
        provenance (agent_id | 'human' | inbound_event id)."""
        ...

    # -- worker lifecycle --
    async def lease_job(self, worker_id: str, capabilities: list[str]) -> Optional[UUID]:
        """worker: atomically lease one queued job it can serve; open an attempt;
        set lease_expires_at. Returns the attempt id, or None if nothing queued."""
        ...

    async def heartbeat_attempt(self, attempt_id: UUID) -> None:
        """worker: extend the lease so a long job isn't reclaimed as crashed."""
        ...

    async def complete_attempt(self, attempt_id: UUID, result: Result) -> None:
        """worker: attempt ok -> job done; persist result + artifact_ref."""
        ...

    async def fail_attempt(self, attempt_id: UUID, result: Result) -> None:
        """worker: attempt failed -> requeue (only if authority-safe; workspace-actors
        never auto-retry) or mark failed/dead per error_class."""
        ...

    async def append_log(self, attempt_id: UUID, stream: str, chunk: str) -> None:
        """worker: progress visibility (stdout|stderr|heartbeat) -> attempt_log,
        off the hot state machine. Long jobs are never dark."""
        ...

    # -- outbox (reliable delivery) --
    async def enqueue_outbound(self, job_id: UUID, body: str) -> UUID:
        ...

    async def claim_outbound(self, transport: str) -> Optional[dict]:
        """transport: claim one pending reply for delivery -> {id, reply_target, body}."""
        ...

    async def mark_outbound_sent(self, outbound_id: UUID, provider_msg_id: str) -> None:
        ...

    async def mark_outbound_failed(self, outbound_id: UUID) -> None:
        ...

    # -- janitor / control --
    async def reclaim_expired(self) -> int:
        """spine: requeue (or dead-letter) jobs whose lease expired (crashed workers).
        Returns count reclaimed."""
        ...

    async def dead_letter(self, source_id: UUID, reason: str) -> None:
        ...

    async def cancel(self, job_id: UUID) -> None:
        """controller|interface: terminate a non-terminal job; kill its running attempt."""
        ...

    async def get_status(self, job_id: UUID) -> dict:
        ...
