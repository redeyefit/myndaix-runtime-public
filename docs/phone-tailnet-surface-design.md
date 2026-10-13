# Phone → Tailnet Surface — design v0.2

**Status:** DESIGN — v0.2 folds the full cross-family round (oracle lead + kilabz
trust-boundaries; all B/H/M/L items addressed below). Build gated on activation P0/P1
(merged review stack deployed, Mini pool re-canaried).
**What:** the vendor-free phone grip on the runtime — iPhone Shortcuts → tailnet SSH →
a forced-command wrapper on the Mini → allowlisted `mxr` verbs. Chosen over Claude
Remote Control as the PRIMARY phone surface (Jefe 2026-09-03; RC = optional bonus).
**Changelog:** v0.2 — TCB stated honestly (B1); env scrub spec (B2); `ask` made
async-capable for the iOS SSH timeout + new `get` verb (H1); exact grammar (H2);
leading-dash rejection (H3); `from=` key pinning + host-scoped ACL (H4); locked caps
+ status cap + concurrency cap (H5); perl-alarm OS timeout (H6); real-sshd test leg
(H7); reject-not-strip controls (M1); log policy (M2); partial-state table (M3); key
lifecycle (M4); verbatim-payload parse spec (L1); answer-first truncation (L2).

## 1. What it does & why

One tap or one dictation on the iPhone runs exactly one allowlisted action on the
factory, from anywhere, with no AI vendor in the transport:

- `ask <scope> <question>` → `mxr ask --scope <scope> -- "<question>"` → cited answer.
  Scopes: `research|fitness|company` (mirror of the librarian fence; `personal` stays
  off by standing decision). Bounded to the iOS window (§5); overflow returns a job id.
- `get <jid>` → fetch a completed answer the iOS timeout abandoned (`mxr get <jid>`).
- `reel <topic>` → `mxr mx-engine -- "<topic>"` → submit-and-return; render lands in
  the jefe drop as usual.
- `status` → bounded one-screen health summary.

Transport: iPhone Tailscale app (WireGuard node on the existing tailnet) → SSH to the
Mini. Nothing public, no new daemon — sshd and the tailnet already exist.

## 2. Data flow

```
iPhone Shortcut (dictation/tap; per-scope Shortcuts hardcode the scope word)
  └─ Run Script Over SSH  (Shortcuts' own ed25519 key; iOS kills the call ~60-120s)
       └─ tailnet (WireGuard; ACL pins phone → THE MINI HOST:22, not a tag)
            └─ sshd on Mini → authorized_keys restrict,from=<phone-ts-ip>,command=
                 └─ ~/.myndaix/bin/mxr-phone   (command-restriction layer — see TCB §3)
                      ├─ scrub env; fixed PATH; absolute /usr/local/bin-style mxr path
                      ├─ parse SSH_ORIGINAL_COMMAND per the GRAMMAR (§4); deny-by-default
                      ├─ flock'd cap check+increment BEFORE dispatch; concurrency cap
                      └─ perl-alarm-bounded exec of mxr with payload as ONE argv after `--`
```

## 3. Trust model (stated honestly — review B1)

This is a **command-restriction boundary, not privilege isolation**. The trusted
computing base is: sshd + its config (AcceptEnv must stay empty, PermitUserEnvironment
no — asserted by the test suite), the jefe login shell machinery, the wrapper itself,
the absolute-path `mxr` + its Python runtime, the state dir, and the downstream
engines (librarian, mx-engine). A wrapper or mxr bug is a bug in a jefe-owned process.
What the design buys: a phone key that cannot open a shell, run non-allowlisted verbs,
or reach any other host — NOT root/jefe separation. A dedicated low-priv user was
rejected (needs DB grants + secrets anyway; complexity without a boundary gain);
this trade is accepted and documented.

| Threat | Containment |
|---|---|
| Lost/stolen phone or copied key | `restrict,from=<phone tailnet IP>,command=` — no shell/pty/forwarding, wrong source IP denied even inside the tailnet (H4); verbs+caps bound the blast; revocation drill §7. |
| Dictated text = untrusted | GRAMMAR deny-by-default (§4); payload extracted VERBATIM via parameter expansion, passed as ONE argv after `--` (option-injection stop, H3); control chars REJECT the request outright — never stripped (M1). Downstream: `ask` hits the tool-less librarian (worst case wrong answer); `reel` topic reaches an LLM as copy, never shell. |
| Env-channel abuse | Wrapper first line of defense: `unset BASH_ENV ENV PYTHONPATH NODE_OPTIONS RUBYOPT PERL5LIB; unset $(compgen -v MXR_)`; fixed PATH literal; absolute mxr path (B2). sshd side asserted: AcceptEnv none. |
| Flooding / cost abuse | flock-guarded check+increment BEFORE dispatch (H5): reel 5/day (paid), ask 50/day, status 60/hour, `get` 100/day; global concurrency cap 2 (mkdir lock, stale-reaped). A failed dispatch after a paid increment wastes one slot — bounded, favors safety (M3). |
| Hung downstream | perl alarm+exec (the `cap_run` house pattern — macOS has no timeout(1)) bounds every dispatch; ask 50s, status 30s, get 30s, reel submit 60s; child killed on expiry (H6). |
| Log leakage | Logs record ts/verb/rc/payload-sha256-prefix+length — NEVER payload text (company/fitness content is sensitive, M2). 0600 perms, self-rotated (keep last 1000 lines on write). Log-write failure ⇒ DENY (fail-closed, consistent with caps). Output + logs escape ANSI/controls. |

## 4. Command grammar (exact — review H2; everything else denies)

```
status                                        # no args allowed
ask (research|fitness|company) <payload>      # scope REQUIRED; per-scope Shortcuts hardcode it
get <jid>                                     # jid =~ ^[0-9a-f-]{6,36}$
reel <payload>
payload := 1..2000 bytes, valid UTF-8, no control chars (reject, don't strip),
           not whitespace-only, first char != '-'
```
Parse spec (L1): verb/scope by exact-match on the first one/two words; the payload is
the VERBATIM remainder via parameter expansion (`${SSH_ORIGINAL_COMMAND#"$prefix"}`),
never word-split, never eval'd — quotes, `$()`, backticks, doubled spaces all travel
untouched into one argv. test.sh asserts byte-for-byte verbatim delivery of hostile
payloads (`$(rm -rf /)`, quotes, consecutive spaces) into the stub's recorded argv.

## 5. The iOS timeout reality (review H1 — oracle)

iOS Shortcuts' SSH action has a hardcoded ~60–120s kill. Design consequence: **no verb
may wait past ~50s.** `ask` waits ≤50s; if the answer isn't back, the wrapper returns
`still thinking — job <jid>; run Get Answer` and the `get` verb (or the "Get Answer"
Shortcut) fetches it. `reel` was already submit-and-return. `status`/`get` are fast.
Truncation (L2): 4KB cap keeps the ANSWER body first, drops citations from the end,
and always appends `…[truncated]` when cut.

## 6. Partial-state behaviors (review M3)

| State | Behavior |
|---|---|
| Cap incremented, dispatch fails | Slot wasted (bounded by caps); error returned; logged. |
| Wrapper killed after submit, before reply | Job completes in the ledger; `get <jid>` recovers (ask) / drop delivers (reel). |
| Shortcut auto-retry duplicates a reel | Two submits possible; bounded by the 5/day cap; mx-produce's single-holder lock rejects concurrent renders; residual accepted. |
| Reel submits, render fails later | Existing mx-engine semantics (non-retried, dead-lettered, drop notice). |
| Ask times out after tokens spent | Answer persists in ledger; `get` recovers it — cost not wasted. |
| No jid parseable from mxr output | Return raw (escaped) mxr output + error note; nothing hidden. |

## 7. Phone key lifecycle (review M4)

Shortcuts generates and holds its own ed25519 key (iOS keychain; never leaves the
device; only the pubkey is registered). One key per device, comment-tagged
`iphone-<date>`. Registration is ONE line in authorized_keys; the test suite asserts
no duplicate phone entries. Rotation = generate new in Shortcuts, swap the line.
Revocation drill (practiced at deploy): delete the line + remove the device in the
Tailscale admin — two actions, both work independently. Residual: a lost UNLOCKED
phone can ask/reel within caps until revoked — accepted (same class as the RC session).

## 8. Components

1. `orchestrator/phone/mxr-phone` — the wrapper (bash, house rules; §3-§6 behaviors).
2. `orchestrator/phone/authorized_keys.example` —
   `restrict,from="<PHONE-TS-IP>",command="/Users/factory/.myndaix/bin/mxr-phone" ssh-ed25519 <PUBKEY> iphone-<date>`
3. Tailnet ACL: phone device → the Mini HOST (by IP/name, NOT `tag:factory` — the tag
   may widen later, H4) port 22 only.
4. `orchestrator/phone/test.sh` — two legs: (a) fixture leg via SSH_ORIGINAL_COMMAND
   (grammar, hostile payloads verbatim, caps incl. a concurrent double-tap race, env
   scrub, control-char rejection, octal-trap counts, log redaction); (b) **real-sshd
   leg** (H7, run at deploy on the Mini against sshd on localhost): forced-command
   enforced, shell/pty/forwarding denied, AcceptEnv empty, env abuse inert, bad
   commands denied, duplicate-key absence, and the Shortcuts command-vs-stdin shape.
5. Shortcuts (phone, config not code): "Ask Research" / "Ask Company" / "Ask Fitness"
   (scope hardcoded per Shortcut — dictating the scope word was bad UX; wrapper still
   REQUIRES it), "Get Answer", "Make a Reel", "Factory Status".

## 9. What this deliberately does NOT build

No new daemon/queue/transport (sshd + tailnet exist; Telegram stays a separate
deferred design; RC untouched as bonus). No team→phone direction. No push
notifications. No new `.secrets` entries — but note honestly (M4): the phone key IS
a new credential; its lifecycle is §7, its registry is authorized_keys.

## 10. Decisions locked (was §9 open questions)

1. Caps: reel 5/day; status 60/hour (uncapped authenticated endpoint = DoS vector);
   ask 50/day; get 100/day; concurrency 2.
2. Scope: hard-required by the wrapper; UX solved by per-scope Shortcuts.
3. Log: `~/.myndaix/state/mxr-phone.log`, 0600, last-1000-lines self-rotation,
   sha-only payload references, write-failure = deny.
