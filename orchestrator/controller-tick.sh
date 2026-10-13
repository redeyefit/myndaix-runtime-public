#!/usr/bin/env bash
# controller-tick.sh — launchd entrypoint for the controller-loop ("the brain").
# Runs ONE bounded tick of the proactive review scheduler, then exits (NOT a daemon).
# launchd fires it on a timer (orchestrator/ai.myndaix.controller.plist.example).
#
# Safe first run (decide + log, write/dispatch NOTHING):
#   MYNDAIX_CONTROLLER_DRY_RUN=1 orchestrator/controller-tick.sh
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# liveness-fire: one unconditional stdout line per fire — liveness-canary reads this job's
# .out mtime as execution evidence, and the python tick has silent early-return paths
# (no repos, lock held) that would otherwise leave the mtime frozen (false "stale" alert).
printf '[%s] [controller-tick] tick fire\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# repo root = this script's parent dir's parent (orchestrator/..), so the wrapper is
# portable across machines without a hardcoded path.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

export MYNDAIX_DSN="${MYNDAIX_DSN:-postgresql://localhost/runtime}"
export PYTHONPATH="src"
exec "$REPO/.venv/bin/python" -m runtime.controller tick
