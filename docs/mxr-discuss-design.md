# DESIGN — mxr discuss

**Status:** v0.5 — fourth review revision
**Branch:** feat/mxr-discuss

## What

`mxr discuss "<topic>" --with <agent> [<agent>...]`

Parallel fan-out to named agents, barrier aggregation, labeled output. One command replaces
the current manual pattern of `MXR_REVIEW_GATE_BYPASS=1 mxr lobster "..."` × N agents +
SSH-routing oracle by hand + collecting replies separately.

```
$ mxr discuss "should we build the proposer now?" --with oracle kilabz

─── [oracle] ────────────────────────────────────
...reply...

─── [kilabz] ────────────────────────────────────
...reply...
```

## Why

Three friction points today:
1. Each agent is a separate manual call — no way to say "the team discusses this"
2. Oracle requires SSH routing to the Mini; callers own that routing
3. The review-gate hook fires on any `mxr kilabz/oracle` call; callers need `MXR_REVIEW_GATE_BYPASS=1`

## Build-vs-Adopt

- `mxr discuss` subcommand: **BUILD** — extends the existing CLI; no prior art covers this
- Parallel dispatch: **BORROW** `asyncio.gather` (standard library)
- Oracle SSH routing: **BUILD** a `host` field in `AgentSpec` + `_remote_fetch()` helper;
  the informal pattern `ssh mini mxr oracle` already exists — we're formalizing it

## Key design decisions

### Native subcommand (all three reviewers agree)

Implemented in `cli.py`. Reuses the registry, existing submission helpers, and the
`asyncio` event loop already required by `run_job()`. No wrapper script.

### Registry `host` field for cross-machine routing

`AgentSpec` gets `host: Optional[str] = None` (None = run locally). Oracle's entry gets
`host="mini"`. The discuss handler checks this field and picks the dispatch path. Adding
future remote agents is a one-line registry change. The `host` value is registry-declared —
callers cannot supply an arbitrary host via command-line flag.

### Two dispatch paths

**Local agents** (`host=None`): call `_submit_and_fetch(agent, topic)` — a non-printing
coroutine extracted from `run_job()`. It submits to the MacBook's ledger, polls, and
returns `DiscussResult`.

**Remote agents** (`host="mini"`): call `_remote_fetch("mini", agent, topic)` — spawns
`asyncio.create_subprocess_exec("ssh", ...)` and parses stdout. See SSH contract below.

### `DiscussResult` return type

```python
@dataclass
class DiscussResult:
    agent: str
    reply: str | None          # None = no usable reply
    job_id: str | None         # present if submission was acknowledged before timeout
    ledger: str                # "local" | "mini" — tells user which mxr get to run
    error: str | None          # TIMEOUT | UNREACHABLE | EMPTY | SSH_ERROR | TRANSPORT_ERROR
```

`reply` and `error` are mutually exclusive. Explicit precedence order (highest wins):
1. Terminal marker (`MXR_DONE_EMPTY`, `MXR_SYNC_TIMEOUT`) — overrides body content
2. SSH exit code (255 = transport; other non-zero = remote CLI failure)
3. Truthy stdout body → success
4. Per-task deadline expiry → TIMEOUT
5. Subprocess creation / ledger failure → error

Legal `(reply, error, job_id)` states:

| reply | error | job_id | meaning |
|---|---|---|---|
| str | None | str | success — agent replied |
| None | "TIMEOUT" | str | timed out after submission; reply durable |
| None | "TIMEOUT" | None | timed out before submission confirmed |
| None | "EMPTY" | str | agent returned no body |
| None | "TRANSPORT_ERROR" | str? | SSH exit 255; job may be live on remote |
| None | "SSH_ERROR" | None | SSH non-255 or remote CLI failure before ACK |
| None | "UNREACHABLE" | None | SSH connect failed / auth error |
| None | "SUBMIT_ERROR" | None | local ledger or subprocess creation failure |

`job_id` is parsed from stderr `JOB_ID=<uuid>` before the reply wait — so a post-submission
timeout always carries it. Pre-submission failures have `job_id=None`; the recovery notice
never promises a retrievable handle in that case.

The acknowledged ACK-loss residual (job committed but ACK lost → `job_id=None` despite
active remote job) is documented in the edge cases table.

### SSH injection defense (oracle CRITICAL)

Topic is passed to the remote `mxr` invocation via `shlex.quote()`, and `--` precedes the
topic argument to prevent option injection:

```python
import shlex
# `env` prefix ensures inline assignment works regardless of remote shell (fish/tcsh/zsh/bash).
# `-T` disables PTY allocation so stdout and stderr remain separate streams.
remote_cmd = f"env MXR_REVIEW_GATE_BYPASS=1 ~/.local/bin/mxr {shlex.quote(agent)} -- {shlex.quote(topic)}"
proc = await asyncio.create_subprocess_exec(
    "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    host, remote_cmd,
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
```

**Hard precondition**: the remote login shell must be POSIX-compatible (bash or zsh). The
Mini runs zsh — this is satisfied. `shlex.quote()` on tcsh would be unsafe for topics
containing newlines; tcsh is not present on the Mini and is not a supported target.

`shlex.quote()` wraps the value in single quotes and escapes internal single quotes —
injection-safe for POSIX-compatible remote shells. `--` terminates option parsing before the topic.
`BatchMode=yes` disables password/host-key prompts (fails noninteractively instead).
`ConnectTimeout=10` bounds the SSH handshake independently of the reply timeout.
`stdin=DEVNULL` ensures the subprocess never inherits the parent's stdin.

### Review-gate exemption

**Local path:** The `discuss` subcommand calls `_submit_and_fetch()` via Python directly —
no `mxr` subprocess is spawned, so no PreToolUse hook fires. No bypass flag needed.

**Remote path:** The SSH command string explicitly prepends `MXR_REVIEW_GATE_BYPASS=1`:

```python
remote_cmd = f"env MXR_REVIEW_GATE_BYPASS=1 ~/.local/bin/mxr {shlex.quote(agent)} -- {shlex.quote(topic)}"
```

This is safe: the bypass value is a literal constant, not derived from user input. `env`
prefix ensures the assignment works on non-POSIX shells (fish, tcsh) on the remote host. The bypass
is documented as the correct mechanism for non-review dispatches (commit-before-review.md).

### Barrier aggregation with per-task timeout

Each `_remote_fetch`/`_submit_and_fetch` handles its own timeout internally — there is no
external `asyncio.wait_for` wrapper. When the per-task deadline fires, the function catches
it and returns `DiscussResult(..., error="TIMEOUT", job_id=job_id)`. This ensures the
`job_id` captured before the wait is never lost to a `wait_for` cancellation.

Total wall-clock ≈ 30 s (the per-task timeout; concurrent tasks race against the same wall
clock). Cancellation cleanup time is additional and unbounded — this is a known limitation.

Tasks are collected via `asyncio.as_completed()` into a local results list. Each completed
task's result is appended immediately (before the next `await`), so completed results are
available even if the event loop is cancelled mid-collection.

#### Ctrl-C / SIGINT handling

`asyncio.run()` responds to SIGINT by cancelling the main task. The `discuss` main coroutine
wraps the `as_completed` loop in a `try/except (asyncio.CancelledError, KeyboardInterrupt)`
block. In the except handler: cancel any still-pending participant tasks, drain their
results (each task's exception is caught, not re-raised), print any completed results, then
`exit(130)`. Participant tasks that are still running (not yet in `as_completed`) will have
their coroutines cancelled — their internal `except CancelledError` blocks must terminate
SSH subprocesses before re-raising.

#### Subprocess teardown

When a task's internal timeout fires OR when cancelled: `proc.terminate()` → `await
asyncio.wait_for(proc.wait(), 5)` → on timeout, `proc.kill()` → `await proc.wait()`. Stream
readers are cancelled after process exit. This teardown is best-effort — the 5 s terminate
window is bounded; the subsequent `kill` + `wait` is not (SIGKILL must succeed).

### Timeout recovery notices

```
─── [oracle] ────────────────────────────────────
[TIMEOUT — job accepted, reply via: ssh mini mxr get <job_id> --reply]

─── [oracle] ────────────────────────────────────
[TIMEOUT — job not confirmed (connection failed)]
```

Only the first form appears when `job_id` is known. The second when `job_id` is None.
`mxr get` target is `ssh mini mxr get` for remote agents, bare `mxr get` for local ones.

### Ephemeral at the discuss level

No new "discussion" table, no `discussion_id` correlating the participant jobs. Local-agent
jobs land in the MacBook ledger as normal independent jobs. Oracle's job lands in the Mini's
ledger. Nothing correlates them — acceptable for v1. Pipe stdout to a file for a durable record.

### Inbound event uniqueness

Each `_submit_and_fetch()` call generates a fresh `uuid.uuid4()` dedupe key (same as the
existing `run_job()` path — see `cli.py:108`). The uniqueness constraint (`capture_candidate_pkey`
on `fingerprint`) is in the capture table, not on inbound events. The `inbound_event` table
has no uniqueness constraint on the dedupe key beyond what `ingest_inbound()` enforces. Each
participant gets its own inbound event row — no collision.

### Agent eligibility

Eligible agents: `Authority.RESPONDER` **only** — oracle, kilabz. (recon is COMPOSITE and excluded in v1; see registry.py)

Rationale for excluding CONTROLLER (lobster is `Authority.CONTROLLER`): CONTROLLER agents
have the registered authority to spawn child jobs via `submit_job`. Nothing in the runner
restricts that capability based on which command submitted the parent job. Rather than argue
that `discuss` is safe with CONTROLLERs, we exclude them for v1. If lobster-as-discussant
is needed, the right path is adding a `RESPONDER`-mode profile to lobster's registry entry.
For now, lobster participates via `mxr lobster "..."` from the caller if desired.

Rejected explicitly: any authority value that is not `RESPONDER` — this is a positive
allowlist, not a negative check. Unknown future authority values are rejected by default.

Preflight validates ALL agent names and authorities before dispatching to ANY. A mixed
roster is rejected atomically with a clear error before any submission.

### Authority × route combinations

| Agent authority | Eligible? | host=None (local) | host="mini" (remote SSH) |
|---|---|---|---|
| RESPONDER | ✅ yes | ✅ | ✅ |
| CONTROLLER | ❌ no | rejected at preflight | rejected at preflight |
| WORKSPACE_ACTOR | ❌ no | rejected at preflight | rejected at preflight |
| (unknown) | ❌ no | rejected at preflight | rejected at preflight |

Remote eligibility is a v1 operational assumption: the Mini's registry is kept in sync via
git pull. `discuss` validates locally and trusts the remote to reject jobs for unrecognized
agents. A missing name surfaces as `error="SSH_ERROR"`. Future work: explicit remote health
check before dispatch.

Remote registry compatibility: `discuss` sends the same topic to the remote `mxr` using the
agent name as declared in the local registry. The remote machine's registry must have a
matching entry — verified trivially (oracle is the only remote agent, and the Mini's registry
is kept in sync with the main tree). If a name is missing on the remote, `mxr` returns a
non-zero exit; `_remote_fetch` surfaces this as `error="SSH_ERROR"`.

Remote eligibility is an operational assumption: the Mini's registry is kept in sync with the
MacBook's via the standard git pull workflow. `discuss` validates locally and trusts the remote
to reject jobs for unrecognized agents. If the remote agent name is missing, `mxr` returns
non-zero; `_remote_fetch` surfaces this as `error='SSH_ERROR'`. This is a known v1 limitation
— future work can add an explicit remote-registry health check.

### Remote result parsing

The existing `mxr` CLI prints to stdout (reply text) and stderr (progress markers: `JOB_ID=`,
`MXR_DONE_EMPTY`, `MXR_SYNC_TIMEOUT`, `-> agent`). `_remote_fetch` captures both streams
concurrently via `asyncio.create_task` readers:

- **Concurrent readers**: stdout and stderr are drained simultaneously via two
  `asyncio.create_task` coroutines running in parallel. Sequential reads can deadlock if the
  pipe fills while the other stream is being read.
- **stderr**: scan for `JOB_ID=<uuid>` and terminal markers (`MXR_DONE_EMPTY`,
  `MXR_SYNC_TIMEOUT`). Retained cap: **32 KB** — bytes beyond are *discarded* while reading
  continues (to prevent pipe backpressure), but the retained window is scanned for markers.
- **stdout**: reply body. Retained cap: **512 KB** — excess bytes are discarded while
  reading continues. Truncation is noted in the DiscussResult if the body was capped.
- **Encoding**: `errors='replace'` on both streams.
- **Reader shutdown**: during normal exit, `await reader.read()` until EOF before closing.
  During timeout/kill, readers are cancelled after `proc.kill()` + `proc.wait()` complete —
  remaining buffered output is lost, which is acceptable post-kill.
- **SSH exit code**: 0 = remote command exited 0 → use body + markers to determine result.
  Non-zero: 255 = SSH transport error (`TRANSPORT_ERROR`); other = remote CLI failure
  (`SSH_ERROR`). Log the distinction. Note: SSH propagates the remote command's exit status
  directly, so exit 255 from the remote `mxr` would be misread as `TRANSPORT_ERROR` — this
  is a known ambiguity; `mxr` does not currently exit 255.
- **All unhandled exceptions** in `_remote_fetch` / `_submit_and_fetch` are caught and
  mapped to `DiscussResult(error="SUBMIT_ERROR")` — no exception may escape a participant
  task and abort the barrier.

### Labeled output trust boundary

Reply text is sanitized before printing with a multi-pass strip:

```python
# CSI sequences: ESC [ ... final-byte (covers colors, cursor movement, erase, etc.)
body = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', body)
# OSC sequences: ESC ] ... BEL  OR  ESC ] ... ESC \  (ST-terminated form)
body = re.sub(r'\x1b\].*?(?:\x07|\x1b\\)', '', body, flags=re.DOTALL)
# Other ESC-prefixed sequences (ESC followed by any single char, e.g. ESC M)
body = re.sub(r'\x1b.', '', body)
# C1 control characters (U+0080–U+009F)
body = re.sub(r'[\x80-\x9f]', '', body)
# DEL (U+007F)
body = body.replace('\x7f', '')
# Unicode bidi controls: strip U+200E, U+200F, U+202A-U+202E, U+2066-U+2069, U+061C.
# Build the character class from explicit code-point escapes in implementation code —
# do not embed bidi literals here (they are invisible and survive encoding drift).
body = re.sub(bidi_pattern, '', body)
# Raw C0 controls (except \n \t)
body = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', body)
# Carriage returns
body = body.replace('\r', '')
```

Every reply line is prefixed with a 4-space indent before printing so that any forged
`─── [agent]` headers embedded in reply text are visually distinguishable from the
application-generated section headers.

## Data flow

```
mxr discuss "<topic>" --with oracle kilabz

1. Validate all agent names + authorities against REGISTRY — reject entire call if any invalid
2. For each agent, build a DiscussTask:
     host=None  → _submit_and_fetch(agent, topic)   [MacBook ledger]
     host="mini" → _remote_fetch("mini", agent, topic)  [ssh + stdout capture]
3. Each task handles its own internal timeout and returns DiscussResult(error="TIMEOUT") on expiry.
   results collected via asyncio.as_completed() so partial results survive KeyboardInterrupt.
4. For each result in --with order:
     if DiscussResult.reply: print header + reply
     else: print header + timeout/error notice
5. Exit 0 if ≥1 reply received; exit 1 if all failed
```

## Edge cases and failure modes

| Scenario | Handling |
|---|---|
| Unknown / ineligible agent name | Preflight rejects before any dispatch; print roster |
| Mixed eligible/ineligible roster | Entire call rejected at preflight, no partial dispatch |
| Agent times out (job accepted) | `[TIMEOUT — reply via: ssh mini mxr get <jid> --reply]` |
| Agent times out (not accepted) | `[TIMEOUT — job not confirmed (connection failed)]` |
| Oracle SSH host unreachable | `ConnectTimeout=10` expires → `[UNREACHABLE]` |
| SSH auth failure / host-key error | `BatchMode=yes` makes it noninteractive; SSH exits 255 |
| Oracle returns `MXR_DONE_EMPTY` | `[EMPTY — agent produced no reply]` |
| Reply contains forged headers | ANSI-stripped; content is indented after a fixed header line |
| `--with` contains duplicates | Deduplicate before preflight |
| Single agent in `--with` | Supported; functions as a bypass-free single dispatch |
| Ctrl-C during wait | Terminate all SSH subprocesses; print received replies; exit 130 |
| All agents fail/timeout | Exit 1 with per-agent notices |
| Job committed but ACK lost (job_id=None despite active job) | v1 known residual: distinguishing known rejection from unknown acceptance is complex. Surfaces as `error='UNREACHABLE'` with `job_id=None`. Recovery requires inspecting the Mini's ledger manually. Future work: add a submission-confirm round-trip. |

## Security surface

- **SSH injection**: mitigated via `shlex.quote()` + `--` option terminator (see above)
- **No free-form host**: `host` is registry-declared, not caller-supplied
- **Authority filtering**: WORKSPACE_ACTOR rejected at preflight
- **Transitive delegation**: CONTROLLER authority is rejected at preflight — v1 does not
  permit agents that can spawn child jobs via `submit_job`. `discuss` submits a plain-text
  prompt to each agent via the standard job submission path and does not grant new
  capabilities; it is a parallel dispatch shortcut. If a CONTROLLER-mode agent is needed
  in the future (e.g. lobster), the right path is a `RESPONDER`-mode profile in that
  agent's registry entry.
- **BYPASS propagation**: explicit literal constant on SSH path; not derived from user input
- **SSH unattended**: `BatchMode=yes`, `ConnectTimeout=10`, `stdin=DEVNULL`
- **Gate bypass for review-flavored discusses**: `discuss` is a broadcast shortcut — it sends
  the topic as-is to each agent. Whether the topic happens to sound like a code review request
  is the user's concern, not the command's. The review gate is a process-discipline tool applied
  by the hook infrastructure, not a semantic filter on question content. `discuss` is exempt
  because it produces no code artifact and has no diff context.

## Files to create / modify

| File | Change |
|---|---|
| `src/runtime/cli.py` | Add `discuss` subcommand; extract `_submit_and_fetch()` + `_remote_fetch()`; add `DiscussResult` |
| `src/runtime/registry.py` | Add `host: Optional[str] = None` to `AgentSpec`; set `host="mini"` on oracle |
| `src/runtime/contracts.py` | Verify `Authority` enum — no change expected |

No new files beyond the above, no schema changes, no new launchd services.

## Dependencies

- `asyncio`, `argparse`, `shlex`, `dataclasses` (stdlib)
- `PostgresLedger`, `REGISTRY` (existing runtime)
- `ssh` binary on PATH with BatchMode-compatible host config for Mini
- Mini reachable via Tailscale when oracle is in `--with`

## Out of scope (v1)

- Multi-round debate (agents see each other's replies)
- Synthesis step
- `--format` / `--output` flags
- Scheduling or persisting discussions as a named artifact
- Routing discuss to phone / RC surface

## Review checklist (pre-dispatch)

- [x] SSH injection: `shlex.quote()` + `--` on both agent name and topic
- [x] `BatchMode=yes`, `ConnectTimeout=10`, `stdin=DEVNULL` on SSH path
- [x] `MXR_REVIEW_GATE_BYPASS=1` prepended as literal constant on SSH command string
- [x] Local path uses Python submission directly — no hook fires
- [x] `DiscussResult` carries `job_id` before reply is awaited
- [x] `job_id` parsed from stderr `JOB_ID=` marker before reply wait
- [x] Timeout notice shows correct `mxr get` target (mini vs local)
- [x] All agents validated + authority-checked before any dispatch
- [x] Mixed eligible/ineligible roster rejected atomically
- [x] ANSI/VT100/OSC/raw control chars stripped via multi-pass regex before printing
- [x] Reply lines prefixed with 4-space indent to prevent forged header confusion
- [x] Only RESPONDER agents pass preflight (positive allowlist: `authority == RESPONDER`)
- [x] No `shell=True` on any subprocess call
- [x] `DiscussResult` (reply, error) state contract documented and mutually exclusive
- [x] Internal timeout handling in `_remote_fetch`/`_submit_and_fetch` (no external wait_for)
- [x] `asyncio.as_completed()` used so partial results survive KeyboardInterrupt
- [x] Subprocess teardown: terminate → 5 s → kill → wait (best-effort, bounded)
- [x] stdout capped at 512 KB, stderr capped at 32 KB, encoding errors='replace'
- [x] Stream readers cancelled after process exit
- [x] Remote eligibility limitation documented (v1 known gap)
- [x] ACK-loss / job_id=None case documented as v1 known residual
- [x] Transitive delegation: no new capabilities; future restriction belongs in runner
