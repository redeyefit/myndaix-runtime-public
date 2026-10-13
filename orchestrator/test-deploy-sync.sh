#!/usr/bin/env bash
# test-deploy-sync.sh — smoke test for deploy-sync.sh. Uses a scratch DEST (never touches the real
# ~/.myndaix) and deploys from HEAD (the committed copies of the two surface files).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC="$DIR/deploy-sync.sh"
REF="HEAD"                                            # committed content, branch-independent
SCRATCH="$(mktemp -d)"
export DEPLOY_SYNC_DEST="$SCRATCH"
# the worktree case registers a scratch worktree in this repo's git dir — deregister it on exit
trap 'git -C "$DIR/.." worktree remove --force "$SCRATCH/wt" >/dev/null 2>&1; rm -rf "$SCRATCH"' EXIT

PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad(){ FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

echo "== --apply into an empty dest =="
"$SYNC" --apply "$REF" >/dev/null 2>&1 && ok "apply exits 0" || bad "apply should succeed"
[[ -f "$SCRATCH/play-fix.sh" && -f "$SCRATCH/play-review.sh" ]] && ok "both files deployed" || bad "files missing"
[[ -x "$SCRATCH/play-fix.sh" ]] && ok "deployed file is executable" || bad "not executable"
[[ ! -L "$SCRATCH/play-fix.sh" ]] && ok "play-fix.sh is a REGULAR file (not symlink)" || bad "symlink!"
[[ -s "$SCRATCH/.deployed-sha" ]] && ok "stamp .deployed-sha written" || bad "no stamp"

echo "== deployed content == committed blob =="
want="$(git -C "$DIR/.." rev-parse "$REF:orchestrator/play-fix.sh")"
have="$(git -C "$DIR/.." hash-object "$SCRATCH/play-fix.sh")"
[[ "$want" == "$have" ]] && ok "play-fix.sh hash matches HEAD blob" || bad "hash mismatch ($have != $want)"

echo "== --check on a clean deploy =="
"$SYNC" --check "$REF" >/dev/null 2>&1 && ok "check exits 0 when synced" || bad "check should pass"

echo "== --check DETECTS drift (exit 1) =="
printf '\n# tampered\n' >> "$SCRATCH/play-fix.sh"
if "$SYNC" --check "$REF" >/dev/null 2>&1; then bad "check must fail on drift"; else ok "check exits non-zero on drift"; fi

echo "== --check DETECTS play-review drift too =="
"$SYNC" --apply "$REF" >/dev/null 2>&1
printf '\n# tampered\n' >> "$SCRATCH/play-review.sh"
if "$SYNC" --check "$REF" >/dev/null 2>&1; then bad "check must flag a drifted play-review.sh"; else ok "drifted play-review.sh -> check exits non-zero"; fi

echo "== --apply HEALS drift + backs up =="
"$SYNC" --apply "$REF" >/dev/null 2>&1 && ok "re-apply exits 0" || bad "re-apply failed"
"$SYNC" --check "$REF" >/dev/null 2>&1 && ok "check clean after heal" || bad "still drifted after heal"
ls "$SCRATCH"/play-fix.sh.bak-* >/dev/null 2>&1 && ok "prior copy backed up" || bad "no backup made"

echo "== --apply HEALS a symlinked dest into a regular file (security invariant) =="
rm -f "$SCRATCH/play-fix.sh"; ln -s /etc/hosts "$SCRATCH/play-fix.sh"
"$SYNC" --apply "$REF" >/dev/null 2>&1 && ok "apply over a symlink exits 0" || bad "apply over symlink failed"
[[ ! -L "$SCRATCH/play-fix.sh" ]] && ok "symlinked dest replaced by a regular file" || bad "still a symlink"

echo "== --check flags a SYMLINK as drift (security invariant, review MED-2) =="
"$SYNC" --apply "$REF" >/dev/null 2>&1                             # clean slate
rm -f "$SCRATCH/play-fix.sh"; ln -s /etc/hosts "$SCRATCH/play-fix.sh"
if "$SYNC" --check "$REF" >/dev/null 2>&1; then bad "check must flag a symlinked copy"; else ok "symlinked deployed copy -> check reports drift (not fooled by referent content)"; fi
"$SYNC" --apply "$REF" >/dev/null 2>&1                             # heal back to a real file

echo "== --apply refuses when a deploy lock is held (review MED-1) =="
mkdir "$SCRATCH/.deploy.lock"
if "$SYNC" --apply "$REF" >/dev/null 2>&1; then bad "apply must refuse while lock held"; else ok "held deploy lock -> apply refuses (no torn deploy)"; fi
rmdir "$SCRATCH/.deploy.lock"
"$SYNC" --apply "$REF" >/dev/null 2>&1 && ok "apply works again after lock released" || bad "apply should work once lock cleared"
[[ ! -e "$SCRATCH/.deploy.lock" ]] && ok "apply releases its lock on exit" || bad "lock leaked after apply"

echo "== --preflight is ADVISORY (always exit 0), even on drift =="
printf '\n# tampered again\n' >> "$SCRATCH/play-review.sh"
"$SYNC" --preflight "$REF" >/dev/null 2>&1 && ok "preflight exits 0 despite drift (cannot brick pool)" || bad "preflight must not fail"

echo "== --preflight does NOT hash a symlink (no FIFO-hang, review r2 MED-2) =="
"$SYNC" --apply "$REF" >/dev/null 2>&1
rm -f "$SCRATCH/play-fix.sh"; ln -s /etc/hosts "$SCRATCH/play-fix.sh"
"$SYNC" --preflight "$REF" >/dev/null 2>&1 && ok "preflight on a symlink completes + exits 0 (continues, never hashes)" || bad "preflight must not fail/hang on symlink"
"$SYNC" --apply "$REF" >/dev/null 2>&1                             # heal

echo "== --apply does NOT hang on a FIFO at the dest path (review r4 HIGH) =="
"$SYNC" --apply "$REF" >/dev/null 2>&1
rm -f "$SCRATCH/play-fix.sh"; mkfifo "$SCRATCH/play-fix.sh"
# with the -f&&!-L backup guard, cp never touches the FIFO; run in bg + kill-guard so a REGRESSION
# (hang) fails the test instead of wedging the suite.
( "$SYNC" --apply "$REF" >/dev/null 2>&1 ) & ap=$!
( sleep 15; kill "$ap" 2>/dev/null ) & kg=$!
if wait "$ap" 2>/dev/null; then kill "$kg" 2>/dev/null; [[ -f "$SCRATCH/play-fix.sh" && ! -p "$SCRATCH/play-fix.sh" ]] && ok "FIFO at dest -> apply completes + heals to a regular file (no cp hang)" || bad "FIFO not healed to regular file"; else bad "apply HUNG on a FIFO (r4 regression)"; fi

echo "== trap does NOT execute injected command substitution (review r2 CRITICAL) =="
PWN="$SCRATCH/PWNED_MARKER"
EVIL_DEST="$SCRATCH/d\$(touch $PWN)x"                              # dir name literally contains \$(...)
mkdir -p "$EVIL_DEST"
DEPLOY_SYNC_DEST="$EVIL_DEST" "$SYNC" --apply "$REF" >/dev/null 2>&1 || true
[[ ! -e "$PWN" ]] && ok "a \$()-bearing DEPLOY_SYNC_DEST does not execute on trap fire" || bad "TRAP INJECTION FIRED"

echo "== --apply with NO usable ref fails closed — never the origin/main default (reviews 37732 #2, 97191) =="
# three shapes of "no ref": quoted empty (\$#==2), an UNQUOTED empty var that word-splitting drops
# (\$#==1 — the case a \$#-based guard misses), and the ref simply omitted.
EMPTY_REF=""
for shape in quoted unquoted omitted; do
  d="$SCRATCH/noref-$shape"; mkdir -p "$d"; er_rc=0
  case "$shape" in
    quoted)   er_out="$(DEPLOY_SYNC_DEST="$d" "$SYNC" --apply "" 2>&1)" || er_rc=$? ;;
    unquoted)
      # shellcheck disable=SC2086  # deliberately UNQUOTED: reproduces the arg-collapse call shape
      er_out="$(DEPLOY_SYNC_DEST="$d" "$SYNC" --apply $EMPTY_REF 2>&1)" || er_rc=$? ;;
    omitted)  er_out="$(DEPLOY_SYNC_DEST="$d" "$SYNC" --apply 2>&1)" || er_rc=$? ;;
  esac
  [[ "$er_rc" -ne 0 ]] && ok "$shape ref exits nonzero" || bad "$shape ref was ACCEPTED (rc=0)"
  grep -q "requires an explicit, non-empty ref" <<<"$er_out" && ok "$shape ref names itself" || bad "$shape ref: no explicit refusal ($er_out)"
  [[ ! -e "$d/play-fix.sh" && ! -e "$d/play-review.sh" ]] && ok "$shape ref installs nothing" || bad "$shape ref installed files"
done
# read-only --check keeps its origin/main default (only the MUTATING mode lost it)
ck_rc=0; ck_out="$(DEPLOY_SYNC_DEST="$SCRATCH" "$SYNC" --check 2>&1)" || ck_rc=$?
# rc 0 = in sync and 1 = drift are both a check that RAN; anything else is a crash. rc 1 is also
# die()'s code, so require positive evidence too: a SAME/DRIFT verdict line and no ERROR line.
# Without this, a crash whose output lacked the refusal text passed the grep below (review 73700).
grep -qE '\] (SAME|DRIFT) ' <<<"$ck_out"; ck_verdict=$?
grep -q 'ERROR:' <<<"$ck_out"; ck_err=$?
[[ ("$ck_rc" -eq 0 || "$ck_rc" -eq 1) && "$ck_verdict" -eq 0 && "$ck_err" -eq 1 ]] \
  && ok "--check ran to a SAME/DRIFT verdict (rc=$ck_rc)" || bad "--check did not run to a verdict (rc=$ck_rc): $ck_out"
# grep rc 1 = no match = default kept; `! grep` would also pass on rc 2 (grep error) — require exactly 1.
grep -q "requires an explicit" <<<"$ck_out"; ck_rc=$?
[[ "$ck_rc" -eq 1 ]] && ok "--check with no ref still runs (default kept)" || bad "--check lost its default, or grep failed (rc=$ck_rc): $ck_out"

echo "== runs from a git WORKTREE (its .git is a FILE, not a dir) =="
# REPO is derived from the script's own location, so the case needs a copy of THIS working-tree
# script inside a real worktree; the installed surface is still read from the ref, not the copy.
if git -C "$DIR/.." worktree add -q --detach "$SCRATCH/wt" HEAD >/dev/null 2>&1; then
  cp "$SYNC" "$SCRATCH/wt/orchestrator/deploy-sync.sh"; mkdir -p "$SCRATCH/wt-dest"
  wt_rc=0; wt_out="$(DEPLOY_SYNC_DEST="$SCRATCH/wt-dest" \
    "$SCRATCH/wt/orchestrator/deploy-sync.sh" --apply HEAD 2>&1)" || wt_rc=$?
  [[ "$wt_rc" -eq 0 && -f "$SCRATCH/wt-dest/play-review.sh" ]] \
    && ok "--apply from a worktree installs" || bad "--apply from a worktree refused (rc=$wt_rc): $wt_out"
else
  bad "could not create a scratch worktree to test against"
fi

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
