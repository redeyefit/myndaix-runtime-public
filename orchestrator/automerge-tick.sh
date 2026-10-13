#!/usr/bin/env bash
# automerge-tick.sh — launchd entrypoint for the docs-only PR auto-merge gate (rung 4).
# Runs ONE bounded tick, then exits (NOT a daemon). OFF unless $ORCH/AUTOMERGE_ENABLED exists.
#
# Safe first run (decide + log, merge NOTHING):
#   MYNDAIX_AUTOMERGE_DRY_RUN=1 orchestrator/automerge-tick.sh
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# liveness-fire: one unconditional stdout line per fire — liveness-canary reads this job's
# .out mtime as execution evidence (the python tick can exit without printing).
printf '[%s] [automerge-tick] tick fire\n' "$(date '+%Y-%m-%d %H:%M:%S')"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# DSN default pinned to 127.0.0.1 (NOT `localhost`): on the Mini the IPv6 loopback [::1] wedges
# intermittently, so asyncpg's `localhost` resolution flips to ::1 and hangs the full connect
# timeout → the tick exits 1 → the gate dies. 127.0.0.1 forces IPv4 loopback (harmless on the
# MacBook, where localhost already works). Matches the serve + mxr pin (session 2026-07-12). The
# proper machine-specific config.env home for this lands in the two-machine substrate (PR-2).

# GH_TOKEN: a fine-grained PAT for the launchd context — `gh`'s keyring token is unreadable
# by a launchd agent. Provision a chmod-600 secret with exactly: Contents r/w + Pull requests
# r/w + Checks r + Metadata r on the target repo. (Deploy prereq; see docs/automerge-design.md §0.)
TOKEN_FILE="${MYNDAIX_AUTOMERGE_TOKEN_FILE:-$HOME/.myndaix/.automerge-token}"
# NOT a `&&`-chain: in `A && B && C`, set -e exempts the non-final B, so a `tr` failure would
# silently skip `export` (short-circuit) and leave any ambient GH_TOKEN in force. The if-block
# makes a token-load failure abort under set -e rather than run with a broken/ambient token.
if [[ -r "$TOKEN_FILE" ]]; then
  GH_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
  export GH_TOKEN
fi

export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://127.0.0.1/runtime}"
export PYTHONPATH="src"
exec "$REPO/.venv/bin/python" -m runtime.automerge tick
