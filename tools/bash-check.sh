#!/bin/bash
# bash-check.sh — ONE command for MyndAIX bash quality across every tracked shell script:
#   1. shellcheck        (generic bugs — quoting, unset vars, subshells)
#   2. semgrep bug-rules (OUR recurring bugs — tools/bash-rules.semgrep.yml)
#   3. pipefail presence (safety header)
# Exits nonzero if shellcheck or a semgrep ERROR fires (WARNING/INFO are surfaced, not gating).
# Skips a checker that isn't installed (so it degrades, never hard-fails on a missing dep).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
rc=0

# tracked shell scripts (git-tracked; skip the venv). bash-3.2-safe array build (no mapfile).
scripts=()
while IFS= read -r f; do scripts+=("$f"); done < <(git ls-files '*.sh' '*.bash' 2>/dev/null | grep -v '^\.venv/' || true)
if [ "${#scripts[@]}" -eq 0 ]; then echo "no tracked shell scripts"; exit 0; fi
echo "== bash-check: ${#scripts[@]} script(s) =="

echo "-- shellcheck (warnings surfaced; ERROR-level gates) --"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck --severity=warning "${scripts[@]}" || true       # show warning+ (the 291 style-notes are muted)
  if shellcheck --severity=error "${scripts[@]}" >/dev/null 2>&1; then
    echo "  no shellcheck ERRORS"
  else echo "  shellcheck ERROR present"; rc=1; fi
else echo "  (shellcheck not installed — skipped)"; fi

echo "-- semgrep (our bug-rules; ERROR gates, WARNING/INFO surfaced) --"
if command -v semgrep >/dev/null 2>&1; then
  # full report (all severities) for visibility...
  semgrep --quiet --config tools/bash-rules.semgrep.yml "${scripts[@]}" || true
  # ...but only ERROR-level findings gate the exit code.
  if ! semgrep --quiet --error --severity ERROR --config tools/bash-rules.semgrep.yml "${scripts[@]}" >/dev/null 2>&1; then
    echo "  ERROR-level bug-rule fired (see report above)"; rc=1
  else echo "  no ERROR-level findings"; fi
else echo "  (semgrep not installed — skipped)"; fi

echo "-- 'set -...pipefail' present (whole file; headers can push it past the top) --"
miss=0
for s in "${scripts[@]}"; do
  grep -qE '^[[:space:]]*set +-.*pipefail' "$s" || { echo "  MISSING pipefail: $s"; rc=1; miss=1; }
done
[ "$miss" -eq 0 ] && echo "  ok"

echo ""
[ "$rc" -eq 0 ] && echo "bash-check: PASS" || echo "bash-check: issues found (rc=$rc)"
exit "$rc"
