"""mxr - submit a task to the running MyndAIX runtime and print the agent's reply.

    PYTHONPATH=src python3 -m runtime.cli <agent> "<task>"
    # or a wrapper on PATH (see docs/OPERATING.md):  mxr <agent> "<task>"

Needs the worker-pool service running (`python3 -m runtime.serve`) and $MYNDAIX_DSN.
This is direct ops: you name the agent, the runtime dispatches it durably and hands
back the real reply. No orchestrator in the loop.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from runtime.contracts import TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger
from runtime.registry import REGISTRY

# `or`, not a get() default: an exported-but-EMPTY MYNDAIX_DSN (an env -i trampoline
# passing "${VAR:-}" through) must fall back too, not connect with "" (phone r3 MED-2).
DSN = os.environ.get("MYNDAIX_DSN") or "postgresql://localhost/runtime"

# ---- stable stderr markers (the script-caller contract; phone-audit marker fold) ----
# One per line, machine-parseable, emitted ALONGSIDE the human prose — the prose stays
# free to change, the markers do not. Script callers (e.g. orchestrator/play-review.sh)
# match ONLY these, never the prose:
#   MXR_SYNC_TIMEOUT    submit's sync wait expired (the job still runs in the ledger)
#   MXR_JOB_FAILED      job reached terminal 'failed'
#   MXR_JOB_DEAD        job reached terminal 'dead'
#   MXR_NO_SUCH_JOB     unknown job id / prefix
#   MXR_DONE_EMPTY      job done but produced no reply body (terminal, not pending)
#   MXR_DELIVERY_ERR    reply exists but the delivery bookkeeping hit an anomaly —
#                       the body stays retrievable via `mxr get --reply`
# Removing or renaming one is a CONTRACT change: update the wrapper + its tests first.


def _clean_reply(s: str) -> str:
    """Terminal-injection belt (phone r1 M-10): agent reply bodies are untrusted output —
    strip C0/C1 controls + DEL (keep \\t \\n \\r) before they hit an operator's terminal, the
    same range play-review's clean() strips before the inbox. Applied to BOTH reply
    prints (submit sync-reply and `get --reply`) so no caller receives raw ESC sequences."""
    return "".join(
        ch for ch in s
        if ch in "\t\n\r" or (ch >= " " and ch != "\x7f" and not "\x80" <= ch <= "\x9f")
    )


def _marker_safe(s: str) -> str:
    """Neutralize marker forgery in AGENT-CONTROLLED text bound for stderr (review r5 #1):
    stderr is the marker channel, and script callers match markers line-anchored
    (`^MXR_...$`), so an interior agent-authored line reading exactly like a reserved
    marker would forge the contract. Indent any MXR_-leading line by one space — content
    preserved for the human, the anchored match can no longer fire."""
    return "\n".join((" " + ln) if ln.startswith("MXR_") else ln for ln in s.splitlines())


@dataclasses.dataclass
class DiscussResult:
    agent: str
    reply: Optional[str]       # None = no usable reply
    job_id: Optional[str]      # present if submission was acknowledged before timeout
    ledger: str                # "local" | "<host>" — which machine holds the job
    error: Optional[str]       # TIMEOUT | UNREACHABLE | EMPTY | SSH_ERROR | TRANSPORT_ERROR
                               # | SUBMIT_ERROR | FAILED | None (success)


def _sanitize_reply(body: str) -> str:
    """Full ANSI+bidi sanitizer for discuss replies (untrusted multi-agent output).
    Strips CSI/OSC/ESC sequences before delegating to _clean_reply for C0/C1/DEL."""
    # CSI: ESC [ ... final-byte (colors, cursor, erase, etc.)
    body = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', body)
    # OSC: ESC ] ... BEL  OR  ESC ] ... ESC \  (ST-terminated)
    body = re.sub(r'\x1b\].*?(?:\x07|\x1b\\)', '', body, flags=re.DOTALL)
    # Other ESC two-char sequences (e.g. ESC M reverse-index)
    body = re.sub(r'\x1b.', '', body)
    # Bidi controls: U+200E, U+200F, U+202A-U+202E, U+2066-U+2069, U+061C.
    # EXPLICIT \u escapes, NEVER embedded literals (review P1): the literals are invisible,
    # and a single bidi-unaware tool in the pipeline (editor/diff/git transport) collapses
    # the class to [--] — silently disabling the strip AND eating every hyphen in the reply.
    body = re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c]', '', body)
    return _clean_reply(body)


async def _submit_and_fetch(agent: str, topic: str, timeout_s: float) -> DiscussResult:
    """Submit topic to the local ledger and poll for a reply. Does not print."""
    try:
        led = await PostgresLedger.connect(DSN)
    except Exception as e:
        return DiscussResult(agent=agent, reply=None, job_id=None,
                             ledger="local", error=f"SUBMIT_ERROR: {e}")
    job_id = None
    try:
        env = TransportEnvelope(transport="cli", account="cli", sender_id="operator",
                                reply_target="cli:operator", dedupe_key=str(uuid.uuid4()))
        event_id = await led.ingest_inbound(env, topic)
        jid = await led.submit_job(to_agent=agent, prompt=topic, context={},
                                   inbound_event_id=event_id, created_by="operator")
        job_id = str(jid)
        print(f"-> {agent}  (job {job_id[:8]})", file=sys.stderr, flush=True)
        print(f"JOB_ID={jid}", file=sys.stderr, flush=True)

        deadline = time.monotonic() + timeout_s
        st = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                     ledger="local", error="TIMEOUT")
            try:
                # BOUND each poll by the remaining deadline (review P2): get_status carries
                # its own ~30s command timeout, so an unbounded await here overshoots the
                # caller's --timeout — and _discuss_main's as_completed then stalls every
                # OTHER agent's print behind this one slow participant.
                st = await asyncio.wait_for(led.get_status(jid), timeout=remaining)
            except asyncio.TimeoutError:
                return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                     ledger="local", error="TIMEOUT")
            if st and st["status"] in ("done", "failed", "dead"):
                break
            await asyncio.sleep(min(0.3, max(0.0, deadline - time.monotonic())))

        if st["status"] == "done":
            outs = st.get("outbound") or []
            # CAS pending->sent, mirroring run_job (r7 #1): a displayed-but-still-pending
            # outbound row is eligible for a later transport to claim and RE-DELIVER.
            # Best-effort — the body prints regardless and stays retrievable via
            # `mxr get --reply`, so a bookkeeping error never sinks the discuss view.
            for o in outs:
                if o.get("status") == "pending" and o.get("id") is not None:
                    try:
                        if await led.mark_outbound_sent_inline(o["id"], f"cli-{o['id']}"):
                            o["status"] = "sent"
                    except Exception:
                        pass
            bodies = [o.get("body") for o in outs if o.get("body")]
            if not bodies:
                return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                     ledger="local", error="EMPTY")
            return DiscussResult(agent=agent, reply=_sanitize_reply(bodies[-1]),
                                 job_id=job_id, ledger="local", error=None)

        return DiscussResult(agent=agent, reply=None, job_id=job_id,
                             ledger="local", error="FAILED")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        return DiscussResult(agent=agent, reply=None, job_id=job_id,
                             ledger="local", error=f"SUBMIT_ERROR: {e}")
    finally:
        await led.close()


async def _drain(stream: asyncio.StreamReader, limit: int) -> tuple[str, bool]:
    """Read up to `limit` bytes; keep draining past the cap so the pipe never blocks the
    writer, but REPORT truncation (review P2) so a clipped reply is never presented as
    complete. Returns (text, truncated). Module-level (not a _remote_fetch closure) so the
    truncation contract is unit-testable."""
    buf = bytearray()
    truncated = False
    while True:
        try:
            chunk = await stream.read(4096)
        except asyncio.CancelledError:
            break
        if not chunk:
            break
        room = limit - len(buf)
        if room > 0:
            buf.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        else:
            truncated = True
    return bytes(buf).decode("utf-8", errors="replace"), truncated


async def _remote_fetch(host: str, agent: str, topic: str, timeout_s: float) -> DiscussResult:
    """SSH-dispatch agent on remote host, capture stdout reply. Does not print."""
    # Hard precondition: remote login shell must be POSIX-compatible (bash/zsh).
    # Mini runs zsh — satisfied. shlex.quote() is unsafe on tcsh with newline-bearing topics.
    quoted_agent = shlex.quote(agent)
    quoted_topic = shlex.quote(topic)
    # Forward the resolved timeout so the REMOTE mxr's sync-wait matches the caller's
    # --timeout (review P2): without it the remote resolves its OWN wait (oracle defaults
    # to 360s), silently capping a `--timeout 900` request at ~360s.
    remote_cmd = (f"env MXR_REVIEW_GATE_BYPASS=1 MXR_TIMEOUT_S={timeout_s:g} "
                  f"~/.local/bin/mxr {quoted_agent} -- {quoted_topic}")

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            host, remote_cmd,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        # ValueError covers e.g. an embedded NUL byte in remote_cmd (create_subprocess_exec
        # rejects it) — must not escape and abort the whole discuss barrier (review LOW).
        return DiscussResult(agent=agent, reply=None, job_id=None,
                             ledger=host, error="UNREACHABLE")

    # stdout/stderr are always set: we pass PIPE above
    assert proc.stdout is not None and proc.stderr is not None

    job_id = None
    timed_out = False
    stdout_truncated = False
    stdout_t = asyncio.create_task(_drain(proc.stdout, 512 * 1024))
    stderr_t = asyncio.create_task(_drain(proc.stderr, 32 * 1024))
    proc_done_t = asyncio.create_task(proc.wait())

    try:
        done, _ = await asyncio.wait(
            {proc_done_t, stdout_t, stderr_t}, timeout=timeout_s
        )
        if proc_done_t not in done:
            # Timeout: process is still running — terminate it
            timed_out = True
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # kill() races the process's own exit in this 5s window; an already-reaped
                # pid raises ProcessLookupError, which would otherwise propagate out and
                # abort _discuss_main's as_completed loop — losing EVERY collected reply
                # (review P2). Terminating an already-dead process is a no-op, not an error.
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                await proc.wait()

        # Cancel any readers still outstanding and collect their buffers
        for t in (stdout_t, stderr_t):
            if not t.done():
                t.cancel()
        gathered = await asyncio.gather(stdout_t, stderr_t, return_exceptions=True)
        stdout_text, stdout_truncated = (
            gathered[0] if isinstance(gathered[0], tuple) else ("", False))
        stderr_text, _ = (
            gathered[1] if isinstance(gathered[1], tuple) else ("", False))

    except asyncio.CancelledError:
        # Outer task cancelled (Ctrl-C) — tear down subprocess cleanly
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Same already-reaped race as the timeout path — never let kill() abort
                # cleanup with ProcessLookupError (review P2).
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                await proc.wait()
        for t in (stdout_t, stderr_t, proc_done_t):
            if not t.done():
                t.cancel()
        gathered = await asyncio.gather(stdout_t, stderr_t, proc_done_t,
                                        return_exceptions=True)
        # The remote job keeps running in its ledger after we cancel; surface its JOB_ID
        # recovery handle before re-raising (review P2) — otherwise the acknowledged id was
        # gathered privately and the operator has no way to reconnect to the live job.
        cerr = gathered[1][0] if isinstance(gathered[1], tuple) else ""
        for line in cerr.splitlines():
            if line.startswith("JOB_ID="):
                print(f"[interrupted — {agent} job may be live on {host}: "
                      f"ssh {host} mxr get {line[7:].strip()} --reply]", file=sys.stderr)
                break
        raise

    try:
        # Parse JOB_ID= from stderr (present if remote mxr reached the submit step)
        for line in stderr_text.splitlines():
            if line.startswith("JOB_ID="):
                job_id = line[7:].strip()
                break

        if timed_out:
            return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                 ledger=host, error="TIMEOUT")

        # Terminal markers from the remote mxr take precedence over the rc gate (review P2):
        # a remote job that ACKED (emitted JOB_ID), then hit its own sync-timeout or
        # empty-done, exits NON-ZERO. Reading rc first would bury it as an opaque SSH_ERROR
        # with job_id discarded, stranding a reply that is still recoverable via
        # `mxr get --reply`. Matched LINE-ANCHORED (review MED), not by substring: a bare
        # `in stderr_text` test lets agent reply text that merely MENTIONS a marker name
        # forge the result — mirrors the documented marker contract (cli.py:35-46) and the
        # phone wrapper's `grep -q '^MARKER$'`.
        stderr_lines = stderr_text.splitlines()
        if "MXR_DONE_EMPTY" in stderr_lines:
            return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                 ledger=host, error="EMPTY")
        if "MXR_SYNC_TIMEOUT" in stderr_lines:
            return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                 ledger=host, error="TIMEOUT")
        if "MXR_JOB_FAILED" in stderr_lines or "MXR_JOB_DEAD" in stderr_lines:
            # The remote agent WAS reached and its job hit a terminal failed/dead state —
            # distinct from SSH_ERROR (transport/mxr-launch failure BEFORE the agent ran).
            # Falling through to the rc gate misreported this as SSH_ERROR, pointing the
            # operator at the wrong layer (SSH) instead of the real one (review MED).
            return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                 ledger=host, error="FAILED")

        rc = proc.returncode
        if rc == 255:
            # SSH transport error (connection refused, host unreachable, etc.)
            return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                 ledger=host, error="TRANSPORT_ERROR")
        if rc != 0:
            # Remote mxr itself failed (unknown agent, ledger down, etc.). PRESERVE job_id
            # (not None): if submission was acknowledged the job may be live — keep its
            # recovery handle rather than forcing an unrecoverable SSH_ERROR (review P2).
            return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                 ledger=host, error="SSH_ERROR")

        body = stdout_text.strip()
        if not body:
            return DiscussResult(agent=agent, reply=None, job_id=job_id,
                                 ledger=host, error="EMPTY")

        reply = _sanitize_reply(body)
        if stdout_truncated:
            # The reply hit the 512 KiB drain cap — flag it INLINE (review P2) so a clipped
            # review is never presented as complete; the full text stays in the ledger.
            handle = (f"ssh {host} mxr get {job_id} --reply" if job_id
                      else "job id not captured — full reply unavailable")
            reply += f"\n\n[reply truncated at 512 KiB — full text: {handle}]"
        return DiscussResult(agent=agent, reply=reply,
                             job_id=job_id, ledger=host, error=None)
    except Exception as e:
        # No exception may escape a participant task and abort the whole discuss barrier
        # (design contract, docs/mxr-discuss-design.md:266-268) — _submit_and_fetch already
        # honors this; this closes the same gap on the remote path (review LOW).
        return DiscussResult(agent=agent, reply=None, job_id=job_id,
                             ledger=host, error=f"SUBMIT_ERROR: {e}")


def _print_discuss_result(result: DiscussResult) -> None:
    bar = "─" * max(0, 48 - len(result.agent) - 2)
    print(f"\n─── [{result.agent}] {bar}")
    if result.reply:
        for line in result.reply.splitlines():
            print("    " + line)
    elif result.error == "TIMEOUT":
        if result.job_id:
            getter = (f"ssh {result.ledger} mxr get {result.job_id} --reply"
                      if result.ledger != "local" else f"mxr get {result.job_id} --reply")
            print(f"    [TIMEOUT — reply may be durable: {getter}]")
        else:
            print("    [TIMEOUT — job not confirmed (connection may have failed)]")
    elif result.error == "EMPTY":
        print("    [EMPTY — agent produced no reply]")
    elif result.error == "UNREACHABLE":
        print("    [UNREACHABLE — SSH connect failed or auth error]")
    elif result.error == "TRANSPORT_ERROR":
        note = "    [TRANSPORT_ERROR — SSH exit 255"
        if result.job_id:
            # Runnable form (review MED): TRANSPORT_ERROR only fires on the remote path, so
            # result.ledger is always a host here — "mxr get ... on <host>" is not a command
            # the operator can paste; ssh there first, like every other remote recovery hint.
            note += f"; job may be live: ssh {result.ledger} mxr get {result.job_id} --reply"
        print(note + "]")
    elif result.error == "SSH_ERROR":
        note = "    [SSH_ERROR — remote mxr failed before reaching the agent"
        if result.job_id:
            # _remote_fetch deliberately PRESERVES job_id on this path (submission was
            # acknowledged) — surface the same recovery handle every other job_id-bearing
            # branch gets instead of silently dropping it (review MED).
            note += f"; job may be live: ssh {result.ledger} mxr get {result.job_id} --reply"
        print(note + "]")
    elif result.error == "FAILED":
        jnote = f" (job {result.job_id})" if result.job_id else ""
        print(f"    [FAILED — agent job failed or died{jnote}]")
    elif result.error:
        print(f"    [{result.error}]")
    else:
        print("    [no reply]")


async def _discuss_main(agents_ordered: list, topic: str,
                        timeout_s: Optional[float]) -> int:
    tasks = []
    for ag in agents_ordered:
        spec = REGISTRY[ag]
        # timeout_s is None unless the operator passed an explicit --timeout/MXR_TIMEOUT_S
        # (that value then applies uniformly, unchanged). Otherwise derive PER-AGENT via
        # _resolve_sync_wait, mirroring run_job (review MED): a flat default shorter than a
        # slow agent's exec cap (kilabz's profile resolves to 960s) strands its DONE reply
        # behind a false TIMEOUT — the exact class _resolve_sync_wait exists to prevent.
        agent_timeout = timeout_s if timeout_s is not None else _resolve_sync_wait(ag)
        if spec.host:
            coro = _remote_fetch(spec.host, ag, topic, agent_timeout)
        else:
            coro = _submit_and_fetch(ag, topic, agent_timeout)
        tasks.append(asyncio.create_task(coro, name=ag))

    results: dict = {}
    try:
        for fut in asyncio.as_completed(tasks):
            result = await fut
            results[result.agent] = result
    except (asyncio.CancelledError, KeyboardInterrupt):
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for ag in agents_ordered:
            if ag in results:
                _print_discuss_result(results[ag])
        cancelled = len(agents_ordered) - len(results)
        if cancelled:
            print(f"\n[interrupted — {cancelled} agent(s) cancelled]", file=sys.stderr)
        print()
        sys.exit(130)

    for ag in agents_ordered:
        if ag in results:
            _print_discuss_result(results[ag])
    print()
    return 0 if any(r.reply for r in results.values()) else 1


def _resolve_sync_wait(agent: str) -> float:
    """The SYNC wait for a submitted job to finish: MXR_TIMEOUT_S when set (env ALWAYS
    wins — play-review exports it for slow reviews), else the agent's profile-derived
    wait (Profile.sync_wait(): exec timeout + margin, so the wait scales with the
    agent instead of a flat 180s that expired under kilabz's 900s exec cap), else 180.
    Parsed HERE, not in an import-time default arg — an empty or malformed exported
    MXR_TIMEOUT_S would otherwise crash float() at import and take down the WHOLE cli,
    including `mxr get`/`mxr skillselect` that never submit a job (kilabz+oracle)."""
    raw = os.environ.get("MXR_TIMEOUT_S") or ""
    if raw:
        try:
            return float(raw)
        except ValueError:
            print(f"warning: invalid MXR_TIMEOUT_S={raw!r}, using the agent default",
                  file=sys.stderr)
    prof = getattr(REGISTRY.get(agent), "profile", None)
    return prof.sync_wait() if prof is not None else 180.0


async def submit(agent: str, task: str, *, context: Optional[dict] = None,
                 repo_id: Optional[str] = None, base_ref: Optional[str] = None,
                 timeout_s: Optional[float] = None) -> int:
    rc, _terminal = await run_job(agent, task, context=context, repo_id=repo_id,
                                  base_ref=base_ref, timeout_s=timeout_s)
    return rc


async def run_job(agent: str, task: str, *, context: Optional[dict] = None,
                  repo_id: Optional[str] = None, base_ref: Optional[str] = None,
                  timeout_s: Optional[float] = None) -> tuple[int, bool]:
    """Submit + sync-wait + print the reply. Returns (rc, job_terminal): job_terminal is
    False ONLY when the sync wait expired with the job still in flight — the review verb
    gates staging teardown on it (a job can outlive the wait; deleting the staged cwd on
    sync-timeout would yank a RUNNING reviewer's cwd — the age-reaper owns that case)."""
    if agent not in REGISTRY:
        roster = ", ".join(sorted(REGISTRY))
        print(f"unknown agent '{agent}'. roster: {roster}", file=sys.stderr)
        return 2, True
    if timeout_s is None:
        timeout_s = _resolve_sync_wait(agent)

    led = await PostgresLedger.connect(DSN)
    try:
        # the CLI is a transport: ingest -> submit, so completion auto-queues the reply
        env = TransportEnvelope(transport="cli", account="cli", sender_id="operator",
                                reply_target="cli:operator", dedupe_key=str(uuid.uuid4()))
        event_id = await led.ingest_inbound(env, task)
        # repo_id/base_ref scope the job to a repo bucket (omitted -> NULL -> cap-exempt)
        jid = await led.submit_job(to_agent=agent, prompt=task, context=context,
                                   inbound_event_id=event_id, created_by="operator",
                                   repo_id=repo_id, base_ref=base_ref)
        print(f"-> {agent}  (job {str(jid)[:8]})", file=sys.stderr, flush=True)
        # full id on its own parseable line so a caller (orchestrator/play-fix.sh) can
        # `mxr get <jid>` for the artifact_ref. stderr-only -> the stdout reply is untouched.
        print(f"JOB_ID={jid}", file=sys.stderr, flush=True)

        deadline = time.monotonic() + timeout_s
        st = None
        while time.monotonic() < deadline:
            st = await led.get_status(jid)
            if st and st["status"] in ("done", "failed", "dead"):
                break
            await asyncio.sleep(0.3)
        else:
            print("MXR_SYNC_TIMEOUT", file=sys.stderr)
            print("timed out (is the pool running? `python3 -m runtime.serve`)", file=sys.stderr)
            return 1, False

        if st["status"] == "done":
            outs = st.get("outbound") or []
            # CAS BEFORE print (r8 P1a), updating the IN-MEMORY status on each win
            # (r10 #4 — a stale 'pending' in the snapshot made every later status check
            # lie). flush=True (r8 P1b) so the reply never sits in a userspace buffer
            # past the commit. ACCEPTED residual: a kill between CAS-commit and flush
            # loses the terminal copy — but not the reply: `mxr get --reply` reads
            # bodies status-independently, so the answer stays retrievable.
            for o in outs:
                if o["status"] == "pending":
                    # inline verb, not the transport one (r7 #1): mark_outbound_sent's
                    # leased-only CAS silently no-opped on these pending rows forever
                    if await led.mark_outbound_sent_inline(o["id"], f"cli-{o['id']}"):
                        o["status"] = "sent"
            # TRUTHY bodies only (r6 P2): `body text NOT NULL` permits "" — an empty
            # string is the SAME terminal no-answer state as a missing row.
            bodies = [o.get("body") for o in outs if o.get("body")]
            if not bodies:
                # done-with-no-body is TERMINAL no-answer, not success-with-silence — the
                # same state get --reply marks; both paths share one contract (review r5 #4).
                print("MXR_DONE_EMPTY", file=sys.stderr)
                print("job done but produced no reply body", file=sys.stderr)
                return 1, True
            # THE reply is the NEWEST truthy body (the get --reply contract; outs is
            # oldest->newest per migration 0015). Print it iff its row is now SENT —
            # either this process won its CAS just above, or it was delivered before we
            # looked (idempotent re-display). A leased/lost newest row is the sender's:
            # printing an OLDER won body instead would hand the user a stale answer
            # (r9 #3 + r10 #5 collapse into this one newest-row-authority rule).
            newest = next(o for o in reversed(outs) if o.get("body"))
            if newest["status"] != "sent":
                # r11 #2: a sender can complete the row between our snapshot and the CAS
                # (the CAS fails, the in-memory status stays 'pending'). Re-read the DB
                # truth ONCE for the authoritative row: sent -> idempotent re-display;
                # leased -> genuinely in-flight, the sender's to deliver.
                # Anomalies here exit MARKER + rc 1 like every other terminal path
                # (r14 #2: a raw traceback carries no marker and no actionable message)
                # — the reply itself stays retrievable via `mxr get --reply`.
                def _delivery_err(msg: str) -> tuple[int, bool]:
                    print("MXR_DELIVERY_ERR", file=sys.stderr)
                    print(f"{msg} — reply retrievable via `mxr get --reply {jid}`",
                          file=sys.stderr)
                    return 1, True
                st2 = await led.get_status(jid)
                if not st2:
                    # r12 #3 / r13 #1: get_status returns {} (not None) for an unknown id
                    # — a vanished job mid-call is anomalous; fail loudly, never dress a
                    # DB failure up as "row leased"
                    return _delivery_err(f"re-read of job {jid} came back empty mid-delivery")
                nid = newest.get("id")
                if nid is None:
                    # r14 #1: a snapshot row without an id is its OWN anomaly — not the
                    # misleading "row None vanished"
                    return _delivery_err(f"outbound snapshot row has no id for job {jid}")
                # r12 #2: never let two missing ids stringify into a 'None'=='None' match
                cur = next((o2 for o2 in (st2.get("outbound") or [])
                            if str(o2.get("id")) == str(nid)), None)
                if cur is None:
                    # r13 #2: job present but the authoritative row GONE is the same
                    # anomaly class — same loud failure, never silently-stale state
                    return _delivery_err(f"outbound row {nid} vanished from job {jid} mid-delivery")
                newest = {**newest, **cur}   # full row (r12 #4): a sender may have
                # written body alongside status — never fresh status over a stale body
            if newest["status"] == "sent":
                print(_clean_reply(newest["body"]), flush=True)
            else:
                print("the newest reply is owned by a transport sender (row leased) — "
                      "body retrievable via `mxr get --reply`", file=sys.stderr)
            return 0, True

        # failed/dead: surface WHY (the agent's error output, from the attempt).
        # _marker_safe: the marker channel is stderr, and this text is AGENT-CONTROLLED —
        # an interior line reading exactly "MXR_SYNC_TIMEOUT" would satisfy a caller's
        # line-anchored marker grep and flip a dead job back to pending (review r5 #1).
        err = next((a.get("text") for a in (st.get("attempts") or [])
                    if a.get("status") == "failed" and a.get("text")), None)
        if err:
            print(_marker_safe(err.strip()), file=sys.stderr)
        print(f"MXR_JOB_{st['status'].upper()}", file=sys.stderr)  # MXR_JOB_FAILED / MXR_JOB_DEAD
        print(f"(job {st['status']})", file=sys.stderr)
        return 1, True
    finally:
        await led.close()


def _build_context(args: argparse.Namespace) -> dict:
    """Pack the optional media flags into Job.context (free-form dict, no contract
    change). Only set keys the operator actually passed."""
    ctx: dict = {}
    if args.image is not None:
        ctx["image_url"] = args.image
    if args.application is not None:
        ctx["application"] = args.application
    if getattr(args, "motion_id", None) is not None:
        ctx["motion_id"] = args.motion_id
    if getattr(args, "motion_strength", None) is not None:
        ctx["motion_strength"] = args.motion_strength
    if getattr(args, "shotlist", None):
        try:
            with open(args.shotlist) as f:
                ctx["shotlist"] = json.load(f)
        except (OSError, ValueError) as e:
            raise SystemExit(f"--shotlist: {e}")
    if getattr(args, "end_card", None):
        ec = args.end_card
        if not ec.startswith(("http://", "https://")):
            raise SystemExit("--end-card must be an http(s) URL (local paths are not accepted; "
                             "host the image or upload it first)")
        ctx["end_card_url"] = ec
    if getattr(args, "staged_workdir", None) is not None:
        # pass-through only: the RUNNER is the trust boundary (realpath strictly inside
        # $MYNDAIX_STAGING_ROOT, staging-cwd adapters only — fail-closed TERMINAL there).
        # `is not None` (not truthy): an EXPLICIT empty --staged-workdir "" must propagate
        # so the runner rejects it TERMINAL, never silently drop to a scratch downgrade
        # (kilabz r2 MED — a wrapper passing an unset var must not quietly de-contextualize).
        ctx["workdir"] = args.staged_workdir
    return ctx


async def get_job(job_id: str, reply: bool = False) -> int:
    """`mxr get <job_id>` -> structured JSON of the job's status, including
    artifact_ref + base_sha. The fix stage (orchestrator/play-fix.sh) reads the
    diff artifact from HERE - via the ledger, parsed as JSON - NEVER by grepping a
    reply body an agent controls (a spoofable path is a security hole, not a bug).

    Accepts a FULL uuid or an id PREFIX of >=8 hex chars (hyphens ignored on both
    sides, so both the 8-char short id `submit` prints and a hyphen-spanning slice
    of the full JOB_ID work). Ambiguous prefix -> fail closed listing candidates —
    same shape as the finding_key resolver (postgres_store.human_dismiss)."""
    raw_id = (job_id or "").strip()
    prefix: Optional[str] = None
    try:
        jid: Optional[uuid.UUID] = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        jid = None
        prefix = raw_id.replace("-", "").lower()
        if len(prefix) < 8 or any(c not in "0123456789abcdef" for c in prefix):
            print(f"not a job id: {job_id!r} (need a full uuid, or an id prefix of "
                  f">=8 hex chars)", file=sys.stderr)
            return 2
    led = await PostgresLedger.connect(DSN)
    try:
        if jid is None:
            matches = await led.resolve_job_prefix(prefix)
            if not matches:
                print("MXR_NO_SUCH_JOB", file=sys.stderr)
                print(f"no such job: {job_id}", file=sys.stderr)
                return 1
            if len(matches) > 1:
                print(f"ambiguous job id prefix {job_id!r} — candidates:", file=sys.stderr)
                for m in matches:
                    print(f"  {m}", file=sys.stderr)
                return 2
            jid = uuid.UUID(matches[0])
        st = await led.get_status(jid)
        if not st:
            print("MXR_NO_SUCH_JOB", file=sys.stderr)
            print(f"no such job: {job_id}", file=sys.stderr)
            return 1
        if reply:
            # latest outbound body = the agent's reply. Ordering is GUARANTEED by the store
            # (json_agg ... ORDER BY o.created_at, o.id — migration 0015; phone r1 M-9), so
            # [-1] is genuinely the newest. A done job with no body is "no reply yet".
            bodies = [o.get("body") for o in (st.get("outbound") or []) if o.get("body")]
            if bodies:
                print(_clean_reply(bodies[-1]))
                return 0
            # rc stays 3 for every no-body case (callers branch on the MARKER, not the
            # rc): failed/dead will never produce a body, and done-with-no-body is a
            # finished job that wrote nothing — both are terminal, not "keep polling".
            status = st.get("status")
            if status in ("failed", "dead"):
                print(f"MXR_JOB_{status.upper()}", file=sys.stderr)
            elif status == "done":
                print("MXR_DONE_EMPTY", file=sys.stderr)
            print(f"no reply yet (job status: {status})", file=sys.stderr)
            return 3
        out = {
            "job": str(st.get("id")),
            "status": st.get("status"),
            "to_agent": st.get("to_agent"),
            "repo_id": st.get("repo_id"),
            "artifact_ref": st.get("artifact_ref"),
            # base_ref carries the anchor the caller passed (--base-ref); base_sha is a
            # distinct column not populated on this path, so binding uses base_ref.
            "base_ref": st.get("base_ref"),
            "base_sha": st.get("base_sha"),
            "attempts": [{"status": a.get("status")} for a in (st.get("attempts") or [])],
        }
        print(json.dumps(out))
        return 0
    finally:
        await led.close()


def main(argv: Optional[list[str]] = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    # `mxr get <job_id>` — structured status read (D1). Special-cased ABOVE the flat
    # positional parser so the established `mxr <agent> <task>` interface is untouched.
    if raw and raw[0] == "get":
        gp = argparse.ArgumentParser(prog="mxr get",
                                     description="print a job's status as JSON")
        gp.add_argument("job_id", help="the job uuid, or an id prefix of >=8 hex chars "
                                       "(from a prior `mxr` submit; hyphens optional)")
        # --reply: print the job's REPLY BODY (latest outbound) instead of the status JSON —
        # the documented recover-a-late-reply flow ("verdict then only in the ledger") without
        # hand-rolled python. Exit 3 = job known but no reply yet (distinct from 1 = unknown
        # job, 2 = malformed id) so callers like the phone wrapper can say "still thinking".
        gp.add_argument("--reply", action="store_true",
                        help="print the reply body of a completed job (exit 3 if none yet)")
        gargs = gp.parse_args(raw[1:])
        return asyncio.run(get_job(gargs.job_id, reply=gargs.reply))

    # `mxr skillselect <repo_id> <changed-path>...` — the +learning rung READ path (build
    # plan Step 4). Routed through mxr so it inherits the runtime venv + PYTHONPATH +
    # MYNDAIX_DSN exactly like every other entry point (bare `python3 -m runtime.skillselect`
    # would not resolve the package in play-review's hook env, and the package lives here
    # regardless of which repo is under review). Special-cased ABOVE the flat agent/task
    # parser, like `get`; skillselect fails OPEN to empty stdout. PLAY_NONCE/PLAY_ID/PLAY_GATE
    # pass through the inherited env.
    if raw and raw[0] == "skillselect":
        from runtime import skillselect
        return skillselect.main(["skillselect", *raw[1:]])

    # `mxr capture-record ...` — auto-capture INSTRUMENTATION (observe-only). Same routing
    # rationale as skillselect (inherits venv/PYTHONPATH/DSN via mxr); fails OPEN, never blocks
    # a review. Records cross-family-agreed rule:<tag> signals; the SEPARATE S7 proposer
    # (`python -m runtime.proposer tick`, launchd `ai.myndaix.proposer`) turns `ready` classes into
    # skill-draft PRs — this recorder never opens a PR.
    if raw and raw[0] == "capture-record":
        from runtime import capturerecord
        return capturerecord.main(["capture-record", *raw[1:]])

    # `mxr outcome-record ...` — outcomes-ledger INSTRUMENTATION (the per-finding OUTCOME LABEL
    # recorder). Same routing rationale as capture-record (inherits venv/PYTHONPATH/DSN via mxr);
    # fails OPEN, HARD no-op in gate mode, never opens a PR. Records finding:<tag> lines from BOTH
    # families into finding_outcome (CLOSE + OPEN) and prints the recorded keys for play-review.
    # `mxr outcome-stats` is checked BEFORE `outcome` because the flat parser would read "-stats" as
    # a prefix; both are special-cased above the flat agent/task parser like get/skillselect.
    if raw and raw[0] == "outcome-record":
        from runtime import outcomerecord
        return outcomerecord.main(["outcome-record", *raw[1:]])
    if raw and raw[0] == "outcome-stats":
        from runtime import outcomerecord
        return outcomerecord.stats_main(["outcome-stats", *raw[1:]])
    # `mxr labelqueue` — read-only browser of findings awaiting a human label, clustered by
    # (rule_tag, family) with paste-ready keys (label-throughput PR-A §2c). OPERATOR tier:
    # fail-CLOSED (exit 2) if the ledger is unreachable.
    if raw and raw[0] == "labelqueue":
        from runtime import outcomerecord
        return outcomerecord.labelqueue_main(["labelqueue", *raw[1:]])
    # `mxr dial-shadow [--snapshot|--eval]` — the shadow dial's MEASURE-ONLY read surface
    # (docs/shadow-dial-design.md v0.6 PR-A): per-(tag × family) Wilson-bounded precision over
    # human labels + a would-suppress/would-trust classification that ACTS ON NOTHING. OPERATOR
    # tier: fail-CLOSED (exit 2) on an unreachable ledger. Rides the morning brain-check.
    if raw and raw[0] == "dial-shadow":
        from runtime import dialshadowrecord
        return dialshadowrecord.main(["dial-shadow", *raw[1:]])
    # `mxr outcome <key12> real|fp|wontfix` (single) or `mxr outcome <kind> <key12>...` (batch) —
    # the human's per-finding label, ALL kinds routed through the fence's confirm_outcome
    # (label-throughput PR-A D-1: `real` is the gating numerator and finally has a caller).
    if raw and raw[0] == "outcome":
        from runtime import outcomerecord
        return outcomerecord.label_main(["outcome", *raw[1:]])

    # `mxr knowledge-ingest / recall / knowledge-rebuild / curate` — the curator rung
    # (docs/curator-design.md v0.4). Same routing rationale as the verbs above (inherits
    # venv/PYTHONPATH/DSN via mxr). Operator verbs: unknown scope is a HARD error (exit 2),
    # never fail-open — misconfiguration must not read as "no knowledge". `curate` is the
    # deterministic guard around the pool's curator agent (stage-in -> dispatch -> promote).
    if raw and raw[0] == "knowledge-ingest":
        from runtime import knowledgerecord
        return knowledgerecord.ingest_main(["knowledge-ingest", *raw[1:]])
    if raw and raw[0] == "knowledge-rebuild":
        from runtime import knowledgerecord
        return knowledgerecord.rebuild_main(["knowledge-rebuild", *raw[1:]])
    if raw and raw[0] == "recall":
        from runtime import knowledgerecord
        return knowledgerecord.recall_main(["recall", *raw[1:]])
    # `mxr ask --scope X "Q"` — the recall LIBRARIAN (synthesis over recall): retrieve fenced hits
    # then dispatch the tool-less `librarian` RESPONDER to answer WITH citations. Read-only; unknown
    # scope is a HARD error (exit 2). The second-brain rung-1 answer layer (docs/mx-ask-librarian-design.md).
    if raw and raw[0] == "ask":
        from runtime import knowledgerecord
        return knowledgerecord.ask_main(["ask", *raw[1:]])
    if raw and raw[0] == "knowledge-index":
        from runtime import knowledgerecord
        return knowledgerecord.index_main(["knowledge-index", *raw[1:]])
    if raw and raw[0] == "curate":
        from runtime import curate
        return curate.main(["curate", *raw[1:]])

    # `mxr review <agent> --repo <path|basename> ...` — the review-context verb
    # (docs/mxr-review-context-design.md D6): stage a de-linked read-only snapshot of the
    # reviewed tip as the CONFINED reviewer's cwd, build the objective-above-fence prompt
    # with the nonce-fenced range diff, dispatch, tear down. Replaces the hand-embed
    # `mxr kilabz "$(cat prompt+diff)"` workflow end-to-end.
    if raw and raw[0] == "review":
        from runtime import review
        return review.main(["review", *raw[1:]])

    # `mxr pr-gate --repo <dir> --repo-id <id> --base-sha B --head-sha H --pr N` — the
    # review-on-GitHub merge gate's trusted core (docs/review-on-github-plan.md [not in the public snapshot] P2): one
    # fixed-format decision line on stdout for the workflow to post as a commit status.
    if raw and raw[0] == "pr-gate":
        from runtime import pr_gate
        return pr_gate.main(["pr-gate", *raw[1:]])

    # `mxr review-stage <repo> <tip> | review-teardown <dir> | review-reap` — the STAGING
    # primitives for play-review.sh (PR-2). play-review runs its OWN review pipeline (fences,
    # skillselect, oracle-inline, triage), so it wants only the exporter, not the full `review`
    # verb. Routed through mxr so the hook env inherits the runtime venv + PYTHONPATH + DSN
    # (bare `python3 -m runtime.staging` would not resolve). stage prints the snapshot dir;
    # teardown refuses anything not a review-* dir under the staging root; reap fails CLOSED
    # if the ledger is unreachable (never blind mtime-reap).
    if raw and raw[0] in ("review-stage", "review-teardown", "review-reap"):
        from runtime import staging
        sub = {"review-stage": "stage", "review-teardown": "teardown",
               "review-reap": "reap"}[raw[0]]
        return staging.main(["staging", sub, *raw[1:]])

    # `mxr discuss "<topic>" --with <agent>...` — parallel fan-out to RESPONDER agents.
    # Routes through Python directly (local) or SSH (host="mini"), so no hook fires here.
    if raw and raw[0] == "discuss":
        dp = argparse.ArgumentParser(prog="mxr discuss",
                                     description="fan a topic to multiple agents in parallel")
        dp.add_argument("topic", help="the question or topic to discuss")
        dp.add_argument("--with", dest="agents", metavar="AGENT", nargs="+", required=True,
                        help="agents to participate (RESPONDER authority only)")
        dp.add_argument("--timeout", type=float, default=None,
                        help="per-agent reply timeout in seconds "
                             "(default: MXR_TIMEOUT_S or each agent's own profile wait)")
        dargs = dp.parse_args(raw[1:])

        # Deduplicate while preserving order
        seen: set = set()
        agents_ordered = []
        for ag in (dargs.agents or []):
            if ag not in seen:
                seen.add(ag)
                agents_ordered.append(ag)

        if not agents_ordered:
            dp.error("--with requires at least one agent")

        # Preflight: validate ALL agents before any dispatch (atomically reject mixed lists)
        errors = []
        for ag in agents_ordered:
            if ag not in REGISTRY:
                errors.append(f"  '{ag}': unknown (roster: {', '.join(sorted(REGISTRY))})")
            elif REGISTRY[ag].authority.value != "responder":
                errors.append(
                    f"  '{ag}': authority={REGISTRY[ag].authority.value} — "
                    f"only RESPONDER agents may participate in discuss"
                )
        if errors:
            print("mxr discuss: agent validation failed:", file=sys.stderr)
            for e in errors:
                print(e, file=sys.stderr)
            return 2

        # No manual MXR_TIMEOUT_S/flat-default resolution here (review MED): an explicit
        # --timeout applies uniformly and wins over everything, same as before. When unset,
        # None flows through to _discuss_main so each agent gets _resolve_sync_wait(ag) —
        # which is where MXR_TIMEOUT_S-env-wins and the per-profile default already live,
        # instead of a flat 300s that ignores a slow agent's profile (review MED).
        if dargs.timeout is not None and not (math.isfinite(dargs.timeout)
                                              and dargs.timeout > 0):
            dp.error(f"--timeout must be a finite positive number, got {dargs.timeout!r}")

        return asyncio.run(_discuss_main(agents_ordered, dargs.topic, dargs.timeout))

    p = argparse.ArgumentParser(
        prog="mxr", description='submit a task to the MyndAIX runtime',
        epilog='for a task that starts with a dash, use --:  mxr recon -- "-v explain"')
    p.add_argument("agent", help="roster agent id (e.g. recon, higgsfield)")
    p.add_argument("task", nargs="?", help="the prompt / task text (or use --prompt-file)")
    p.add_argument("--prompt-file", metavar="PATH", dest="prompt_file",
                   help="read the task text from this file instead of argv — sidesteps the OS "
                        "argv/env size ceiling (E2BIG) for large embedded diffs/reviews "
                        "(issue #83); trusted operator input, read verbatim")
    p.add_argument("--image", metavar="URL",
                   help="input image url (media agents, e.g. higgsfield image->video)")
    p.add_argument("--application", metavar="PATH",
                   help="override the agent's media application/model path")
    p.add_argument("--motion-id", metavar="UUID", dest="motion_id",
                   help="DoP camera-preset uuid (higgsfield/stitcher); see GET /v1/motions")
    p.add_argument("--motion-strength", metavar="N", dest="motion_strength", type=float,
                   help="DoP motion strength, 0.3 (subtle) to 1.0 (dramatic)")
    p.add_argument("--shotlist", metavar="PATH",
                   help="path to a JSON shot-list (stitcher): ordered list of shot objects")
    p.add_argument("--end-card", metavar="URL", dest="end_card",
                   help="branded end-card image URL to append (stitcher; http(s) only)")
    p.add_argument("--repo", metavar="ID", dest="repo_id",
                   help="repo bucket id for per-repo concurrency (omitted -> cap-exempt)")
    p.add_argument("--base-ref", metavar="REF", dest="base_ref",
                   help="base git ref/SHA the work is anchored to (e.g. the reviewed tip)")
    p.add_argument("--staged-workdir", metavar="DIR", dest="staged_workdir",
                   help="a staging dir the CALLER created for this job's cwd — honored ONLY "
                        "by staging-cwd adapters (kilabz/lobster/curator), MUST resolve "
                        "strictly inside $MYNDAIX_STAGING_ROOT, and fails the job TERMINAL "
                        "otherwise (it cannot select an arbitrary cwd)")
    args = p.parse_args(raw)
    # exactly ONE task source: the positional or --prompt-file (operator error -> exit 2).
    if (args.task is None) == (args.prompt_file is None):
        p.error("provide exactly one of <task> or --prompt-file")
    task = args.task
    if args.prompt_file is not None:                 # is-None, not truthiness: --prompt-file "" must
        try:                                         # still enter here (and fail cleanly), never leave task=None
            task = Path(args.prompt_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:   # UnicodeDecodeError is a ValueError, not OSError
            p.error(f"--prompt-file: {e}")
        if not task.strip():
            p.error("--prompt-file: file is empty")
    return asyncio.run(submit(args.agent, task, context=_build_context(args),
                              repo_id=args.repo_id, base_ref=args.base_ref))


if __name__ == "__main__":
    raise SystemExit(main())
