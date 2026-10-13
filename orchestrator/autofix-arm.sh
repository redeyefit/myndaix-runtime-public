#!/usr/bin/env bash
# autofix-arm.sh — arm / disarm the autonomous-fix flip (PLAY_AUTOFIX) durably and safely.
#
# ONE command to make it live + on. "Set once" enable: after the pre-arm gates pass, it (1) deploys
# the trusted worker + fixer copies into $ORCH (the live worker re-execs $ORCH/play-review.sh, and the
# auto path execs ONLY $ORCH/play-fix.sh), and (2) drops a flag file ($ORCH/AUTOFIX_ENABLED) that the
# worker reads — so arming SURVIVES shell restarts (unlike the per-push PLAY_AUTOFIX env var). Disarm
# any time. Re-run `arm` after pulling new code OR after a codex CLI update (it re-probes the .git
# seatbelt). The flip NEVER auto-applies/merges — it only pre-drafts a candidate fix diff into the
# human inbox. Design: docs/phase2-autonomous-fix-flip-design.md (Pre-ship gates).
#
#   autofix-arm.sh [status|arm|disarm]      (default: status)
#
# NOTE: `arm` deploys the WORKING-TREE copies of play-review.sh + play-fix.sh. Run it from a clean,
# up-to-date checkout (e.g. main after merge), not a stale/dirty branch.
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

ORCH="$HOME/.myndaix/orchestrator"            # MUST match play-review.sh:23 EXACTLY — the worker reads the
                                              # flag + re-execs from here; no MYNDAIX_ORCH override (codex MAJOR)
REPOS_JSON="${MYNDAIX_REPOS_JSON:-$ORCH/repos.json}"
INBOX="${MYNDAIX_FIX_INBOX:-$HOME/.myndaix/bridge/inbox/jefe}"
FLAG="$ORCH/AUTOFIX_ENABLED"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PROBE="$SELF_DIR/probe-git-write-vector.sh"
INSTALLS=(play-review.sh play-fix.sh)         # trusted copies the live worker/fixer run from
cmd="${1:-status}"

say(){ printf '%s\n' "$*"; }
ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
no(){ printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

verify_one(){   # verify $ORCH/$1 is a real regular file (NOT a symlink), executable, matches the working tree
  local f="$1" a b
  [[ -f "$ORCH/$f" && ! -L "$ORCH/$f" ]] || { no "$f trusted install MISSING or a SYMLINK ($ORCH/$f)"; return 1; }
  [[ -x "$ORCH/$f" ]] || { no "$f trusted install not executable"; return 1; }
  a="$(shasum -a 256 "$SELF_DIR/$f" | awk '{print $1}')"
  b="$(shasum -a 256 "$ORCH/$f"     | awk '{print $1}')"
  if [[ "$a" == "$b" ]]; then ok "$f trusted install matches the working tree"; return 0
  else no "$f trusted install STALE vs working tree (re-run: $0 arm)"; return 1; fi
}
verify_all(){ local f rc=0; for f in "${INSTALLS[@]}"; do verify_one "$f" || rc=1; done; return "$rc"; }

show_repos(){
  command -v jq >/dev/null 2>&1 || { warn "jq missing — cannot read repos.json"; return 0; }
  [[ -f "$REPOS_JSON" ]] || { warn "no repos.json at $REPOS_JSON (auto-fire fails closed for every repo)"; return 0; }
  say "  repos eligible for auto-fire (need fail_to_pass:null):"
  jq -r 'to_entries[] | select((.key|startswith("_"))|not)
         | "    - \(.key): fail_to_pass=\(.value.fail_to_pass|tostring)" + (if .value.fail_to_pass==null then "   (eligible)" else "   (EXCLUDED — not null)" end)' \
     "$REPOS_JSON" 2>/dev/null || warn "could not parse repos.json"
}

watcher_check(){   # best-effort loop-immunity check — warn, never block
  if pgrep -fl "$INBOX" >/dev/null 2>&1; then
    warn "a process references $INBOX — ensure NO agent auto-processes the inbox (loop-immunity)"
  else ok "no obvious watcher on $INBOX (loop-immunity holds)"; fi
}

case "$cmd" in
  status)
    say "autofix-arm — autonomous-fix flip status"
    if [[ -f "$FLAG" ]]; then ok "ARMED (durable flag: $FLAG)"; else say "  ◻ DISARMED (no flag file)"; fi
    [[ "${PLAY_AUTOFIX:-0}" == "1" ]] && warn "PLAY_AUTOFIX=1 set in THIS shell too (per-push override)"
    verify_all || true
    watcher_check || true
    show_repos
    say ""
    say "  enable:  $0 arm        disable:  $0 disarm"
    ;;
  arm)
    say "arming the autonomous-fix flip — running pre-arm gates…"
    rm -f "$FLAG"     # FAIL-CLOSED: disarm FIRST so any gate/deploy failure below leaves it OFF, never partially armed (codex BLOCKER)
    say "  [1/3] codex .git-write-vector probe (runs codex; ~30s)…"
    if bash "$PROBE" >/tmp/autofix-arm-probe.log 2>&1; then ok "probe PASS — seatbelt denies shared-.git writes"
    else no "probe FAILED — see /tmp/autofix-arm-probe.log. NOT arming."; exit 1; fi
    say "  [2/3] deploying trusted worker + fixer into $ORCH (atomic temp+rename)…"
    mkdir -p "$ORCH"
    for f in "${INSTALLS[@]}"; do
      cp "$SELF_DIR/$f" "$ORCH/.$f.tmp.$$"; chmod 0755 "$ORCH/.$f.tmp.$$"
      mv -f "$ORCH/.$f.tmp.$$" "$ORCH/$f"      # atomic replace — clobbers any preexisting symlink (codex MAJOR)
    done
    verify_all || { no "install verification failed — NOT arming."; exit 1; }
    say "  [3/3] invariants…"
    watcher_check || true
    show_repos
    printf 'armed %s by autofix-arm.sh\nNEVER auto-applies — human-apply only. disarm: orchestrator/autofix-arm.sh disarm\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" > "$FLAG"
    say ""
    ok "ARMED. NEEDS-FIX verdicts on fail_to_pass:null repos now auto-draft a fix into the inbox (≤ PLAY_FIX_DAILY_CAP/day). It NEVER auto-applies; you apply diffs by hand."
    ;;
  disarm)
    rm -f "$FLAG"
    ok "DISARMED (removed $FLAG). Auto-fire off; manual play-fix.sh + the inbox hint still work."
    ;;
  *) say "usage: $0 [status|arm|disarm]"; exit 2 ;;
esac
