"""Terminal transport (C3) - a DUMB PIPE over the ledger.

Inbound:  a line of text -> a normalized TransportEnvelope -> ingest_inbound
          (exactly-once) -> submit_job. `ingest()` returns the job id IMMEDIATELY;
          it NEVER waits for the agent. That non-blocking boundary is the whole
          point - coupling comms to agent work is what froze the prior runtime.

Outbound: a separate loop claims completed replies from the ledger and prints
          them. Completion atomically queued the reply (transactional outbox in
          complete_attempt), so a reply is never lost and the transport only has
          to deliver - it has no idea what an agent is or how long it took.

Interactive use is a thin driver on top: `while True: ingest(input(), ...)` in
one task and `run_delivery(stop)` in another. The demo drives it with a scripted
batch so the decoupling is visible and reproducible.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional
from uuid import UUID, uuid4

from runtime.contracts import TransportEnvelope


class TerminalTransport:
    name = "terminal"

    def __init__(self, ledger, *, account: str = "local", out=None):
        self.ledger = ledger
        self.account = account
        self.out = out if out is not None else sys.stdout

    async def ingest(self, text: str, *, to_agent: str, sender_id: str = "user") -> UUID:
        """Inbound: normalize -> dedupe -> queue. Returns the job id immediately;
        does NOT wait for the agent."""
        env = TransportEnvelope(
            transport=self.name, account=self.account, sender_id=sender_id,
            reply_target=f"{self.name}:{sender_id}",
            # terminal input has no natural idempotency token - every line is a NEW
            # message, so a fresh UUID is the honest dedupe_key: never collides and
            # restart-stable (a per-instance counter would collide after a restart and
            # misroute a new sender's reply through an old envelope - the C3 failure).
            dedupe_key=str(uuid4()))
        event_id = await self.ledger.ingest_inbound(env, text)
        return await self.ledger.submit_job(
            to_agent=to_agent, prompt=text, inbound_event_id=event_id)

    async def deliver_once(self) -> int:
        """Outbound: claim + emit + mark-sent every pending reply for this
        transport. Returns how many were delivered this pass."""
        delivered = 0
        while True:
            msg = await self.ledger.claim_outbound(self.name)
            if msg is None:
                break
            self._emit(msg["reply_target"], msg["body"])
            await self.ledger.mark_outbound_sent(msg["id"], f"{self.name}-{msg['id']}")
            delivered += 1
        return delivered

    async def run_delivery(self, stop: asyncio.Event, *, poll_s: float = 0.03) -> int:
        """Background delivery loop until `stop`, then drain everything still
        pending. Runs concurrently with (and independently of) the workers.

        Shutdown ordering: stop the PRODUCERS (the worker pool) BEFORE signalling
        `stop` here, so delivery outlives them. A reply enqueued after delivery
        stops is NOT lost - it stays durably 'pending' in the ledger for the next
        delivery run; the outbox is the source of truth, not this loop."""
        total = 0
        while not stop.is_set():
            total += await self.deliver_once()
            await asyncio.sleep(poll_s)
        while True:                       # final drain of everything pending at stop
            n = await self.deliver_once()
            total += n
            if n == 0:
                break
        return total

    def _emit(self, reply_target: str, body: str) -> None:
        print(f"  <- [{reply_target}] {body}", file=self.out, flush=True)
