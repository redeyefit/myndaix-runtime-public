# Telegram Transport — DESIGN (v0.2)

_A new **transport adapter** on the existing ledger spine — NOT a new system. The runtime already
defines the transport contract (`transport/terminal.py`, the "dumb pipe over the ledger");
Telegram is a second implementation of exactly that interface, so the spine, admission checks,
outbox, and agent pool are untouched by construction. This is the biggest remaining Factory-OS
gap: phone access to the autonomous brain. Follows the new-systems rule (research → design → BOTH
kilabz+oracle before code)._

_**v0.2 folds the kilabz (codex) design review — NEEDS-REVISION, 7/7 accepted.** The v0.1
"read-only agents" framing was NOT enforceable: `lobster` is `CONTROLLER` authority (may emit
dispatches) and `recon` actually SPENDS (perplexity, and `cost_budget` isn't even enforced by the
ledger). So v1 shrinks to **NOTIFY + `/status` + `/help` — no agent dispatch at all** — and the
real enforcement mechanism becomes a HARD authority-admission check (§4) for when dispatch is added
later. Also folded: offset must persist on EVERY terminal decision incl. drops (else rejected
updates wedge the poller); Telegram jobs need queue isolation (they'd else starve the shared review
pool); outbound needs lease/failure handling + reply_target validation; `/status`/alerts need a
strict redaction contract. Status: pre oracle review (kilabz done)._

## 1. What & why

Today Jefe reads verdicts only when Mack relays them from `~/.myndaix/bridge/inbox/jefe/`, and can
only reach the runtime from a terminal. The phone is the missing surface. A Telegram bot gives a
two-way channel over the tailnet: the brain pushes verdicts/alerts to Jefe's phone, and Jefe can
read status from anywhere.

**The load-bearing scope decision (v1): NOTIFY + STATUS ONLY — no agent dispatch at all.** An
inbound Telegram message is untrusted text entering a system that can spend money, write code, and
merge PRs. v1 mirrors how every rung here was built — observe/notify first (safe), act later
(gated): v1 lets the phone (a) receive verdict/alert SUMMARIES (§5 redaction), and (b) run
`/status` (a DIRECT, redacted ledger read — no agent, no spend) and `/help`. **No message dispatches
any agent in v1.** The kilabz review made the reason concrete: the v0.1 "read-only agents" idea was
un-enforceable — `lobster` is `CONTROLLER` authority (may emit new dispatches) and `recon` is a
paid API agent whose `cost_budget` the ledger does not even enforce. So there is no genuinely
safe agent to expose yet; agent dispatch (`/ask`, `/research`) waits for §4's hard authority gate
PLUS either a real `RESPONDER`-only non-paid agent or an enforced budget — and, for anything
consequential, the **approvals primitive** (§8, v2). "The human flips the switch" stays intact:
in v1 the phone can only *read*, never dispatch or act.

Non-goals (v1): webhooks; inline keyboards/buttons; multi-user/group chats; media upload; ANY
agent dispatch (deferred to v2 behind §4 + budget/approvals); arbitrary `to_agent` from the
message; editing/deleting sent messages; a second autonomous git writer.

## 2. Transport = polling, never a webhook (the NAT/Tailscale-forced, and safer, choice)

The Mini is behind NAT on the tailnet — it has **no public HTTPS endpoint**, which a Telegram
webhook requires. Long-polling (`getUpdates` with a timeout) is the correct and only fit: the bot
makes an **outbound** HTTPS call to `api.telegram.org` and Telegram holds it open until an update
arrives or the timeout elapses. No inbound port, no public surface, no reverse proxy — the attack
surface is strictly "what Telegram hands back to a poll we initiated." (Confirmed against current
Bot-API guidance: long-poll is the standard behind-NAT pattern; webhooks are for public hosts.)

Exactly-once ingest rides Telegram's own `update_id`: `getUpdates(offset=last+1)` **acknowledges**
everything below `offset`, and each update's `update_id` is monotonic per bot. So `update_id`
**is** the `dedupe_key` — the same restart-stable, never-colliding property the terminal transport
gets from `uuid4()`, but here it also gives crash-safe at-least-once delivery from Telegram +
exactly-once *processing* via the ledger's `inbound_event.dedupe_key UNIQUE`.

**Offset advances after EVERY terminal decision, not only after a successful ingest (kilabz #3).**
The v0.1 draft advanced offset only after `ingest_inbound`; but a dropped update (unauthorized
chat, non-text, unknown command) has no ingest, so it would be returned by every subsequent
`getUpdates(offset=last+1)` FOREVER — re-dropping it and blocking every later update behind it (a
poller wedge). Rule: `offset = update_id` is persisted once the update reaches ANY terminal
outcome — ingested, or deliberately dropped. Ingest is still committed BEFORE its offset write so a
crash mid-ingest re-delivers (UNIQUE dedupe collapses it); a drop's offset write has nothing to
lose. Offset lives in a durable `telegram_offset(account, last_update_id)` row (one per bot).

## 3. Architecture (mirrors `TerminalTransport` exactly)

```
INBOUND (a poller task, one per bot account):
  loop:
    updates = GET api.telegram.org/bot<token>/getUpdates?offset=<last+1>&timeout=50
              &allowed_updates=["message"]           # ONLY message updates; ignore edited/callback/etc
    for u in updates (ascending update_id):
      try:
        m = u.message
        if m.chat.id NOT in CHAT_ALLOWLIST:   log+drop (fail-closed authN — §5)          # NO reply
        elif m.text empty / not a text msg:   drop (v1 = text only)
        else:
          cmd, arg = parse_command(m.text)           # v1: /status | /help  (NO agent dispatch)
          handle_locally(cmd, arg)                    # /status = redacted ledger read; else usage hint
      finally:
        persist offset = u.update_id                  # ADVANCE ON EVERY TERMINAL DECISION incl. drops
                                                       # (kilabz #3 — else a dropped update wedges the poll)

OUTBOUND (a delivery task, mirrors TerminalTransport.run_delivery):
  loop (until stop, then drain):
    msg = claim_outbound("telegram")                  # transactional outbox
    if not msg.reply_target.startswith("telegram:") or not valid_chat(msg): 
        mark_outbound_failed(msg.id); dead_letter(msg.id, "bad telegram target"); continue  # kilabz #6
    try:
        r = POST .../sendMessage {chat_id: msg.reply_target.split(":",1)[1], text: chunk}
        if r.ok: mark_outbound_sent(msg.id, provider_msg_id=<telegram message_id>)
        else:    mark_outbound_failed(msg.id)         # 4xx/5xx/timeout — NEVER leave leased (kilabz #5)
    except (timeout, network):
        mark_outbound_failed(msg.id)                  # every non-success path marks failed
```
Note the ledger presently has NO outbound-lease expiry, so a crash between `claim_outbound` and
`mark_*` strands a row in `leased` (kilabz #5). v1 wraps every send so the process itself never
leaks a leased row; the **belt** (a real outbound reclaim in `reclaim_expired`, symmetric with
attempt leases) is a small spine follow-up flagged in §9 — not Telegram-specific, but this
transport is the first to exercise the gap.

Plus a **verdict bridge** (the highest-value half): the jefe-inbox verdict drop and controller
`_alert_jefe` also enqueue an `outbound` row with `transport="telegram"`, `reply_target=
"telegram:<jefe_chat>"` — so alerts/verdicts reach the phone through the SAME outbox path, no
special case. (Mack still relays substance in-terminal; this is additive.)

Runs as its own launchd job `ai.myndaix.telegram` (a poller + delivery loop), sourcing the bot
token from `~/.myndaix/.secrets` — **separate** from serve/controller/automerge so a poller crash
never touches the brain, and it can be disarmed independently (`rm $ORCH/TELEGRAM_ENABLED`).

## 4. Command grammar (v1 — closed allowlist, NO agent dispatch)

| Command | Handler | Spend | Notes |
|---|---|---|---|
| `/status` | direct ledger read (no agent) | none | redacted summary: queued/running counts, review-cursor SHAs, last N verdict HEADLINES (never bodies — §5). |
| `/help` | local string (no agent) | none | lists these commands. |

v1 dispatches NO agent (kilabz #1/#2 killed the "read-only agent" idea — see below). The phone
reads; it does not run agents. A per-chat **rate limit** (token-bucket, e.g. 20 msgs / 5 min,
reuses the automerge/play-review cap pattern) still bounds abuse even from the allowlisted chat,
and a hard cap on `/status` result size bounds the read.

### The authority-admission gate (the enforcement mechanism for when v2 adds dispatch)

The v0.1 draft said "read-only agents only" but that is not something the registry can express or
`submit_job` enforce — it queues any `to_agent` string, `lobster` is `CONTROLLER` (may emit
dispatches), and `recon` is a **paid** API agent whose `cost_budget` the ledger does not enforce.
So a fixed command→agent map is necessary but NOT sufficient. When v2 adds any dispatching command
it MUST pass a hard, positive admission check at the Telegram boundary, BEFORE `submit_job`:

```
TELEGRAM_DISPATCHABLE = agents where ALL hold:
  spec.authority == Authority.RESPONDER          # never CONTROLLER / WORKSPACE_ACTOR
  AND spec.adapter.get("non_idempotent") is not True   # never a paid/charging submit
  AND agent_id in an explicit TELEGRAM_AGENT_ALLOWLIST  # positive allowlist, not a denylist
  AND (reach == CLI  OR  an ENFORCED per-transport budget exists)   # no unmetered paid API
```

No agent in the current registry satisfies this for a *useful* phone query (kilabz #1/#2): the
review agents are RESPONDER but their point is reviewing a diff, not answering a phone; `recon` is
paid+unenforced. So v2 dispatch is gated on FIRST landing one of: (a) a genuinely `RESPONDER`,
non-paid Q&A agent, or (b) an **enforced** `cost_budget` in the ledger's admission checks (today
only depth/child limits are enforced — a real gap). Until then, notify + `/status` is the whole
surface, and it is fully safe by construction because it never dispatches.

## 5. Security surface & failure modes (the core of this design)

- **AuthN = chat_id allowlist, fail-closed.** A public bot receives messages from anyone who finds
  its handle. The ONLY trust boundary is `m.chat.id ∈ CHAT_ALLOWLIST` (Jefe's chat id(s), from a
  `chmod 600` config, not the code). Everything else is logged-and-dropped BEFORE any ingest,
  submit, or reply — an unknown sender gets **no response at all** (no oracle for the bot's
  existence, no error that confirms the id). Empty allowlist → the poller refuses to start
  (fail-closed, like the API's empty-keys rule).
- **AuthZ = no dispatch in v1; a hard authority gate for v2 (kilabz #1/#2).** v1 exposes NO agent,
  so an owned phone / shoulder-surfed unlock can at worst read a redacted `/status`. When v2 adds
  dispatch it passes §4's positive admission gate (`RESPONDER` + not `non_idempotent` + explicit
  allowlist + CLI-or-enforced-budget) BEFORE `submit_job` — because the registry cannot express
  "read-only" and `submit_job` queues any `to_agent`, so enforcement lives at the boundary, not in
  a prose promise.
- **Queue isolation — Telegram jobs must not starve the review pool (kilabz #4).** `serve` runs ONE
  shared worker pool leasing by `priority DESC, created_at`, and a NULL-`repo_id` job is cap-exempt.
  A v2 Telegram dispatch would otherwise let a (compromised, allowlisted) phone flood workers and
  delay the autonomous reviews — the brain's core job. So any Telegram-originated job MUST run at a
  LOW priority (below reviews), carry a short timeout, and be bounded by a **per-transport
  concurrency cap** (a `repo_id`-style bucket keyed on the transport, reusing the per-repo cap
  machinery) or a dedicated small pool. Designed now so v2 can't regress it; irrelevant to v1 (no
  dispatch) but a hard prerequisite for v2.
- **Injection (v2).** When dispatch exists, message text → job prompt is wrapped in a nonce fence as
  UNTRUSTED DATA, objective above the fence, admitted agents have no write/merge/dispatch tools. A
  phone message never parameterizes `to_agent` or a shell command — it fills only the `arg` slot of
  a fixed command.
- **Bot token = a bearer secret.** In `~/.myndaix/.secrets` (600), sourced into the launchd job's
  env, never in the plist, never logged. Token compromise lets an attacker *impersonate the bot*
  (send as it, **read its updates + read whatever the runtime sends**) — which is exactly why the
  redaction contract below is load-bearing. The chat allowlist still gates who the runtime obeys.
  Rotate via BotFather on suspicion.
- **Redaction contract for `/status` + the verdict/alert bridge (kilabz #7).** Raw `get_status`
  includes attempt text + outbound bodies, and `_alert_jefe` writes arbitrary body text. Telegram is
  a third party AND a phone/token compromise reads everything sent. So a **Telegram-specific summary
  schema** is mandatory: forward ONLY headlines/counts/SHAs/verdict-labels — **never** raw prompts,
  diffs, attempt text, outbound bodies, secrets, `artifact_ref` URLs, or finding bodies. Every
  verdict/alert crossing to `transport="telegram"` passes through a `redact_for_telegram()` allowlist
  projection; the full substance stays in the jefe inbox (which Mack relays in-terminal). "Details in
  the inbox" is the default, not raw content.
- **Reply-target validation (kilabz #6).** `claim_outbound("telegram")` filters only on `transport`,
  not that `reply_target` is well-formed. The delivery loop MUST validate `reply_target` starts with
  `telegram:` and the chat id is numeric/allowlisted, and **dead-letter** any malformed row rather
  than `split(":")[1]`-crash or mis-send.
- **Availability / isolation.** The poller is its own launchd job; a hung `getUpdates`, a Telegram
  outage, or a poll crash cannot delay a review or wedge the brain (hard isolation, like the
  capture rung's fail-open). Bounded HTTP timeouts on every Bot-API call; every `sendMessage` marks
  the outbound row sent-or-failed so a crash after `claim_outbound` never strands a `leased` row
  (kilabz #5), with a spine-level outbound reclaim as the belt (§9).
- **Dedup / replay.** `update_id` UNIQUE dedupe_key makes a re-delivered update a no-op; offset
  advanced only post-commit means a crash re-processes at-least-once → the UNIQUE collapses it to
  exactly-once. A replayed old `update_id` (< offset) is never returned by `getUpdates` anyway.
- **Reply mis-routing.** `reply_target = "telegram:<chat_id>"` is derived from the INBOUND chat;
  the delivery loop validates + dead-letters any non-`telegram:<numeric-allowlisted-chat>` target
  (above) so a reply can't leak to another transport or chat. (The audit's InboundIn NUL-guard gap
  does not apply — this transport builds the envelope in-process, not via the HTTP `/inbound` body.)

## 6. Edge cases

- **Long message / long reply.** Telegram caps `sendMessage` text at 4096 chars → the delivery
  loop chunks a long verdict into multiple sends (ordered), or truncates with "…(details in
  inbox)". Inbound over the agent body cap → reject with a hint.
- **`getUpdates` 409 (conflict).** Two pollers or a stale webhook → Telegram returns 409. Fail
  closed: log, back off, ensure only one poller + `deleteWebhook` on startup.
- **Bot added to a group.** `chat.id` won't be in the allowlist → dropped. (allowed_updates also
  excludes `my_chat_member` churn.)
- **Clock/token expiry.** Bot tokens don't expire; a revoked token → 401 on every call → the
  poller alerts (via the still-working inbox path) and exits non-zero for launchd to surface.

## 7. Borrow / build / reject

| Piece | Verdict |
|---|---|
| the transport interface (ingest/deliver over the ledger) | **REUSE** — `TerminalTransport` is the template, unchanged spine |
| long-poll `getUpdates` + `update_id` dedupe | **BORROW pattern** (Bot API idiom; ~a thin httpx loop) |
| chat_id allowlist authN + redaction contract (v1's whole security model) | **BUILD** (tiny) |
| authority-admission gate + per-transport queue cap (v2 dispatch prerequisites) | **BUILD when v2 dispatch lands** (§4/§5) |
| a Telegram bot framework (python-telegram-bot, aiogram) | **REJECT for v1** — a raw `getUpdates`/`sendMessage` httpx loop is ~80 lines and adds no dependency/attack surface; frameworks bundle webhook servers, handlers, and update types we deliberately don't want. Revisit only if inline-keyboard approvals (v2) justify it. |
| webhook / public endpoint | **REJECT** — NAT + larger surface |
| the approvals primitive | **DEFER to v2** (§8) — do not build consequential-action dispatch until it exists |

## 8. The approvals primitive (v2 sketch — do NOT build yet)

When the phone should be able to trigger a consequential action (approve an auto-fix, greenlight a
paid render, merge a flagged PR), it goes through an **approvals table** — the missing Factory-OS
primitive:

```
approval { id, requested_by (agent/controller), action (enum), payload jsonb, state
           (pending|approved|denied|expired), decided_by, decided_at, ttl }
```

Flow: the brain (or a phone command) STAGES an `approval` in `pending`; the phone gets a message
with a short token; Jefe replies `/approve <token>` or `/deny <token>`; a deterministic executor
(not an LLM) performs the pre-registered action ONLY on `approved`, once, then marks it terminal.
Same asymmetry as the autonomy ladder: staging is cheap, execution is human-gated, TTL auto-denies.
This is where inline keyboards (approve/deny buttons) may earn the framework dependency. Designed
separately, cross-family reviewed, after v1 proves the transport is safe and useful.

## 9. Build plan (feature-flagged, each PR reviewed)

- **PR-A** `transport/telegram.py` (poller + delivery, raw httpx `getUpdates`/`sendMessage`),
  `telegram_offset(account, last_update_id)` row + **offset-on-every-terminal-decision** (incl.
  drops), chat allowlist (fail-closed), `/status`+`/help` local handlers, `redact_for_telegram()`,
  reply_target validation + dead-letter, outbound sent-or-failed on every path. Pure parts
  unit-tested: allowlist fail-closed, unknown-command, oversize, redaction projection, offset
  advances on a dropped update, malformed reply_target dead-lettered.
- **PR-B** launchd job `ai.myndaix.telegram.plist.example` + secret wiring + `TELEGRAM_ENABLED` arm
  + the verdict/alert bridge (enqueue a REDACTED `telegram` outbound alongside the inbox drop).
- **Spine follow-up (own PR, not Telegram-specific):** outbound-lease reclaim in `reclaim_expired`
  (symmetric with attempt leases) so a stranded `leased` outbound row self-heals (kilabz #5 belt).
- **PR-C (v2, separate, gated):** the authority-admission gate + per-transport queue cap + the
  FIRST dispatch command — only after a `RESPONDER`-non-paid Q&A agent or an enforced `cost_budget`
  exists. Then the approvals table + `/approve`,`/deny` + the deterministic executor (§8).

Deploy: install token → arm `touch $ORCH/TELEGRAM_ENABLED` → the poller starts; disarm = rm + the
launchd job exits no-op. **v1 ships the notify + `/status` value with ZERO dispatch/write/spend
surface** — safe by construction because no message runs an agent.
