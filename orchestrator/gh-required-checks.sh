#!/usr/bin/env bash
# gh-required-checks.sh — the ONE way to change a repo's required status checks on `main`
# (docs/review-on-github-plan.md [not in the public snapshot] P4; the working repo's OPERATING.md "PR gate" runbook [not in the public snapshot]).
#
#   gh-required-checks.sh <owner/repo> show
#   gh-required-checks.sh <owner/repo> add <context> [<app_id>]    default app 15368 = GitHub Actions
#   gh-required-checks.sh <owner/repo> remove <context>
#
# WHY a script, not a hand PATCH: the REST PATCH replaces the WHOLE `checks` list, so a PATCH naming
# only the check you meant to add silently drops the others (e.g. `security-test`), and the
# add-context endpoint cannot carry an app_id (the check would be pinned to whatever app posted it
# last). This script reads the current list, changes exactly one entry, PATCHes the full list with
# every existing pin preserved, then re-reads and FAILS unless the result is exactly what it meant.
# `strict` is carried over unchanged. Branch protection itself (enforce_admins, PR-required) is not
# touched — only the required_status_checks sub-resource.
#
# Needs `gh` authenticated as a repo admin. Nothing is changed by `show`.
#
# OPERATOR TOOL — one human at a time; nothing automated calls it, and nothing should. The API has
# no conditional write for this endpoint, so a read-modify-write can race another writer. The
# script re-reads the list immediately before its PATCH and aborts if anything changed since its
# first read, which leaves only a sub-second window between two overlapping manual runs (P4
# review r1). Wiring this into automation would need a real single-writer, not this re-read.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

GH_ACTIONS_APP_ID=15368
# GH_BIN: tests point this at a stub. PATH-order stubbing is NOT safe here — the PATH export above
# puts the real gh first, so a PATH stub would silently hit the live API.
GH="${GH_BIN:-gh}"
usage() { echo "usage: $0 <owner/repo> show | add <context> [<app_id>] | remove <context>" >&2; exit 2; }

repo="${1:-}"; cmd="${2:-}"; context="${3:-}"; app_id="${4:-$GH_ACTIONS_APP_ID}"
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
case "$cmd" in
  show) ;;
  add|remove) [[ "$context" =~ ^[A-Za-z0-9_./:@\ -]{1,100}$ ]] || usage ;;
  *) usage ;;
esac
[[ "$app_id" =~ ^-?[0-9]+$ ]] || usage

endpoint="repos/$repo/branches/main/protection/required_status_checks"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

"$GH" api "$endpoint" > "$tmp/before.json"

if [ "$cmd" = show ]; then
  python3 - "$tmp/before.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"strict: {d.get('strict')}")
for c in d.get("checks", []):
    print(f"  {c['context']}  (app {c.get('app_id')})")
PYEOF
  exit 0
fi

# Build the full new list (python via argv, never interpolated): existing entries kept verbatim,
# exactly one added or removed. Refuse states this script can't reason about.
python3 - "$tmp/before.json" "$cmd" "$context" "$app_id" "$tmp/payload.json" "$tmp/want.json" <<'PYEOF'
import json, sys
before_path, cmd, context, app_id, payload_path, want_path = sys.argv[1:]
d = json.load(open(before_path))
checks = d.get("checks")
if not isinstance(checks, list):
    sys.exit("refusing: the API returned no `checks` list")
if set(d.get("contexts", [])) != {c["context"] for c in checks}:
    sys.exit("refusing: `contexts` and `checks` disagree — fix by hand, then rerun")
names = [c["context"] for c in checks]
if cmd == "add":
    if context in names:
        sys.exit(f"refusing: {context!r} is already required (remove it first to re-pin)")
    checks = checks + [{"context": context, "app_id": int(app_id)}]
else:
    if context not in names:
        sys.exit(f"refusing: {context!r} is not a required check")
    checks = [c for c in checks if c["context"] != context]
    if not checks:
        sys.exit("refusing: this would leave NO required checks")
json.dump({"strict": bool(d.get("strict")), "checks": checks}, open(payload_path, "w"))
json.dump(sorted([c["context"], c.get("app_id")] for c in checks), open(want_path, "w"))
PYEOF

# re-read right before writing: if another writer changed the list since our first read, our full-
# list PATCH would silently undo their change — abort instead
"$GH" api "$endpoint" > "$tmp/recheck.json"
python3 - "$tmp/before.json" "$tmp/recheck.json" <<'PYEOF'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
key = lambda d: (bool(d.get("strict")), sorted([c["context"], c.get("app_id")] for c in d.get("checks", [])))
if key(a) != key(b):
    sys.exit("ABORT: required checks changed since this run read them — nothing written; rerun")
PYEOF
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [gh-required-checks] $repo: $cmd $context — PATCH $(cat "$tmp/payload.json")"
"$GH" api -X PATCH "$endpoint" --input "$tmp/payload.json" > /dev/null
"$GH" api "$endpoint" > "$tmp/after.json"

python3 - "$tmp/after.json" "$tmp/want.json" <<'PYEOF'
import json, sys
after = json.load(open(sys.argv[1]))
want = json.load(open(sys.argv[2]))
got = sorted([c["context"], c.get("app_id")] for c in after.get("checks", []))
if got != want:
    sys.exit(f"MISMATCH after PATCH — required checks are now {got}, expected {want}. Fix by hand NOW.")
print("verified: required checks =", ", ".join(f"{c} (app {a})" for c, a in got))
PYEOF
