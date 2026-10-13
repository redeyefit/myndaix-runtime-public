#!/usr/bin/env bash
# deploy-sync.sh — deterministic sync + drift guard for the MANUALLY-COPIED, load-bearing
# orchestrator scripts that live OUTSIDE the repo (the autonomous-loop deploy surface).
#
# WHY: the autonomous review/fix loop runs two scripts from ~/.myndaix/orchestrator/ that are
# HAND-COPIED from the repo (a deliberate security boundary — play-fix.sh must be a real regular
# file the review worker can't hijack via a repo symlink). Nothing auto-syncs them, so a merged
# fix can silently NOT reach production. On 2026-07-24 PR#112 merged but the deployed play-fix.sh
# sat on the pre-flock version for 3 days, caught by luck not signal (see the runtime-deploy
# topology audit). This turns that silent, luck-dependent failure into a loud, deterministic one.
#
# Surface (audited — exactly these two files, everything else runs repo-direct):
#   ~/.myndaix/orchestrator/play-fix.sh      (autofix worker — anti-symlink security boundary)
#   ~/.myndaix/orchestrator/play-review.sh   (review worker, re-exec'd by the pre-push dispatcher)
#
# Modes:
#   --check       read-only: report SAME/DRIFT of each deployed copy vs <ref>; exit 1 on any drift.
#   --apply       sync each file from <ref> (atomic, backed up, hash-verified, regular-file-asserted);
#                 stamp ~/.myndaix/orchestrator/.deployed-sha with the deployed commit.
#   --preflight   ADVISORY (always exit 0): warn if deployed copies drift OR the repo working tree
#                 HEAD != the stamped deploy sha (branch-float). Safe to call at pool start.
#
#   deploy-sync.sh --check|--preflight [ref]   ref defaults to origin/main (read-only)
#   deploy-sync.sh --apply <ref>               ref REQUIRED — the mutating mode has no default
#
# House rules: bash-scripts.md — set -euo pipefail, PATH pin, atomic mv, quote all, no eval.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DEST="${DEPLOY_SYNC_DEST:-$HOME/.myndaix/orchestrator}"   # overridable for tests
STAMP="$DEST/.deployed-sha"
# the audited copied surface: "<repo-relative src>:<absolute dest>" (split on the FIRST ':' only)
FILES=(
  "orchestrator/play-fix.sh:$DEST/play-fix.sh"
  "orchestrator/play-review.sh:$DEST/play-review.sh"
)

mode="${1:-}"
ref="${2:-origin/main}"

_LOCK=""                                              # script-scope so the EXIT trap sees it (review r2 CRIT)

log(){ printf '[%s] [deploy-sync] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die(){ log "ERROR: $*" >&2; exit 1; }
# --apply (the ONE mutating mode) takes NO default ref: a missing ref must fail closed, never
# silently deploy origin/main ungated. Testing $# is NOT enough — an UNQUOTED empty variable
# (`--apply $S`, S="") is dropped by word-splitting, so the call arrives with $#==1, looking
# exactly like "no ref given" (reviews 37732 #2, 97191). Read-only --check/--preflight keep the default.
if [[ "$mode" == "--apply" && -z "${2:-}" ]]; then die "--apply requires an explicit, non-empty ref — origin/<branch>, HEAD, or a full sha"; fi
# lock release as a FUNCTION (never interpolate a path into a trap string — a $()-bearing
# DEPLOY_SYNC_DEST would inject live command substitution at trap-fire time; review r2 CRITICAL).
_release_lock(){ [[ -n "${_LOCK:-}" ]] && rmdir "$_LOCK" 2>/dev/null; return 0; }

# -e not -d: `.git` is a FILE in a git worktree — running this from one must not be refused
[[ -e "$REPO/.git" ]] || die "repo not a git dir: $REPO"
[[ -d "$DEST" ]]      || die "deploy dest missing: $DEST"

# blob sha of a path AT a git ref (the intended/committed content); arg = repo-relative path
ref_blob(){ git -C "$REPO" rev-parse --verify --quiet "$ref:$1"; }
# blob sha of a file's CURRENT content on disk (git's own content hash — comparable to ref_blob).
# NEVER follow a symlink (review r2): git hash-object dereferences, which both hides a symlink
# security-boundary violation AND can hang forever on a symlink-to-FIFO/device. A non-regular or
# symlinked path returns MISSING (reads as drift) rather than being hashed.
# ACCEPTED RESIDUALS (review r3, declined by design):
#  - the [[ -f && ! -L ]] guard and the hash are separate syscalls (a same-user writer could swap a
#    symlink in between). NOT in the threat model: a same-user process on this solo single-host box
#    can edit the deployed files directly — racing this guard buys nothing. The O_NOFOLLOW fd dance
#    to close it is over-engineering for a non-threat.
#  - a git hash-object FAILURE (not just absence) also returns MISSING → fail-safe (reads as drift →
#    redeploy). We don't log it: disk_blob runs in tight check/preflight loops; noise > signal here.
disk_blob(){ [[ -f "$1" && ! -L "$1" ]] || { echo "MISSING"; return; }; git -C "$REPO" hash-object "$1" 2>/dev/null || echo "MISSING"; }

do_check(){
  local drift=0 pair src dst f want have
  ref="$(git -C "$REPO" rev-parse --verify "$ref")" || die "cannot resolve ref: $ref"  # pin (r4): one commit for all files
  for pair in "${FILES[@]}"; do
    src="${pair%%:*}"; dst="${pair#*:}"; f="$(basename "$dst")"
    # SECURITY-boundary invariant (review MED-2): git hash-object follows symlinks, so a symlinked
    # deployed copy pointing at matching content would hash SAME and hide the violation. Check the
    # link-ness FIRST — a symlink is drift regardless of what it points at.
    if [[ -L "$dst" ]]; then
      log "DRIFT $f  SECURITY: deployed copy is a SYMLINK (must be a real regular file)"; drift=1; continue
    fi
    want="$(ref_blob "$src")" || die "path not found at $ref: $src"
    have="$(disk_blob "$dst")"
    if [[ "$want" == "$have" ]]; then
      log "SAME  $f  ($want)"
    else
      log "DRIFT $f  deployed=$have  want($ref)=$want"; drift=1
    fi
  done
  return "$drift"
}

do_apply(){
  # _LOCK stays SCRIPT-scope on purpose (top-of-file comment: the EXIT trap must see it) —
  # r6 P5's "make it local" would strand the lock at trap time. _head_now IS local.
  local pair src dst ddir f want tmp bak deployed_sha _head_now _branch _branch_re
  # scope this call's ref mutations (normalization, qualification, commit pin) — bash
  # locals are dynamically scoped, so ref_blob() still reads this value (r13 #3)
  local ref="$ref"
  # serialize (review MED-1): two concurrent --apply could interleave the per-file mv's and leave a
  # torn deploy (play-fix from ref A, play-review from ref B, stamp = last writer). Atomic mkdir lock
  # (portable — no flock binary dep); no stale-reaper (a human deploy tool: a stranded lock is a
  # loud, operator-clearable signal, not worth the reaper complexity #112 cycled on).
  _LOCK="$DEST/.deploy.lock"
  # distinguish EEXIST (another deploy) from a real mkdir failure (perms/ENOSPC) — surface the OS
  # error instead of a misleading "another deploy holds" (review r4).
  local mkerr
  if ! mkerr="$(mkdir "$_LOCK" 2>&1)"; then
    if [[ -d "$_LOCK" ]]; then die "another deploy holds $_LOCK — remove it if stale"
    else die "cannot create lock $_LOCK: $mkerr"; fi
  fi
  trap _release_lock EXIT                             # function form — no path interpolation (r2 CRIT)
  # refresh the tracking ref when deploying from a remote. Fetch failure is FATAL in --apply (review
  # r4): silently deploying a stale/rolled-back local origin/main is the exact silent-non-deploy this
  # tool exists to prevent. (--preflight tolerates it — it's advisory.)
  # explicit DESTINATION refspec (r7 #2), DERIVED from the requested ref (r8 P1: a
  # hardcoded main would refresh the wrong ref for --apply origin/<other> and then bless
  # the stale local tracking ref — the exact silent-non-deploy this tool prevents). A bare
  # `fetch origin <branch>` lands only in FETCH_HEAD under non-wildcard fetch configs.
  # No stderr suppression: fetch warnings are signal. Non-ff (rolled-back origin) -> die.
  # normalize the fully-qualified spelling FIRST (r11 #1: refs/remotes/origin/<x> matched
  # no case arm and skipped the fetch entirely — same stale-ref fail-open, different door)
  case "$ref" in refs/remotes/origin/*) ref="origin/${ref#refs/remotes/origin/}" ;; esac
  case "$ref" in
    origin/*)
      _branch="${ref#origin/}"
      # PLAIN branch names only — anything else DIES (r10 #1: the bare-fetch fallback for
      # revision expressions could no-op under a narrow remote.origin.fetch and bless a
      # stale tracking ref — the exact fail-open this script prevents; expressions like
      # origin/main~1 must be operator-resolved to a sha first). First char alphanumeric
      # (r10 #3: a leading '-' would parse as a git OPTION, not a refspec). Regex in a
      # var (house idiom; probed fine unquoted on 3.2.57, the var form is the belt).
      _branch_re='^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9._-]+)*$'
      [[ "$_branch" =~ $_branch_re ]] \
        || die "unsupported ref '$ref' for --apply: use origin/<branch>, HEAD, or a full sha (resolve revision expressions like ~1 to a sha first)"
      git -C "$REPO" fetch --quiet origin "${_branch}:refs/remotes/origin/${_branch}" \
        || die "fetch failed for $ref — refusing to deploy a possibly-stale local ref"
      # resolve the FULLY-QUALIFIED name we just fetched (r12 #1): shorthand origin/<b>
      # goes through git's disambiguation, where a local branch/tag literally named
      # 'origin/<b>' would shadow the tracking ref we refreshed
      ref="refs/remotes/origin/${_branch}"
      ;;
  esac
  deployed_sha="$(git -C "$REPO" rev-parse --verify "$ref")" || die "cannot resolve ref: $ref"
  # version-skew guard (review r5 #2, hardened r6 P1): the phone wrapper's marker contract
  # runs against the CHECKED-OUT tree's cli.py — deploying a ref that advanced past the
  # running checkout silently skews wrapper vs serve. FAIL CLOSED: warn-and-continue was
  # exactly the mixed-tree deploy this guard exists to stop. --apply HEAD never trips it;
  # DEPLOY_SYNC_ALLOW_SKEW=1 is the deliberate operator override.
  _head_now="$(git -C "$REPO" rev-parse --verify --quiet HEAD || echo unknown)"
  if [[ "$_head_now" != "$deployed_sha" ]]; then
    if [[ "${DEPLOY_SYNC_ALLOW_SKEW:-}" == "1" ]]; then
      log "WARN: SKEW override — deploying $deployed_sha over working-tree HEAD $_head_now"
    else
      die "refusing skewed deploy: ref resolves to $deployed_sha but working-tree HEAD is $_head_now — pull first, use '--apply HEAD', or set DEPLOY_SYNC_ALLOW_SKEW=1"
    fi
  fi
  # PIN to the immutable commit for the rest of the loop (review r3 MAJOR-1): reading blobs through
  # the moving $ref (origin/main) could deploy file A from one commit and file B from another if the
  # ref advances mid-loop, then stamp the earlier sha — a silent mixed-commit deploy. All subsequent
  # blob reads use $deployed_sha, so the two files + the stamp always describe ONE commit.
  ref="$deployed_sha"
  for pair in "${FILES[@]}"; do
    src="${pair%%:*}"; dst="${pair#*:}"; f="$(basename "$dst")"
    # the phone wrapper's home may not exist on a fresh box — create it, then assert
    # non-symlink ADJACENT to use (r5 #3: check-then-mkdir left a wider race window; the
    # residual same-user swap between this assert and the mv below stays out of the threat
    # model per the accepted-residuals note above — a same-user writer edits files directly).
    ddir="$(dirname "$dst")"
    mkdir -p "$ddir" || die "cannot create dest dir $ddir"
    [[ -L "$ddir" ]] && die "SECURITY: dest dir $ddir is a symlink — refusing"
    want="$(ref_blob "$src")" || die "path not found at $deployed_sha: $src"
    # unpredictable, O_EXCL temp (review HIGH-1): a PID-named ($$) temp with '>' follows a
    # pre-planted same-user symlink; mktemp uses O_EXCL + random suffix, closing that vector on the
    # very files that ARE the security boundary.
    tmp="$(mktemp "$ddir/.$f.tmp.XXXXXX")" || die "mktemp failed in $ddir"
    # materialize the EXACT committed blob (not the working tree — avoids dirty-tree bleed)
    git -C "$REPO" show "$ref:$src" > "$tmp" || { rm -f "$tmp"; die "git show failed for $src"; }
    chmod +x "$tmp"
    # verify BEFORE install — never place a mismatched file
    [[ "$(disk_blob "$tmp")" == "$want" ]] || { rm -f "$tmp"; die "hash mismatch building $f (refusing)"; }
    # backup any existing REGULAR deployed copy (reversible). SKIP a symlinked source (review r3
    # MAJOR-2): cp -p follows the link and would hang forever on a symlink-to-FIFO/device — and
    # backing up a planted symlink is pointless anyway. The mv below heals it to a regular file.
    if [[ -f "$dst" && ! -L "$dst" ]]; then
      bak="$dst.bak-$(date '+%Y%m%d%H%M%S')"; cp -Pp "$dst" "$bak"   # -P: never deref (belt)
    elif [[ -e "$dst" ]]; then
      # a SYMLINK or a special file (FIFO/device) sits where a real script should — do NOT cp it
      # (cp would hang forever on a FIFO; review r4), just log and let the mv below heal it.
      log "SECURITY: $f is not a regular file (symlink/FIFO/device) — not backing up; healing"
    fi
    mv -f "$tmp" "$dst"                                    # atomic rename (same fs); replaces a symlink
    [[ ! -L "$dst" ]] || die "SECURITY: $f became a symlink — refusing"   # regular-file invariant
    [[ "$(disk_blob "$dst")" == "$want" ]] || die "post-install hash mismatch $f"
    log "APPLIED $f -> $dst  ($want)"
  done
  printf '%s\n' "$deployed_sha" > "$STAMP"
  log "stamped .deployed-sha = $deployed_sha"
}

do_preflight(){
  # ADVISORY only — never fails the caller (pool start must not be bricked by this guard).
  local f want have head_sha stamped
  ref="$(git -C "$REPO" rev-parse --verify "$ref" 2>/dev/null || printf '%s' "$ref")"  # best-effort pin (r4); never dies
  local pair src dst
  for pair in "${FILES[@]}"; do
    src="${pair%%:*}"; dst="${pair#*:}"; f="$(basename "$dst")"
    # continue BEFORE hashing (review r2 MED): never call git hash-object on a symlink — it would
    # hang forever on a symlink-to-FIFO/device, and this advisory must never stall pool start.
    if [[ -L "$dst" ]]; then
      log "PREFLIGHT WARN: $f is a SYMLINK — security-boundary violation (must be a real file)"; continue
    fi
    want="$(ref_blob "$src" 2>/dev/null || echo '?')"; have="$(disk_blob "$dst")"
    [[ "$want" == "$have" ]] || log "PREFLIGHT WARN: deployed $f drifts from $ref (deployed=$have want=$want)"
  done
  if [[ -f "$STAMP" ]]; then
    stamped="$(cat "$STAMP" 2>/dev/null || true)"
    head_sha="$(git -C "$REPO" rev-parse --verify --quiet HEAD || echo unknown)"
    [[ "$head_sha" == "$stamped" ]] || \
      log "PREFLIGHT WARN: working-tree HEAD ($head_sha) != stamped deploy sha ($stamped) — pool may run un-deployed code (branch-float)"
  else
    log "PREFLIGHT WARN: no .deployed-sha stamp — run 'deploy-sync.sh --apply <ref>' to establish it"
  fi
  return 0
}

case "$mode" in
  --check)     do_check ;;
  --apply)     do_apply ;;
  --preflight) do_preflight ;;
  *) die "usage: deploy-sync.sh --check|--apply|--preflight [ref]" ;;
esac
