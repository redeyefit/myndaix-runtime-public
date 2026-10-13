"""Core contracts for the MyndAIX Team Runtime.

These Pydantic models ARE the contracts (C0-C3) from DESIGN.md. The spine,
workers, transports, and adapters all speak in these types. Persistence lives
in ledger/schema.sql; the Command API (command_api.py) is the only writer.

Nothing here knows *how* an agent does its work - only how it's reached
(reach), what it's allowed to do (authority), and the shape of a job/result.
That separation is the whole point (the prior runtime's failure was coupling).
"""
from __future__ import annotations

import enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# -- C0: capability model -------------------------------------------------
class Reach(str, enum.Enum):
    """How an agent is invoked (drives the adapter + auth/cost)."""
    CLI = "cli"
    API = "api"


class Authority(str, enum.Enum):
    """What an agent may do - drives retry-safety, isolation, dispatch rights.
    This, not reach, is the load-bearing distinction."""
    RESPONDER = "responder"               # prompt->text, no side effects; auto-retry safe
    WORKSPACE_ACTOR = "workspace_actor"   # reads/writes files; gets a worktree; NEVER auto-retried
    CONTROLLER = "controller"             # may emit new dispatches (via Command API only)
    COMPOSITE = "composite"               # multiple internal calls; declares net authority


# margin the CLI's sync wait adds over the agent's exec timeout: covers queue/lease/
# outbound latency around the attempt itself, so the wait can't expire while a
# still-in-budget attempt is finishing.
_SYNC_WAIT_MARGIN_S = 60


class Profile(BaseModel):
    """Cost/concurrency/timeout characteristics - consumed by C4."""
    timeout_s: int = 300
    concurrency_weight: int = 1
    cost_budget: Optional[float] = None   # api agents; None = no $ budget
    sync_wait_s: Optional[int] = None     # cli sync-wait override; None -> derived (sync_wait())

    def sync_wait(self) -> float:
        """How long `mxr` waits synchronously for this agent's reply, when the operator
        set nothing (MXR_TIMEOUT_S always wins — cli.submit). DERIVED from the exec
        timeout + margin unless sync_wait_s pins it, so the two budgets can never be
        hand-tuned apart: kilabz's exec cap is 900s while the CLI default wait was a
        flat 180s, so a slow-but-successful review stranded its DONE reply in the
        ledger looking like a timeout (2026-07-03/06)."""
        if self.sync_wait_s is not None:
            return float(self.sync_wait_s)
        return float(self.timeout_s + _SYNC_WAIT_MARGIN_S)


# -- ledger state-machine enums (C2 / C4) --------------------------------
class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class AttemptStatus(str, enum.Enum):
    OPEN = "open"
    OK = "ok"
    FAILED = "failed"


class OutboundStatus(str, enum.Enum):
    PENDING = "pending"
    LEASED = "leased"
    SENT = "sent"
    FAILED = "failed"


class ResultStatus(str, enum.Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"
    NEEDS_HUMAN = "needs_human"


class ErrorClass(str, enum.Enum):
    RETRYABLE = "retryable"       # transient: network, rate-limit, read-only timeout
    TERMINAL = "terminal"         # bad-auth, validation, non-zero-exit on a mutation
    NEEDS_HUMAN = "needs_human"   # interactive/TTY prompt detected -> park, never loop


class Stream(str, enum.Enum):
    STDOUT = "stdout"
    STDERR = "stderr"
    HEARTBEAT = "heartbeat"


# -- C3: transport envelope ----------------------------------------------
class FormattingCaps(BaseModel):
    max_len: int = 2000
    chunking: bool = True


class TransportEnvelope(BaseModel):
    """Normalized inbound context. Transport semantics live HERE and must never
    leak into Job/agent fields - that leak sank a prior system: a chat platform's
    'group' classification made the bot silently drop replies."""
    transport: str
    account: str
    sender_id: str
    channel_id: Optional[str] = None
    thread_id: Optional[str] = None
    reply_target: str
    provider_msg_id: Optional[str] = None
    dedupe_key: str
    formatting_caps: FormattingCaps = Field(default_factory=FormattingCaps)


# -- C1: invocation contract ---------------------------------------------
class Job(BaseModel):
    """A unit of work handed to invoke(). Workspace fields enable chaining:
    a child job sets base_ref = a prior job's artifact_ref, so its worktree is
    created from the previous step's output instead of the live tree."""
    id: UUID
    to_agent: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    # workspace (workspace-actors only)
    repo_id: Optional[str] = None
    base_ref: Optional[str] = None
    base_sha: Optional[str] = None
    worktree_path: Optional[str] = None
    timeout_s: int = 300
    attempt_no: int = 1


class Result(BaseModel):
    status: ResultStatus
    text: str = ""
    exit_code: Optional[int] = None
    error_class: Optional[ErrorClass] = None
    artifact_ref: Optional[str] = None   # e.g. a branch/patch for a workspace-actor diff
    cost: Optional[float] = None
    ms: Optional[int] = None


# -- shared ledger signal -------------------------------------------------
class LostLease(Exception):
    """Raised by a ledger when a worker acts on an attempt it no longer holds
    (reclaimed, cancelled, or already completed). The worker treats it as 'someone
    else owns this job now' - abort, do NOT retry. (The SQLite demo store no-ops
    instead of raising; the worker catches this so both stores behave the same.)"""
