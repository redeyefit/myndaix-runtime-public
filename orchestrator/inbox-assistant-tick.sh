#!/usr/bin/env bash
# inbox-assistant-tick.sh — launchd entrypoint for the Inbox Assistant morning brief.
# Runs ONE bounded tick (pull -> classify -> label/draft -> deliver), then exits (NOT a daemon).
# OFF unless INBOX_ACCOUNTS is set in the environment — the wrapper exits 0 BEFORE the
# secrets/keychain gates (and the module double-checks, exiting 0 on an empty list too).
#
# Safe first run (classify + print the brief to stdout, touch NOTHING — no labels, no drafts,
# no deliveries, no cursor writes):
#   MYNDAIX_INBOX_DRY_RUN=1 orchestrator/inbox-assistant-tick.sh
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# liveness-fire: one unconditional stdout line per fire — liveness-canary reads this job's
# .out mtime as execution evidence (several early-exit paths below print nothing otherwise).
printf '[%s] [inbox-assistant-tick] tick fire\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# repo root = this script's parent dir's parent (orchestrator/..), so the wrapper is
# portable across machines without a hardcoded path.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Component-off short-circuit BEFORE the .secrets sourcing and the Keychain gate: on a
# factory between merge and runbook execution INBOX_ACCOUNTS is unset and the keychain
# item doesn't exist — that must be ONE quiet line and exit 0, not a daily FATAL.
if [[ -z "${INBOX_ACCOUNTS:-}" ]]; then
  echo "[inbox-assistant] not configured (INBOX_ACCOUNTS empty) — exiting"
  exit 0
fi

# SINGLE-FLIGHT LOCK (KilaBz 2026-07-16): launchd is single-instance per label, but a manual
# run overlapping the scheduled tick would pull the same slice, race the cursor CAS, and
# double-draft. Atomic mkdir (the house pattern); a crashed run's stale lock clears after
# 3600s — that must exceed the worst LEGITIMATE tick (3 classify chunks + 5 draft composes
# at 300s each = 2400s + Gmail/backoff overhead; Oracle round-2: a 900s clear would steal a
# live long tick's lock and resurrect the exact races this lock exists to stop). Held for
# the WHOLE tick — released by the EXIT trap (which is why the python call below must NOT
# be `exec`: exec replaces the shell and the trap never fires).
LOCK="$HOME/.myndaix/inbox-assistant.tick.lock"
mkdir -p "$HOME/.myndaix"
if ! mkdir "$LOCK" 2>/dev/null; then
  lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
  if [[ "$lock_age" -lt 3600 ]]; then
    echo "[inbox-assistant] another tick holds the lock (age ${lock_age}s) — exiting"
    exit 0
  fi
  echo "[inbox-assistant] clearing stale lock (age ${lock_age}s)"
  rm -f "$LOCK/pid" 2>/dev/null || true
  rmdir "$LOCK" 2>/dev/null || true
  if ! mkdir "$LOCK" 2>/dev/null; then
    echo "FATAL: could not acquire tick lock after stale clear — aborting" >&2
    exit 1
  fi
fi
echo "$$" > "$LOCK/pid"
trap 'rm -f "$LOCK/pid" 2>/dev/null; rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

# ~/.myndaix/.secrets provides CLAUDE_CODE_OAUTH_TOKEN for the classify subprocess
# (`claude -p`). The set -a bracket exports everything the file defines; close it so
# nothing sourced later leaks into auto-export. Values are secrets — never echoed,
# and xtrace stays off in this script.
if [[ -r "$HOME/.myndaix/.secrets" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$HOME/.myndaix/.secrets"
  set +a
fi

# OP_SERVICE_ACCOUNT_TOKEN gates every 1Password read (OAuth client, per-account refresh
# tokens, Notion token). It lives in the macOS login Keychain — never on disk. Fail CLOSED:
# without it every account fails auth anyway, so one clear line in the launchd log beats N
# confusing per-account failures downstream. NOT a `&&`-chain (see automerge-tick.sh: set -e
# exempts non-final links, so a failed read could silently export nothing).
if ! OP_SERVICE_ACCOUNT_TOKEN="$(security find-generic-password -s 'op.inbox-assistant.token' -w)" \
    || [[ -z "$OP_SERVICE_ACCOUNT_TOKEN" ]]; then
  echo "FATAL: op.inbox-assistant.token not readable from the login Keychain (security find-generic-password failed or empty) — inbox tick aborted" >&2
  exit 1
fi
export OP_SERVICE_ACCOUNT_TOKEN

# DSN default pinned to 127.0.0.1 (NOT `localhost`): on the Mini the IPv6 loopback [::1]
# wedges intermittently, so asyncpg's `localhost` resolution flips to ::1 and hangs the
# full connect timeout → the tick exits 1. 127.0.0.1 forces IPv4 loopback (harmless on the
# MacBook). Matches the automerge + serve + mxr pin (session 2026-07-12).
export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://127.0.0.1/runtime}"
export PYTHONPATH="src"
# NOT exec — the EXIT trap must fire to release the single-flight lock. Under set -e a
# nonzero python rc still exits the script with that rc (trap runs first).
"$REPO/.venv/bin/python" -m runtime.inbox_assistant tick
