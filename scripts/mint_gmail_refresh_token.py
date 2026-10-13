"""mint_gmail_refresh_token — one-time interactive OAuth mint for an Inbox Assistant account.

Run on the MacBook (needs a browser); the Mini consumes the token headless via 1Password.
Builds the client config in-memory and drives google_auth_oauthlib's local-server flow —
NOTHING is written to disk: no client_secret.json, no token cache. With --op-vault the minted
token is stored STRAIGHT into the vault (`op item edit`/`create` on gmail-rt-<email>) and only
the secret reference + account are printed — the raw token never hits stdout. Printing the raw
token (vault-less runs) requires an explicit --print and warns on stderr.

SECRET-HANDLING NOTE: on the vault path the refresh token transits the local `op` process's
argv briefly (operator's own machine, a subprocess arg list — no shell involved). That beats
the alternative of printing it for a copy/paste through the clipboard and terminal scrollback.

Scopes are FROZEN at mint (gmail.modify + gmail.compose + drive.file — must stay identical to
gmail_client._SCOPES). Changing scopes later means re-minting. gmail.modify permits send-free
mutation (labels, but also trash/archive/mark-read) and gmail.compose permits send — the
runtime is drafts-only and labels-only by code contract; no send/trash/archive method is ever
invoked (source-scan-tested).

Usage:
    # store directly to the vault (needs `op` signed in) — prints only the secret reference
    python3 scripts/mint_gmail_refresh_token.py --client-id X --op-vault Automation \
        --account someone@x.com --check
    # vault-less: secret piped/pasted on stdin (never argv), token printed ONLY with --print
    op read 'op://Automation/gmail-oauth-client/client_secret' | \
        python3 scripts/mint_gmail_refresh_token.py --client-id X --client-secret-stdin \
        --account someone@x.com --print
"""
from __future__ import annotations

import argparse
import subprocess
import sys

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.file",
]


def _read_client_secret(args: argparse.Namespace) -> str:
    """Secret from `op read` (if --op-vault) or stdin. Never argv, never echoed, never logged."""
    if args.op_vault:
        ref = f"op://{args.op_vault}/gmail-oauth-client/client_secret"
        proc = subprocess.run(["op", "read", ref], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"FAIL: `op read {ref}` exited {proc.returncode} — is `op` signed in? "
                  f"{proc.stderr.strip()}", file=sys.stderr)
            sys.exit(2)
        secret = proc.stdout.strip()
    else:
        if sys.stdin.isatty():
            print("Paste the OAuth client secret, then Ctrl-D:", file=sys.stderr)
        secret = sys.stdin.read().strip()
    if not secret:
        print("FAIL: empty client secret", file=sys.stderr)
        sys.exit(2)
    return secret


def _store_in_vault(vault: str, account: str, token: str) -> bool:
    """UPSERT the token into op://<vault>/gmail-rt-<account>/refresh_token. Probe with
    `op item get` first: edit if the item exists, create otherwise. The token rides the op
    subprocess argv (see the header note) but is never printed or shell-interpolated."""
    item = f"gmail-rt-{account}"
    probe = subprocess.run(["op", "item", "get", item, "--vault", vault],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        cmd = ["op", "item", "edit", item, f"refresh_token={token}", "--vault", vault]
    else:
        cmd = ["op", "item", "create", "--category=password", f"--title={item}",
               f"--vault={vault}", f"refresh_token={token}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # stderr from op is safe (it never echoes field values) — the token is NOT in it.
        print(f"FAIL: `op item {cmd[2]}` exited {proc.returncode} for {item!r} in vault "
              f"{vault!r}: {proc.stderr.strip()}", file=sys.stderr)
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mint a Gmail refresh token for one Inbox Assistant account "
                    "(interactive; run where a browser lives).")
    ap.add_argument("--client-id", required=True,
                    help="OAuth client id (not secret; argv is fine for this one)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--client-secret-stdin", action="store_true",
                     help="read the client secret from stdin (pipe, or paste + Ctrl-D)")
    src.add_argument("--op-vault", metavar="VAULT",
                     help="read the client secret from op://VAULT/gmail-oauth-client/"
                          "client_secret AND store the minted token back into "
                          "op://VAULT/gmail-rt-<account>/refresh_token")
    ap.add_argument("--account", required=True,
                    help="the Gmail address this token is minted for (authorize as THIS account)")
    ap.add_argument("--check", action="store_true",
                    help="after minting, verify the token can list labels before storing it")
    ap.add_argument("--print", dest="print_token", action="store_true",
                    help="print the RAW refresh token to stdout (vault-less runs only need "
                         "this; without --op-vault it is required or the token is lost)")
    args = ap.parse_args()

    if "@" not in args.account:
        ap.error(f"--account {args.account!r} does not look like an email address")
    if not args.op_vault and not args.print_token:
        ap.error("without --op-vault the minted token has nowhere to go — pass --print "
                 "to output it (or use --op-vault to store it directly)")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("FAIL: google-auth-oauthlib not installed — "
              "pip install google-auth-oauthlib google-api-python-client", file=sys.stderr)
        return 2

    client_secret = _read_client_secret(args)

    # In-memory client config — NEVER a client_secret.json on disk.
    client_config = {
        "installed": {
            "client_id": args.client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    print(f"Opening browser — authorize as {args.account} (watch the account picker: the wrong "
          f"account mints a token for the wrong inbox).", file=sys.stderr)
    # port=0 grabs a free ephemeral port. access_type=offline + prompt=consent forces Google
    # to issue a refresh token even if this client was consented before (else it comes back None).
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("FAIL: Google returned no refresh token (re-run; prompt=consent should force one)",
              file=sys.stderr)
        return 1

    if args.check:
        try:
            from googleapiclient.discovery import build
        except ImportError:
            print("FAIL: --check needs google-api-python-client — "
                  "pip install google-api-python-client", file=sys.stderr)
            return 2
        try:
            gmail = build("gmail", "v1", credentials=creds)
            # IDENTITY FIRST (KilaBz 2026-07-16): labels.list proves the token WORKS, not
            # that it belongs to --account. The browser account picker is exactly where the
            # wrong mailbox slips in — verify getProfile's address before storing anything.
            profile = gmail.users().getProfile(userId="me").execute()
            minted_for = str(profile.get("emailAddress") or "").strip().lower()
            if minted_for != args.account.strip().lower():
                print(f"CHECK FAIL: token was minted for {minted_for!r}, not "
                      f"--account {args.account!r} — wrong account in the browser picker; "
                      "NOT stored, re-run and pick the right account", file=sys.stderr)
                return 1
            labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
        except Exception as exc:  # HttpError et al — message is safe, credentials are not in it
            print(f"CHECK FAIL: profile/labels — {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"CHECK OK: token is for {args.account} and lists {len(labels)} labels",
              file=sys.stderr)

    if args.op_vault:
        if not _store_in_vault(args.op_vault, args.account, creds.refresh_token):
            print("Token minted but NOT stored — nothing was printed. Fix the op error and "
                  "re-run (re-minting is harmless), or use --client-secret-stdin --print.",
                  file=sys.stderr)
            return 1
        # NOTHING secret on stdout — only the reference the runtime reads and the account.
        print(f"account: {args.account}")
        print(f"secret_ref: op://{args.op_vault}/gmail-rt-{args.account}/refresh_token")
    if args.print_token:
        print("WARNING: printing the RAW refresh token to stdout — it will sit in terminal "
              "scrollback/pipes; store it and clear your scrollback.", file=sys.stderr)
        print(f"account: {args.account}")
        print(f"refresh_token: {creds.refresh_token}")
    print("REMINDER: scopes are frozen at mint (re-mint to change them).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
