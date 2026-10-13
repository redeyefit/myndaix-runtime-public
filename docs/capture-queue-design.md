# DESIGN — Capture Queue v0.2

**Status:** DESIGN — KilaBz NEEDS-REVISION addressed; ready for /feature.
**Changelog:** v0.2 — corrects endpoint name (POST /jobs → 201); adds trusted dispatcher
for Lobster triage output; removes unworkable Mac fallback; fixes Ghostty mechanism; adds
dedup semantics; corrects auth-scope exposure; corrects payload-validation claims.
**What:** A quick-capture system for ideas and tasks from two surfaces: iOS Shortcut (anywhere,
via Tailscale) and Mac Ghostty keybinding (terminal busy). Both submit to the Mini's existing
runtime HTTP API; Lobster triages and routes each capture to the right agent via a trusted
dispatcher (not inline in the HTTP handler).

## 1. What it does & why

When Jefe has a task or idea and can't type into the terminal (busy or away), he needs a
frictionless capture path that doesn't require a free terminal or proximity to the Mac.

One tap on the phone or one keybinding on the Mac queues the text. The Mini (always-on)
picks it up and Lobster triages it: routes to the right agent, parks it for later, or logs
it as an idea.

## 2. Data flow

```
iPhone Shortcut (dictation / text input)
  └─ POST /submit  (Bearer token; Tailscale → Mini runtime API port 8080)
       └─ runtime ledger: job { to_agent: lobster, prompt: "CAPTURE: <text>", context: {source: ios} }
            └─ worker picks up → mxr lobster → triage + route

Mac Ghostty keybinding
  └─ curl one-liner → same POST /submit endpoint (local or Tailscale)
       └─ same path, context: {source: mac}
```

## 3. Components

**3a. Runtime endpoint**

The existing `POST /jobs` on `src/runtime/api.py` accepts `{to_agent, prompt, context}` and
returns 201 + job_id. We submit to `lobster` with a structured prompt:

```
POST /jobs
{
  "to_agent": "lobster",
  "prompt": "CAPTURE TRIAGE — return JSON only: {\"category\": <one of: task|research|idea|feedback|unclear>, \"route_to\": <agent_id or null>, \"summary\": <≤80 chars>}\n\nCapture:\n<user text>",
  "context": { "source": "ios|mac", "capture_id": "<uuid>" }
}
```

No new endpoint. No new migration. Additive use of existing verb.

**3b. Trusted dispatcher (new — the missing component from v0.1)**

Lobster is confined (Read/Glob/Grep only); it cannot shell-dispatch or write files. Its job
result is structured JSON (category + route_to + summary). A separate trusted component
reads that output and acts via Command API verbs:

- `task/research/feedback` + `route_to` set → submit a new child job via `POST /jobs` to the
  named agent, using the Lobster summary as prompt prefix + original text appended
- `idea` → write to `~/ideas/YYYY-MM-DD.md` via the dispatcher script directly (not an agent)
- `unclear` → write to `~/queue/unrouted/<capture_id>.md` for next-session surface
- `route_to` unknown / category outside allowlist → fail-closed to `unclear`

The dispatcher is `~/.myndaix/bin/capture-dispatch.sh`. It polls the Lobster job to
completion (bounded: 120s timeout), then executes exactly one Command API call or file write
based on an allowlisted category→action map. Never eval's Lobster output as shell.

**Dispatch lineage:** child jobs set `context.parent_capture_id = <capture_id>` for
traceability. Child jobs are new root jobs (not ledger-linked children) — the ledger's child
admission requires a live parent, which Lobster's completed job is not.

**3c. iOS Shortcut**

- Input: "Ask Siri" or tap → text field
- Generates a UUID as `capture_id` (Shortcut's "Generate UUID" action)
- Action: "Get Contents of URL" → `POST http://<mini-tailscale-ip>:8080/jobs`
- Headers: `Authorization: Bearer <capture-token>`, `Content-Type: application/json`
- Body: JSON built from Shortcut variables — text sanitized (Shortcut strips leading/trailing
  whitespace; script validates non-empty before POST)
- Response: show `job_id` (accepted); on timeout: store original text in Notes as fallback

**3d. Mac keybinding**

Ghostty `keybind = <chord>=new_tab:capture` is NOT the right mechanism — the `text` action
sends to the foreground process. Instead: a macOS global hotkey via **Hammerspoon** (or
macOS Shortcuts app) that fires regardless of active app:

```lua
-- ~/.hammerspoon/init.lua
hs.hotkey.bind({"cmd","shift"}, "space", function()
  local text = hs.dialog.textPrompt("Capture", "Task or idea:", "", "Send", "Cancel")
  if text ~= "" then
    -- calls ~/.myndaix/bin/capture.sh <text>
    hs.task.new("/Users/you/.myndaix/bin/capture.sh", nil, {text}):start()
  end
end)
```

`capture.sh` validates input, generates a UUID, and POSTs to `POST /jobs` on the Mini.
No Mac fallback to local `mxr` — if Mini is unreachable, the script prints the error and
preserves the text in `~/queue/unrouted/offline-<timestamp>.md` for manual retry.

**3e. Auth**

One new `MYNDAIX_API_KEYS` entry: `<capture-token>:jefe-capture:client`. The `client` role
can submit to ANY agent — this is documented exposure, not a security boundary:

- The token is Jefe-held, Tailscale-gated, and the capture surface is Jefe-only
- Client-side, the script hardcodes `to_agent: lobster` — server-side there is no per-principal
  agent restriction (v1 accepted; add if surface widens beyond Jefe)
- A leaked token could submit directly to builders — blast radius is a queued job, never
  code execution. Documented, not minimized.

Token stored in `~/.myndaix/.secrets` (0600). iOS: stored in the Shortcut's text field —
NOT automatically a keychain item; document this and add to key lifecycle (§7).

**3f. Payload validation**

Raw capture text (before prompt prefix) must be validated at the client:
- Non-empty after whitespace strip
- Max 2000 bytes (UTF-8 encoded)
- No NUL chars, no control chars (reject, don't strip — same policy as mxr-phone)
- Quotes, newlines, emoji, `$()`, backticks stay as data — passed via JSON body field,
  never interpolated into shell or AppleScript strings

The runtime's `min_length=1` accepts whitespace and applies to the full prefixed prompt;
client-side validation is the real gate on the raw text.

## 4. Edge cases & failure modes

| Scenario | Behavior |
|---|---|
| Mini unreachable (phone) | POST fails (timeout/refused); Shortcut shows error; original text saved to Notes; user retries manually |
| Mini unreachable (Mac) | `capture.sh` error + text written to `~/queue/unrouted/offline-<ts>.md`; no silent loss |
| Empty / whitespace input | Client-side validation rejects before POST; fast, no ledger write |
| POST accepted, response lost | `capture_id` UUID is client-generated; same `capture_id` can be resubmitted — dispatcher checks for existing Lobster job by `capture_id` in context and skips if already processed |
| Lobster triage fails / times out | Dispatcher writes to `~/queue/unrouted/<capture_id>.md`; user sees it next session |
| Dispatcher reads ambiguous Lobster output | Fail-closed to `unclear` / unrouted; never guess a route |
| Lobster routes to unknown agent | Fail-closed to `unclear` — route_to validated against registry allowlist |
| Auth token leaked | Rotate: edit `MYNDAIX_API_KEYS` on Mini → restart runtime API → update `~/.myndaix/.secrets` on Mac → update iOS Shortcut text field |

## 5. Security surface

- **Transport:** Tailscale (WireGuard, existing tailnet). Verify Mini's Tailscale ACL restricts
  phone and Mac to the Mini's port 8080 only — enrollment alone does not configure this.
- **Auth:** Bearer token; `jefe-capture` client role. Client can submit to any agent — this is
  known exposure documented in §3e. Not a security boundary; a trust boundary.
- **Payload:** Jefe-authored text, not LLM output, not external data. Client validates before
  POST (§3f). Dispatcher never eval's Lobster output as shell — routes via allowlisted map only.
- **No new daemon / port.** Runtime API already runs on Mini port 8080.
- **Blast radius:** a queued job routed to an agent. A mis-routed job wastes an agent run.
  A leaked token + direct builder submit = a queued builder job (not code execution; recoverable).
  Honestly: "worst case mis-routing" understated it; this is the correct picture.

## 6. What this deliberately does NOT build

- No new HTTP server (uses existing runtime API)
- No file-drop / folder watcher (no polling, no iCloud sync dependency)
- No push notifications back to phone (out of scope v1)
- No web UI / dashboard
- No new persistence layer (ledger already covers it)
- No integration with phone-tailnet-surface SSH system (complementary, different verbs)

## 7. Relationship to phone-tailnet-surface-design.md

`phone-tailnet-surface-design.md` uses SSH forced-command for a RESTRICTED verb set
(ask/reel/status/get). This design uses HTTP for open-text capture only. They are
complementary and do not conflict. The capture token is a separate credential from the
SSH phone key.

## 8. Decisions

1. Use existing `POST /jobs` → 201 — no new endpoint, no migration, additive.
2. Tailscale for transport — already enrolled on all three devices; verify ACL §5.
3. One shared Bearer token for capture — accepted exposure documented in §3e; per-device
   keys are a future upgrade if the surface widens.
4. Lobster triage as standard worker job; trusted dispatcher reads the result separately —
   keeps Lobster confined, decouples HTTP latency from triage execution.
5. No Mac fallback to local mxr — too complex (ledger split, sync semantics); offline writes
   to `~/queue/unrouted/` instead.
6. Hammerspoon for global Mac hotkey — Ghostty `text` action sends to the foreground process,
   which is wrong when the terminal is busy. Hammerspoon fires regardless of active app.
7. Dispatcher fail-closed to `unclear` on any ambiguity — never guess a route.

## 9. Build scope (what /feature implements)

1. `~/.myndaix/bin/capture.sh` — client-side validation + UUID generation + `POST /jobs` +
   offline fallback to `~/queue/unrouted/offline-<ts>.md`
2. `~/.myndaix/bin/capture-dispatch.sh` — polls Lobster job to completion, validates output
   against allowlist, executes one action (POST /jobs child | file write | unrouted park);
   never evals Lobster output as shell
3. Hammerspoon hotkey (`~/.hammerspoon/init.lua`) — global `Cmd+Shift+Space` dialog → calls
   `capture.sh` (requires Hammerspoon installed; instructions included)
4. `MYNDAIX_API_KEYS` entry + `~/.myndaix/.secrets` update on Mini + Mac
5. Tailscale ACL verification (document the required Mini port 8080 ACL entry)
6. iOS Shortcut instructions (manual — UUID generation + POST to Mini; capture-token stored
   in Shortcut text field with documented risk)
7. `test.sh` — validate: (a) POST /jobs returns 201 + job_id; (b) Lobster job reaches done;
   (c) dispatcher routes a task to mini; (d) dispatcher parks unclear to unrouted;
   (e) hostile text (quotes, $(), backticks) survives as data end-to-end;
   (f) empty/whitespace input rejected before POST; (g) duplicate capture_id not re-dispatched
