# Runbook — Deploy the Inbox Assistant to the Mini

**Status:** NOT EXECUTED. This is the deploy contract — Jefe executes it top to bottom on the
Mini. Design: `docs/inbox-assistant-design.md`. One-time cloud setup (steps 1–6) happens on the
MacBook in a browser; the arm (step 7) is a normal merge — the substrate reconcile does the rest.

**Goal:** 3 Gmail inboxes → one 06:30 brief in the jefe drop, labels applied, reply drafts
created. **SEND IS NEVER CALLED** — v1 is drafts-only by code contract (`gmail.compose` permits
send; no send method is invoked anywhere). Everything reversible; per-account fail-closed.

## 1. GCP project + OAuth client (browser, MacBook)

1. **Log in as a Workspace account on your own domain FIRST, then create the project** (e.g. `inbox-assistant`).
   A project owned by the Workspace account keeps the "Internal" audience option open if we ever
   want it; harmless otherwise. Creating it under the consumer account forecloses that forever.
2. **Enable APIs:** APIs & Services → Library → enable **Gmail API** and **Google Drive API**.
3. **OAuth consent — Google Auth Platform → Audience page.** The console was reorganized in 2025;
   old screenshots/tutorials showing "OAuth consent screen" under APIs & Services lie
   (verified 2026-07, Google docs). Configure:
   - User type: **External**.
   - Scopes (Data Access page) — add exactly these three:
     ```
     https://www.googleapis.com/auth/gmail.modify
     https://www.googleapis.com/auth/gmail.compose
     https://www.googleapis.com/auth/drive.file
     ```
     Why `gmail.modify`: label APPLICATION requires it (`gmail.labels` alone cannot
     `messages.batchModify`, and it supersedes `gmail.readonly` too); its extra powers
     (trash/archive/mark-read) are banned by code contract with a source-scan test, exactly
     like drafts-only bans send.
   - **'Publish app'** → confirm past the "will require verification" warning. Status must read
     **'In production'**.
   - **NEVER leave it in Testing** — Testing-status refresh tokens expire after 7 days, which
     would silently kill all three accounts weekly (verified 2026-07, Google OAuth docs).
   - **NEVER submit for verification.** An unverified in-production app used only by its owner
     rides the personal-use exemption — that is exactly what keeps the restricted Gmail scopes
     free of the security assessment (verified 2026-07, Google OAuth verification FAQ). Submitting
     starts a process we do not want and cannot cheaply exit.
4. **Create the OAuth client:** Clients page → Create client → type **Desktop app**. The client
   secret is shown **ONCE** — post-June-2025 the console never redisplays it (verified 2026-07,
   Google Cloud release notes). Put `client_id` + `client_secret` straight into 1Password
   (step 3 below); do not park them in a file.

## 2. Workspace trust (optional but recommended)

Workspace Admin console (admin.google.com) → Security → API Controls → **App Access Control** →
add our client ID and mark it **Trusted**. Removes the unverified-app interstitial AND the
unverified-app user cap for the two Workspace accounts (verified 2026-07, Google Workspace Admin
docs). The consumer account can't be trusted this way — it clicks through the interstitial once
at mint (step 4).

## 3. 1Password — ORDER OF OPERATIONS (grants are immutable; vault FIRST)

A service account's vault grants are fixed at creation — create the vault and items BEFORE the
service account, or you re-create the SA (verified 2026-07, 1Password docs).

1. **Create custom vault `Automation`.** Service accounts cannot read built-in Personal vaults
   (verified 2026-07, 1Password docs) — a custom vault is mandatory, not hygiene.
2. **Create the items** (the vault contract the code reads via `op read`):
   - `gmail-oauth-client` — fields `client_id`, `client_secret` (from step 1.4).
   - `gmail-rt-<email>` — ONE item per account, item name is literally `gmail-rt-` + the full
     email address; field `refresh_token` (written — or the whole item created — by the mint
     script in step 4).
   - `notion-inbox-assistant` — field `token` (filled in step 6; only if the Notion mirror is on).
3. **THEN create the service account**, read-only on that one vault:
   ```
   op service-account create inbox-assistant --vault Automation:read_items
   ```
4. **The SA token is shown once.** Store a copy in 1Password AND install it on the Mini's login
   Keychain (the tick script exports it as `OP_SERVICE_ACCOUNT_TOKEN` from there):
   ```
   security add-generic-password -a "$USER" -s 'op.inbox-assistant.token' -T /usr/bin/security -U -w
   ```
   - bare `-w` (no value): `security` prompts for the token interactively — it never lands in
     argv, `ps`, or shell history.
   - `-T /usr/bin/security`: pre-authorizes reads by the `security` binary itself, so the
     headless 06:30 launchd run cannot hang on a keychain ACL confirmation prompt.
   - `-U`: update-in-place if the item already exists — the command is safe to re-run.

   **Caveat:** after an unattended reboot without auto-login the login keychain is locked and the
   tick fails (`op` errors) until someone logs in. Known trade-off; the brief just skips days.

## 4. Mint the refresh tokens (MacBook — a browser is required)

Run `scripts/mint_gmail_refresh_token.py` once per account (3x), logged into that account in the
browser:

```
python3 scripts/mint_gmail_refresh_token.py --client-id <id> --op-vault Automation \
    --account <email> --check
```

With `--op-vault` the script stores the token STRAIGHT into the `gmail-rt-<email>` vault item
(creating the item if it doesn't exist yet) and prints only the secret reference
(`op://Automation/gmail-rt-<email>/refresh_token`) — the raw token never hits stdout, terminal
scrollback, or the clipboard. (It does transit the local `op` process argv briefly — operator's
own machine, subprocess arg list, no shell.) Printing the raw token requires an explicit
`--print` (vault-less runs only) and warns on stderr.

The consumer account hits "Google hasn't verified this app" → **Advanced → Go to app (unsafe)**
— expected, click through. The Workspace accounts skip the interstitial if step 2 was done.

**Scopes are frozen at mint** — the token carries exactly the scopes consented at mint time.
`drive.file` is in the list NOW deliberately, even before the Drive mirror is armed: adding a
scope later means re-minting all three tokens. Do not trim the scope list to "what we use today."

## 5. Mini config — `~/.myndaix/config.env`

> **⚠️ ORDER: MERGE FIRST (step 7.1), THEN add these keys.** The `INBOX_*` keys exist only in
> THIS PR's `config_parse.py` schema. The currently-deployed parser rejects unknown keys
> fail-closed (exit 2), and `drift-canary.sh` runs `substrate_load_config` every 15 min on the
> factory — so adding these keys before the merge lands makes the canary hard-ALARM every tick,
> and any unrelated merge in that window wedges the reconcile converge until this PR lands or the
> edit is reverted. Do the merge in step 7.1, confirm the new parser is live, and only then edit
> `config.env`. (This is why step 7's arm is a plain merge — the config edit rides *after* it.)

Add the `INBOX_*` keys (parsed, never sourced; strict `KEY=value`). Injected into the tick via
the plist env:

```
INBOX_ACCOUNTS=you@yourdomain.com,ops@yourdomain.com,you@gmail.com
# 1Password vault name (default 'Automation')
INBOX_OP_VAULT=Automation
# first-run bounded backfill window (default 90)
INBOX_BACKFILL_DAYS=90
# whose Drive receives the brief mirror; MUST be one of INBOX_ACCOUNTS; empty = Drive mirror off
INBOX_DRIVE_ACCOUNT=you@yourdomain.com
# the database shared with the integration (step 6); empty = Notion mirror off
INBOX_NOTION_DB=<notion-database-id>
# empty = ping off
INBOX_IMESSAGE_TO=+1XXXXXXXXXX
```

The parser is strict `KEY=value` — **inline `#` comments are NOT supported** (a trailing comment
fails validation, and config_parse exit 2 is a hard reconcile ALARM). Comments go on their own
lines, as above.

- `INBOX_ACCOUNTS` empty/absent = component off (tick exits 0). That's the kill switch.
- Dry-run any time: `MYNDAIX_INBOX_DRY_RUN=1` — no labels, no drafts, no deliveries, no cursor
  writes; brief prints to stdout.

## 6. Notion (only if the mirror is on)

notion.so → My integrations → create an **internal** integration → token →
`op://Automation/notion-inbox-assistant/token`. Then share the target database with the
integration (database → ••• → Connections → add it — an unshared database is invisible to the
token). `INBOX_NOTION_DB` = the database id from the database URL.

## 7. Arm

1. **Merge the PR to `main` FIRST** (before the step-5 config edit — see the step-5 warning). The
   Mini's reconcile poll converges within 15 min (no SSH deploy step), installing the new parser
   that understands the `INBOX_*` keys.
2. **Now do step 5**: add the `INBOX_*` keys to `~/.myndaix/config.env`. With the new parser live,
   the keys validate and the drift-canary stays quiet.
3. Verify the job landed:
   ```
   launchctl print gui/$(id -u)/ai.myndaix.inbox-assistant
   ```
4. After the first 06:30 run, read `{MYNDAIX_HOME}/orchestrator/inbox-assistant.out` and confirm
   the brief in `~/.myndaix/bridge/inbox/jefe/`. **The first run does a bounded backfill**
   (`INBOX_BACKFILL_DAYS`, default 90) — expect it to be slow and the first brief to be big;
   subsequent runs are incremental off the historyId cursor.

## 8. Failure modes cheat-sheet

| Symptom | Meaning | Action |
|---|---|---|
| `invalid_grant` on one account | That account's refresh token died — usually a password change; Google revokes Gmail-scope tokens on password change, this is EXPECTED behavior, not a bug (verified 2026-07, Google OAuth docs) | Re-mint that one token (step 4), update its vault item. Other accounts keep working (per-account fail-closed). |
| `historyId` 404 | Google expired the cursor | Nothing — the tick auto-resyncs via bounded backfill and re-seeds the cursor. |
| `op: not signed in` / `op` nonzero | Keychain locked (post-reboot, no login) or SA token problem | Log in on the Mini; verify `security find-generic-password -s op.inbox-assistant.token` exists; worst case re-issue the SA token (step 3.4). |
| Brief missing | Anything upstream | Read `{MYNDAIX_HOME}/orchestrator/inbox-assistant.out` — every failure logs there. |
