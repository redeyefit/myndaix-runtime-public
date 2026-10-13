#!/usr/bin/env bash
# automerge-preflight.sh — read-only deploy smoke check for the auto-merge gate.
# Verifies the §0 prereqs are satisfied BEFORE arming (AUTOMERGE_ENABLED). Merges nothing.
# Run: orchestrator/automerge-preflight.sh
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCH="$HOME/.myndaix/orchestrator"
ok=0; bad=0
chk(){ if eval "$2" >/dev/null 2>&1; then echo "  ok: $1"; ok=$((ok+1)); else echo "  XX: $1"; bad=$((bad+1)); fi; }

TOKEN_FILE="${MYNDAIX_AUTOMERGE_TOKEN_FILE:-$HOME/.myndaix/.automerge-token}"
# NOT a `&&`-chain: short-circuiting on a `tr` failure would skip `export` and leave any ambient
# GH_TOKEN in force. The if-block always runs `export` when the file is readable so the token
# actually loaded is the one the chk's below verify (no silent fall-through to an inherited token).
if [[ -r "$TOKEN_FILE" ]]; then
  GH_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
  export GH_TOKEN
fi
NWO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"

echo "auto-merge preflight ($REPO -> ${NWO:-?})"
chk "GH_TOKEN present (launchd-readable secret)"      '[[ -n "${GH_TOKEN:-}" ]]'
chk "gh authenticates (api user)"                     'gh api user -q .login'
chk "gh rate limit healthy (>100 core remaining)"     '[[ "$(gh api rate_limit -q .resources.core.remaining 2>/dev/null)" -gt 100 ]]'
chk "can list open PRs against main"                  'gh pr list --base main --state open --json number'
# Branch protection requires Administration:read to QUERY — the gate's scoped PAT deliberately
# lacks it, and the gate never reads protection (GitHub enforces it server-side at merge). So this
# is INFORMATIONAL, not a pass/fail: if the token can read it, confirm `test` is required; if not,
# note it and move on (a 403 here means least-privilege, not a misconfiguration).
if gh api "repos/$NWO/branches/main/protection/required_status_checks" -q '.contexts[]' 2>/dev/null | grep -qx test; then
  echo "  ok: branch protection requires the 'test' check"; ok=$((ok+1))
else
  echo "  --: branch protection not readable with this token (needs Administration:read) — enforced server-side, not gate-required"
fi
chk "main has NO merge queue"                          '[[ "$(gh api "repos/$NWO/rules/branches/main" -q "any(.[]; .type==\"merge_queue\")" 2>/dev/null)" != "true" ]]'
chk "trusted play-review.sh installed + executable"   '[[ -f "$ORCH/play-review.sh" && ! -L "$ORCH/play-review.sh" && -x "$ORCH/play-review.sh" ]]'
chk "play-review.sh has the --gate (PLAY_GATE) mode"  'grep -q PLAY_GATE "$ORCH/play-review.sh"'
echo "AUTOMERGE_ENABLED: $([[ -e "$ORCH/AUTOMERGE_ENABLED" ]] && echo ARMED || echo off)"
echo "=== $ok ok, $bad missing — arm only when all ok + a clean DRY-RUN ==="
[[ "$bad" -eq 0 ]]
