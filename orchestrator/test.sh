#!/usr/bin/env bash
# test.sh — smoke test for play-review.sh. Drives the WORKER against a throwaway
# git repo with a STUBBED mxr/osascript (no real dispatch, no runtime, no 300s).
# Run: bash orchestrator/test.sh   (exits non-zero if any case fails)
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/play-review.sh"
ROOT="$(mktemp -d /tmp/playrev-test.XXXXXX)"
FAKE="$ROOT/home"
REPO="$ROOT/repo"
PASS=0; FAIL=0
trap 'rm -rf "$ROOT"' EXIT

# --- stub mxr + osascript on the fake HOME's PATH (script puts ~/.local/bin first) ---
mkdir -p "$FAKE/.local/bin"
cat > "$FAKE/.local/bin/mxr" <<'STUB'
#!/usr/bin/env bash
agent="$1"; prompt="$2"
printf '%s\t%s\t%s\n' "$agent" "${MXR_TIMEOUT_S:-unset}" "$*" >> "$HOME/.myndaix/mxr-argv.log" 2>/dev/null || true   # PR-0a: argv (+ MXR_TIMEOUT_S) so tests can assert scope flags + review-call timeout
# `mxr capture-record ...` / `mxr outcome-record ...` — instrumentation verbs (both --list-tags
# source-of-truth + the record call). Handled BEFORE the agent case: their $1 is a verb, not an agent.
case "$agent" in
  capture-record)
    if [[ "${2:-}" == "--list-tags" ]]; then printf 'fail-open\ntoctou-race\n'; exit 0; fi
    exit 0 ;;   # record call: no-op stub (the real behavior is tested in python)
  outcome-record)
    if [[ "${2:-}" == "--list-tags" ]]; then printf 'fail-open\ntoctou-race\n'; exit 0; fi
    # record call: log it (so tests can assert it fired) + emit a canned recorded-key line so the
    # follow-up keys-file path is exercised, gated by STUB_OUTCOME_KEYS (empty -> nothing recorded).
    printf 'outcome-record\t%s\n' "$*" >> "$HOME/.myndaix/outcome-argv.log" 2>/dev/null || true
    [[ -n "${STUB_OUTCOME_KEYS:-}" ]] && printf '%s\n' "$STUB_OUTCOME_KEYS"
    exit 0 ;;
  review-stage)
    # PR-2 staging primitive: STUB_STAGE_FAIL simulates an infra failure (empty stdout + a
    # control-charged reason on stderr, to exercise the degradation/fail-closed + clean() paths).
    # STUB_STAGE_FAIL_WITH_PATH prints a REAL dir to stdout but exits NON-ZERO — the "partial path
    # before a late failure" case (kilabz HIGH): play-review must key on the EXIT STATUS, not the
    # stdout shape, so this must still take the fail-closed/degrade branch. Otherwise: mkdir a fake
    # review-* dir and echo it (the ONLY stdout) with exit 0.
    if [[ -n "${STUB_STAGE_FAIL:-}" ]]; then printf 'staging failed: %b\n' "stub \033[31mreason\033[0m" >&2; exit 1; fi
    d="$HOME/.myndaix/orchestrator/staging/review-stub-$$-$RANDOM"
    mkdir -p "$d" 2>/dev/null || { echo "mkdir failed" >&2; exit 1; }
    printf '%s\n' "$d"
    [[ -n "${STUB_STAGE_FAIL_WITH_PATH:-}" ]] && exit 1   # path printed, but FAILED -> must not read as success
    exit 0 ;;
  review-teardown)
    printf 'review-teardown\t%s\n' "$*" >> "$HOME/.myndaix/teardown-argv.log" 2>/dev/null || true
    [[ -n "${2:-}" ]] && rm -rf "$2" 2>/dev/null || true; exit 0 ;;
  review-reap)
    printf 'review-reap\n' >> "$HOME/.myndaix/reap-calls" 2>/dev/null || true; exit 0 ;;
esac
case "$prompt" in
  *READY*) if [[ "${STUB_CANARY_FAIL:-}" == "$agent" ]]; then
             [[ -n "${STUB_CANARY_ERR:-}" ]] && printf '%b\n' "$STUB_CANARY_ERR" >&2   # the captured .err the cause label is read from
             exit 1
           fi
           echo READY; exit 0 ;;
esac
case "$agent" in
  kilabz)  [[ -n "${STUB_KILABZ_FAIL:-}" ]] && exit 1; echo "${STUB_REVIEW:-bug: line 1 returns a-b}" ;;
  lobster) [[ -n "${STUB_LOBSTER_FAIL:-}" ]] && exit 1; echo "${STUB_TRIAGE:-1. fix the subtraction}" ;;
  # `mxr skillselect ...` (+learning Step 4): default-OFF emits empty (models SKILLS_ENABLED
  # absent). STUB_ARMED lets a test inject a canned (already-fenced) hint region.
  skillselect) printf '%s' "${STUB_ARMED:-}" ;;
  *) echo "stub:$agent" ;;
esac
STUB
printf '%s\n' '#!/usr/bin/env bash' 'mkdir -p "$HOME/.myndaix" 2>/dev/null' 'echo called >> "$HOME/.myndaix/osascript-calls"' 'exit 0' > "$FAKE/.local/bin/osascript"
chmod +x "$FAKE/.local/bin/mxr" "$FAKE/.local/bin/osascript"

# --- a throwaway git repo with one real commit ---
git init -q "$REPO"
git -C "$REPO" config user.email t@t; git -C "$REPO" config user.name t
printf 'def add(a,b): return a-b\n' > "$REPO/m.py"
git -C "$REPO" add -A; git -C "$REPO" commit -qm init
# PIN the branch name: `git init`'s initial branch follows init.defaultBranch, so on a `master`
# box the local trunk play-review looks for ("main") would not exist and the trunk-resolution
# cases (50c/50e) would silently stop testing what they claim to (kilabz LOW). -M, not `init -b`:
# works on git < 2.28 too.
git -C "$REPO" branch -M main
TIP="$(git -C "$REPO" rev-parse HEAD)"
EMPTY=4b825dc642cb6eb9a060e54bf8d69288fbee4904
INBOX="$FAKE/.myndaix/bridge/inbox/jefe"
STATE="$FAKE/.myndaix/orchestrator/state"
REPOS_JSON="$FAKE/.myndaix/orchestrator/repos.json"   # PLAY_AUTOFIX gate reads this
RUNS="$FAKE/.myndaix/orchestrator/runs"
# scoped transient marker: transient-<repo>-<ref>-<sha> (repo basename 'repo', refs/heads/main
# slugged). HARDCODED on purpose — this string is the bash<->python contract, don't derive it.
TMARKER="$STATE/transient-repo-refs-heads-main-$TIP"
# scoped done marker: done-<repo>-<ref>-<sha>, same contract as TMARKER (was a GLOBAL done-<sha>).
DMARKER="$STATE/done-repo-refs-heads-main-$TIP"

# --- recording stub fixer, OUTSIDE the repo (the auto path rejects in-repo fixers). Records its
#     argv (overwrite) AND appends a per-call marker so we can assert fire-count. ---
FIXER="$ROOT/fake-play-fix.sh"
printf '%s\n' '#!/usr/bin/env bash' 'mkdir -p "$HOME/.myndaix" 2>/dev/null' \
  'printf "%s\n" "REMOTE=${MYNDAIX_FIX_REMOTE-}" "BASE_BRANCH=${MYNDAIX_FIX_BASE_BRANCH-}" > "$HOME/.myndaix/fixer-env"' \
  'printf "%s\n" "$#" "$@" > "$HOME/.myndaix/fixer-argv"' \
  'printf x >> "$HOME/.myndaix/fixer-calls"' 'exit 0' > "$FIXER"
chmod +x "$FIXER"
NULLCFG="$(printf '{"%s":{"path":"%s","fail_to_pass":null}}' "$(basename "$REPO")" "$REPO")"

reset(){ rm -rf "$FAKE/.myndaix"; }
run(){ env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "${1:-}" 2>"$ROOT/stderr"; }
# armed run: PLAY_AUTOFIX on, test-seam fixer wired
run_af(){ env HOME="$FAKE" PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$FIXER" \
            bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "${1:-}" 2>"$ROOT/stderr"; }
af_repos(){ mkdir -p "$(dirname "$REPOS_JSON")"; printf '%s' "$1" > "$REPOS_JSON"; }
wait_fixer(){ local _; for _ in $(seq 1 40); do [[ -f "$FAKE/.myndaix/fixer-argv" ]] && return 0; sleep 0.1; done; return 1; }
settle(){ sleep 0.6; }   # let a (possible) detached fire either land or prove absent
latest(){ ls -t "$INBOX"/*.md 2>/dev/null | head -1; }
ck(){ # ck <label> <substr> <file-or-empty>
  local f="${3:-$(latest)}"
  if [[ -n "$f" && -f "$f" ]] && grep -q "$2" "$f"; then echo "  ok: $1"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (want '$2')"; FAIL=$((FAIL+1)); fi
}
cknot(){ # cknot <label> <substr> <file-or-empty> — assert substr ABSENT; FAIL if the delivery is
  # MISSING (oracle r4: a bare `grep -q ... 2>/dev/null` in the else-branch conflates a vanished
  # file with a clean pass — positive proof the message exists is required before trusting absence).
  local f="${3:-$(latest)}"
  if [[ -z "$f" || ! -f "$f" ]]; then echo "  FAIL: $1 (no delivery to inspect)"; FAIL=$((FAIL+1));
  elif grep -q "$2" "$f"; then echo "  FAIL: $1 (unwanted '$2')"; FAIL=$((FAIL+1));
  else echo "  ok: $1"; PASS=$((PASS+1)); fi
}
ckfile(){ if [[ -e "$1" ]]; then echo "  ok: $2"; PASS=$((PASS+1)); else echo "  FAIL: $2 (missing $1)"; FAIL=$((FAIL+1)); fi; }
cknofile(){ if [[ ! -e "$1" ]]; then echo "  ok: $2"; PASS=$((PASS+1)); else echo "  FAIL: $2 (exists $1)"; FAIL=$((FAIL+1)); fi; }
ckexit(){ if [[ "$1" == "$2" ]]; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3 (rc $1 != $2)"; FAIL=$((FAIL+1)); fi; }
# gate-mode run: synchronous --worker with PLAY_GATE on; writes a structured verdict, no detach
gate_run(){ env HOME="$FAKE" PLAY_GATE=1 PLAY_GATE_VERDICT="$ROOT/verdict.json" PLAY_GATE_RUN_ID=run123 \
              bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "" 2>"$ROOT/stderr"; }

echo "1. NEEDS-FIX path";    reset; STUB_TRIAGE="1. fix it" run; ck "delivers NEEDS-FIX" "review NEEDS-FIX"
echo "2. clean PASS gate";   reset; STUB_TRIAGE="PLAY_PASS" run; ck "delivers PASS" "review PASS"
echo "2b. clean() keeps UTF-8 (em-dash, warning sign) and strips ESC + ENCODED C1 (U+009B)"
  # 2026-10-10: tr -d '\200-\237' deleted UTF-8 continuation bytes, so every verdict with an em-dash or
  # a banner glyph was invalid UTF-8 (grep needed -a; the banner checks below were red for weeks).
  reset; STUB_TRIAGE=$'Verified \xe2\x80\x94 fine \xe2\x9a\xa0 banner \xc2\x9b31m csi \x1b[31m esc\n\nPLAY_PASS' run
  ck "em-dash survives clean()" $'Verified \xe2\x80\x94 fine'
  ck "warning sign survives clean()" $'\xe2\x9a\xa0 banner'
  cknot "encoded C1 CSI (C2 9B) is stripped" $'\xc2\x9b'
  cknot "ESC is stripped" $'\x1b'
  if iconv -f UTF-8 -t UTF-8 "$(latest)" >/dev/null 2>&1; then echo "  ok: the delivery is valid UTF-8"; PASS=$((PASS+1)); else echo "  FAIL: the delivery is not valid UTF-8"; FAIL=$((FAIL+1)); fi
echo "3. canary failure";    reset; STUB_CANARY_FAIL=kilabz run; ck "aborts on canary" "ABORTED — canary"
echo "3b. canary abort marks transient (push mode); gate mode does NOT"; reset; STUB_CANARY_FAIL=kilabz run
  ckfile "$TMARKER" "push-mode canary abort writes the scoped transient marker"
  reset; STUB_CANARY_FAIL=kilabz gate_run >/dev/null 2>&1 || true
  cknofile "$TMARKER" "gate-mode canary abort writes NO transient marker"
echo "3c. canary abort names the REAL cause in the title + the transient marker (not 'auth or pool down')"
cktitle(){ # cktitle <label> <exact-title-substring> — asserts on the delivery's FIRST line only
  local f; f="$(latest)"
  if [[ -n "$f" ]] && head -1 "$f" | grep -qF "$2"; then echo "  ok: $1"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (title '$( [[ -n "$f" ]] && head -1 "$f")' lacks '$2')"; FAIL=$((FAIL+1)); fi
}
  reset; STUB_CANARY_FAIL=kilabz STUB_CANARY_ERR='ERROR: Your workspace is out of credits. Add credits to continue.' run
  cktitle "credits" "review ABORTED — canary: kilabz OUT OF CREDITS"
  if [[ "$(cat "$TMARKER" 2>/dev/null)" == "kilabz OUT OF CREDITS" ]]; then echo "  ok: marker carries the cause"; PASS=$((PASS+1)); else echo "  FAIL: marker content '$(cat "$TMARKER" 2>/dev/null)'"; FAIL=$((FAIL+1)); fi
  # atomic tmp+mv write: no temp file may be left behind next to the marker
  if ls "$STATE"/transient-*.tmp.* >/dev/null 2>&1; then echo "  FAIL: marker temp file left behind"; FAIL=$((FAIL+1)); else echo "  ok: marker written atomically (no temp left)"; PASS=$((PASS+1)); fi
  ck "body says the login is fine" "login is fine"
  reset; STUB_CANARY_FAIL=kilabz STUB_CANARY_ERR="ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Sep 24th, 2026 3:00 PM." run
  cktitle "usage limit + reset time" "canary: kilabz USAGE LIMIT (Sep 24th, 2026 3:00 PM)"
  reset; STUB_CANARY_FAIL=lobster STUB_CANARY_ERR='Credit balance is too low' run
  cktitle "claude-side credits wording" "canary: lobster OUT OF CREDITS"
  reset; STUB_CANARY_FAIL=kilabz STUB_CANARY_ERR='ERROR codex_api: failed to connect to websocket: HTTP error: 401 Unauthorized' run
  cktitle "auth" "canary: kilabz AUTH 401"
  reset; STUB_CANARY_FAIL=kilabz STUB_CANARY_ERR='JOB_ID=00483e09-4013-401d-b99d-f4b80ea01610\nMXR_JOB_FAILED' run
  cktitle "hex '401' in a job id is NOT auth" "canary: kilabz JOB FAILED"
  reset; STUB_CANARY_FAIL=kilabz STUB_CANARY_ERR='MXR_SYNC_TIMEOUT' run
  cktitle "sync-wait timeout" "canary: kilabz POOL SLOW"
  reset; STUB_CANARY_FAIL=kilabz run
  cktitle "no output at all" "canary: kilabz POOL DOWN?"
  reset; STUB_CANARY_FAIL=kilabz STUB_CANARY_ERR='\033[31mweird\033[0m $(touch /tmp/x) try again at <script>' run
  cktitle "unrecognized -> UNKNOWN, raw text never in the title" "canary: kilabz UNKNOWN"
echo "3d. merge bar: advisory-only triage = PASS (advisory); a late/trailing token = NEEDS-FIX; automerge gate stays strict"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n## Advisory\n- harden the X parser' run
  cktitle "advisory-only -> PASS (advisory)" "review PASS (advisory) — refs/heads/main"
  ck "advisory list is delivered" "harden the X parser"
  ck "says advisories are not required" "NOT required before merge"
  ckfile "$DMARKER" "advisory PASS marks the tip done"
  reset; af_repos "$NULLCFG"; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n- harden X' run_af; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "advisory PASS never fires autofix (no fix-list)"
  reset; STUB_TRIAGE=$'\n  PLAY_PASS_ADVISORY \r\n- x' run
  cktitle "leading blank line + spaces + CR on the token line still parse" "review PASS (advisory)"
  reset; STUB_TRIAGE=$'## Blocking\n1. real bug\nPLAY_PASS_ADVISORY' run
  cktitle "token quoted AFTER a blocking list -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE='PLAY_PASS_ADVISORY but item 1 is blocking' run
  cktitle "trailing text on the token line -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'\r\nPLAY_PASS_ADVISORY\r\n- x' run
  cktitle "a CRLF-only leading line is blank (trim before the blank test)" "review PASS (advisory)"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n## Non-blocking\n- x' run
  cktitle "a 'Non-blocking' heading is not a blocker" "review PASS (advisory)"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n## Blocking\n1. real bug A\n## Advisory\n- nit B' run
  cktitle "advisory token + a Blocking section -> NEEDS-FIX (review r1 HIGH)" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n**Blocking**\n1. real bug A' run
  cktitle "advisory token + a bold Blocking heading -> NEEDS-FIX" "review NEEDS-FIX"
  _fixlist(){ local d; d="$(ls -t "$RUNS" 2>/dev/null | head -1)"; cat "$RUNS/$d/fixlist.txt" 2>/dev/null; }
  # scope cut (r3): the fixer gets the WHOLE triage, advisories included — exact equality, not grep
  _mixed=$'## Blocking\n1. real bug A\n## Advisory\n- nit B'
  reset; STUB_TRIAGE="$_mixed" run
  if [[ "$(_fixlist)" == "$_mixed" ]]; then echo "  ok: the fixer gets the whole triage (no section filtering)"; PASS=$((PASS+1)); else echo "  FAIL: fixlist='$(_fixlist)'"; FAIL=$((FAIL+1)); fi
  ck "the delivered review shows the advisories" "nit B"
  reset; STUB_TRIAGE="1. fix it" run
  if [[ "$(_fixlist)" == "1. fix it" ]]; then echo "  ok: a legacy (headerless) fix-list passes through whole"; PASS=$((PASS+1)); else echo "  FAIL: legacy fixlist='$(_fixlist)'"; FAIL=$((FAIL+1)); fi
  # fail-closed: if the has_blocking scan itself fails, an advisory token must NOT pass. A stub awk
  # (first on the worker's PATH) fails ONLY that scan, recognized by its unique `print b + 0`.
  printf '%s\n' '#!/bin/bash' 'case "$*" in *"print b + 0"*) exit 2 ;; esac' 'exec /usr/bin/awk "$@"' > "$FAKE/.local/bin/awk"; chmod +x "$FAKE/.local/bin/awk"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n- harmless nit' run
  rm -f "$FAKE/.local/bin/awk"
  cktitle "a FAILED blocking scan fails closed -> NEEDS-FIX" "review NEEDS-FIX"
  reset; rm -f "$ROOT/verdict.json"; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n- x' gate_run; ckexit $? 1 "automerge gate: advisory is NOT a pass (exit 1)"
  ck "gate verdict says NEEDS-FIX" '"verdict":"NEEDS-FIX"' "$ROOT/verdict.json"
  reset; STUB_TRIAGE="PLAY_PASS" run
  if grep '^lobster' "$FAKE/.myndaix/mxr-argv.log" 2>/dev/null | grep -q "MERGE BAR — classify every finding"; then echo "  ok: the rubric reaches lobster"; PASS=$((PASS+1)); else echo "  FAIL: lobster prompt lacks the MERGE BAR rubric"; FAIL=$((FAIL+1)); fi
  _bar_pr="$(grep -o 'MERGE_BAR="[^"]*"' "$SCRIPT" | head -1)"; _bar_xr="$(grep -o 'MERGE_BAR="[^"]*"' "$(dirname "$SCRIPT")/xreview.sh" | head -1)"
  if [[ -n "$_bar_pr" && "$_bar_pr" == "$_bar_xr" ]]; then echo "  ok: play-review + xreview carry the identical rubric"; PASS=$((PASS+1)); else echo "  FAIL: MERGE_BAR drifted between play-review.sh and xreview.sh"; FAIL=$((FAIL+1)); fi
echo "3e. preamble tolerance: prose before the token counts in push mode; list/bold/trailing/'Blocking' forms and the gate stay strict"
  _pre=$'Verified against the code snapshot: the oracle premise does not hold here.\n\nKilaBz found nothing.'
  reset; STUB_TRIAGE="$_pre"$'\n\nPLAY_PASS' run
  cktitle "prose preamble + PLAY_PASS -> clean PASS (not the advisory title)" "review PASS — refs/heads/main"
  ck "the preamble is delivered as triage notes" "triage notes"
  ckfile "$DMARKER" "preamble PASS marks the tip done"
  reset; STUB_TRIAGE="$_pre"$'\n\nPLAY_PASS_ADVISORY\n\n## Advisory\n1. harden the Y guard' run
  cktitle "prose preamble + PLAY_PASS_ADVISORY -> PASS (advisory)" "review PASS (advisory) — refs/heads/main"
  _adv="$(cat "$RUNS/$(ls -t "$RUNS" 2>/dev/null | head -1)/advisory.txt" 2>/dev/null)"
  if [[ "$_adv" == *"harden the Y guard"* && "$_adv" != *"Verified against"* ]]; then echo "  ok: advisory.txt = the list after the token, preamble excluded"; PASS=$((PASS+1)); else echo "  FAIL: advisory.txt='$_adv'"; FAIL=$((FAIL+1)); fi
  reset; STUB_TRIAGE=$'Two issues:\n1. real bug A\n\nPLAY_PASS' run
  cktitle "a numbered list before the token -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'**Note** a finding\n\nPLAY_PASS_ADVISORY\n- x' run
  cktitle "a bold line before the token -> NEEDS-FIX" "review NEEDS-FIX"
  # 2026-10-10 main repro: lobster's refutations as a bulleted list BEFORE the advisory token. Still
  # fail-closed, but the title and body must say so — a plain NEEDS-FIX over zero blockers misleads.
  reset; STUB_TRIAGE=$'Checked both threads.\n\n- **Finding 1:** refuted against the snapshot.\n- **Finding 2:** refuted.\n\nPLAY_PASS_ADVISORY\n\n## Advisory\n- split the copy change' run
  cktitle "a bulleted refutation list before PLAY_PASS_ADVISORY -> NEEDS-FIX tagged fail-closed (2026-10-10 repro)" "review NEEDS-FIX (fail-closed: unparsed verdict) — refs/heads/main"
  ck "the fail-closed body names the cause" "Fail-closed to NEEDS-FIX: a pass token is present"
  reset; STUB_TRIAGE=$'Issues found:\n1. the write path drops committed records\n2. the retry re-sends the stale body' run
  cktitle "a fix-list with no pass token and no Blocking line -> NEEDS-FIX tagged no-token" "review NEEDS-FIX (fail-closed: unparsed verdict)"
  ck "the no-token body says so" "no verdict token in the triage"
  reset; STUB_TRIAGE=$'The fix list follows.\n\n## Blocking\n1. the write path drops committed records' run
  cktitle "a Blocking section keeps the PLAIN NEEDS-FIX title (no fail-closed tag)" "review NEEDS-FIX — refs/heads/main"
  cknot "a Blocking section carries no fail-closed banner" "Fail-closed to NEEDS-FIX"
  reset; STUB_TRIAGE="$_pre"$'\n\nPLAY_PASS\n\nactually one more thing' run
  cktitle "text after PLAY_PASS -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'Blocking: X loses data.\n\nPLAY_PASS' run
  cktitle "a prose line starting 'Blocking' -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'__Blocking__\nThe write path drops committed records.\n\nPLAY_PASS' run
  cktitle "an underscore-bold Blocking heading before PLAY_PASS -> NEEDS-FIX (review 2026-09-27 repro)" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'> **Blocking**: the write path drops committed records.\n\nPLAY_PASS' run
  cktitle "a blockquoted Blocking line before PLAY_PASS -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'| finding | file |\n|---|---|\n| drops records | a.py |\n\nPLAY_PASS_ADVISORY\n- x' run
  cktitle "a table before the token -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n__Blocking__\n1. real bug A' run
  cktitle "advisory token + an underscore-bold Blocking heading -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n- Blocking: drops records' run
  cktitle "advisory token + a '- Blocking' list item -> NEEDS-FIX (review r2 repro)" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n1. **Blocking** drops records' run
  cktitle "advisory token + a numbered Blocking item -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n- [ ] Blocking: drops records' run
  cktitle "advisory token + a task-list Blocking item -> NEEDS-FIX (review r3 repro)" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n### \xf0\x9f\x9a\xa8 Blocking\n1. drops records' run
  cktitle "advisory token + an emoji-prefixed Blocking heading -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'[ ] fix the X path\n\nPLAY_PASS' run
  cktitle "a bare task-list line before PLAY_PASS -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'\xf0\x9f\x94\xb4 the lock in foo.sh can be skipped\n\xf0\x9f\x9f\xa1 naming nit\n\nPLAY_PASS' run
  cktitle "an emoji-bulleted finding list before PLAY_PASS -> NEEDS-FIX (review r4 repro)" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'\xe2\x80\xa2 drops committed records\n\nPLAY_PASS_ADVISORY\n- x' run
  cktitle "a U+2022-bulleted line before the advisory token -> NEEDS-FIX" "review NEEDS-FIX"
  reset; STUB_TRIAGE=$'`foo.sh:12` premise refuted against the snapshot.\n3 kilabz findings dropped as out of range.\n(oracle skipped)\n\nPLAY_PASS' run
  cktitle "prose opening with a backtick / digit-word / paren is still prose -> clean PASS" "review PASS — refs/heads/main"
  reset; STUB_TRIAGE=$'PLAY_PASS_ADVISORY\n- non-blocking: tidy the log line' run
  cktitle "a '- non-blocking' advisory item is NOT a blocker" "review PASS (advisory)"
  reset; rm -f "$ROOT/verdict.json"; STUB_TRIAGE="$_pre"$'\n\nPLAY_PASS' gate_run; ckexit $? 1 "automerge gate: preamble + PLAY_PASS is NOT a pass (exact whole-reply only)"
  ck "gate verdict says NEEDS-FIX" '"verdict":"NEEDS-FIX"' "$ROOT/verdict.json"
echo "3f. a linked worktree reports as its PRIMARY repo in emitted data; lock/marker keys stay per-checkout"
  WT="$ROOT/wt-agent-x"; git -C "$REPO" worktree add -q --detach "$WT" "$TIP"
  reset; STUB_TRIAGE="PLAY_PASS" env HOME="$FAKE" bash "$SCRIPT" --worker "$WT" "$EMPTY" "$TIP" refs/heads/main "" 2>"$ROOT/stderr"
  _lf="$(basename "$(latest)")"
  if [[ "$_lf" == *-repo-* && "$_lf" != *wt-agent-x* ]]; then echo "  ok: inbox file is named for the primary repo"; PASS=$((PASS+1)); else echo "  FAIL: inbox file '$_lf'"; FAIL=$((FAIL+1)); fi
  ck "verdict header names the primary repo" "repo: repo"
  if grep -q $'\tskillselect repo ' "$FAKE/.myndaix/mxr-argv.log" 2>/dev/null; then echo "  ok: review-skills lookup keyed on the primary repo"; PASS=$((PASS+1)); else echo "  FAIL: skillselect repo arg: $(grep skillselect "$FAKE/.myndaix/mxr-argv.log" 2>/dev/null | head -1)"; FAIL=$((FAIL+1)); fi
  ckfile "$STATE/done-wt-agent-x-refs-heads-main-$TIP" "done marker stays per-checkout (lock/marker keys unchanged)"
  git -C "$REPO" worktree remove --force "$WT"
echo "4. dedupe (2nd no-op)"; reset; STUB_TRIAGE="PLAY_PASS" run; before="$(ls "$INBOX" | wc -l)"; STUB_TRIAGE="PLAY_PASS" run; after="$(ls "$INBOX" | wc -l)"
  if [[ "$before" == "$after" ]]; then echo "  ok: 2nd run produced no new delivery"; PASS=$((PASS+1)); else echo "  FAIL: dedupe ($before -> $after)"; FAIL=$((FAIL+1)); fi
echo "5. daily cap";         reset; mkdir -p "$STATE"; printf 9999 > "$STATE/count-repo-$(date +%Y%m%d)"; STUB_TRIAGE="PLAY_PASS" run; ck "aborts on cap" "ABORTED — cap"
echo "6. corrupt counter (numeric guard)"; reset; mkdir -p "$STATE"; printf 'garbage' > "$STATE/count-repo-$(date +%Y%m%d)"; STUB_TRIAGE="PLAY_PASS" run; ck "survives corrupt counter" "review PASS"
echo "7. oversize diff FAILs fast (over the 256KB default cap)"; reset; head -c 300000 /dev/zero | tr '\0' 'x' > "$REPO/big.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm big; BIGTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$BIGTIP" refs/heads/main 2>/dev/null; ck "aborts oversize" "ABORTED — diff"
  git -C "$REPO" reset -q --hard "$TIP"   # restore
echo "7b. a ~100KB diff (over the OLD 64KB cap, under the new) now REVIEWS"; reset; head -c 100000 /dev/zero | tr '\0' 'y' > "$REPO/mid.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm mid; MIDTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$MIDTIP" refs/heads/main 2>/dev/null; ck "100KB diff reviews (not aborted)" "review PASS"
  git -C "$REPO" reset -q --hard "$TIP"   # restore
echo "7c. PLAY_MAX_DIFF knob still caps (env override)"; reset; head -c 5000 /dev/zero | tr '\0' 'z' > "$REPO/small.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm small; SMTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" PLAY_MAX_DIFF=1000 bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$SMTIP" refs/heads/main 2>/dev/null; ck "PLAY_MAX_DIFF=1000 caps a 5KB diff" "ABORTED — diff"
  git -C "$REPO" reset -q --hard "$TIP"   # restore
# The fixture must stay OVER the PLAY_MAX_DIFF_LINES default — bump it in lockstep with any
# default change, or 7d/7f assert nothing at all (an under-cap diff simply reviews).
echo "7d. changed-LINES cap FAILs fast (many small lines, way under the byte cap)"; reset; seq 1 5000 > "$REPO/lines.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm lines; LNTIP="$(git -C "$REPO" rev-parse HEAD)"
  env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$LNTIP" refs/heads/main 2>/dev/null; ck "5000 changed lines abort at the 4000 default" "changed lines"
  git -C "$REPO" reset -q --hard "$TIP"   # restore (LNTIP object stays reachable for 7e/7f)
echo "7e. PLAY_MAX_DIFF_LINES override raises the cap (same diff now reviews)"; reset
  env HOME="$FAKE" PLAY_MAX_DIFF_LINES=9000 STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$LNTIP" refs/heads/main 2>/dev/null; ck "9000-line cap lets the 5000-line diff review" "review PASS"
echo "7f. non-numeric PLAY_MAX_DIFF_LINES falls back to the 4000 default"; reset
  env HOME="$FAKE" PLAY_MAX_DIFF_LINES=banana bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$LNTIP" refs/heads/main 2>/dev/null; ck "garbage line cap still aborts the 5000-line diff" "changed lines"
echo "7g. leading-zero PLAY_MAX_DIFF_LINES is base-10, not octal (08 would crash [[ -le ]])"; reset
  env HOME="$FAKE" PLAY_MAX_DIFF_LINES=08 bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$LNTIP" refs/heads/main 2>/dev/null; ck "cap '08' = 8 aborts the 5000-line diff cleanly" "changed lines"
echo "7h. leading-zero PLAY_MAX_DIFF is base-10 (08=8B, not octal); a normal diff aborts cleanly"; reset
  env HOME="$FAKE" PLAY_MAX_DIFF=08 bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; ck "PLAY_MAX_DIFF '08' = 8B caps the diff (no octal crash)" "ABORTED — diff"
echo "7i. leading-zero PLAY_DAILY_CAP is base-10 (09=9, not an octal [[ -ge ]] crash)"; reset; mkdir -p "$STATE"; printf 5 > "$STATE/count-repo-$(date +%Y%m%d)"
  env HOME="$FAKE" PLAY_DAILY_CAP=09 STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; ck "cap '09'=9 with count 5 still reviews (no octal crash)" "review PASS"
echo "7j. deliver() strips control/ANSI (ESC) from the LLM verdict before it lands in the inbox file"; reset
  STUB_TRIAGE=$'1. \033[31mfake COMPLIANT\033[0m sneaky' run
  df="$(latest)"
  if [[ -n "$df" ]] && LC_ALL=C grep -q $'\033' "$df" 2>/dev/null; then echo "  FAIL: ESC survived into the verdict file"; FAIL=$((FAIL+1)); else echo "  ok: ESC stripped from delivered verdict"; PASS=$((PASS+1)); fi
echo "8. contention records a slug-scoped skip marker (content = the skipped base)"; reset; mkdir -p "$STATE/lock-repo"; STUB_TRIAGE="PLAY_PASS" run; ck "delivers SKIPPED" "review SKIPPED"
  SKMARKER="$STATE/skipped-repo-refs-heads-main-$TIP"   # same hardcoded bash<->front contract as TMARKER/DMARKER
  ckfile "$SKMARKER" "slug-scoped skipped marker written"
  got="$(cat "$SKMARKER" 2>/dev/null)"
  if [[ "$got" == "$EMPTY" ]]; then echo "  ok: marker content is the skipped range's base"; PASS=$((PASS+1)); else echo "  FAIL: marker content '$got' != $EMPTY"; FAIL=$((FAIL+1)); fi
  cknofile "$STATE/SKIPPED-$TIP" "legacy global SKIPPED sentinel no longer written"
  ck "notice: next review folds the range in" "folds it in" ; ck "notice offers the xreview manual option" "xreview.sh"
echo "8b. contention marks transient (push mode); gate contention does NOT"; reset; mkdir -p "$STATE/lock-repo"; STUB_TRIAGE="PLAY_PASS" run
  ckfile "$TMARKER" "push-mode contention writes the scoped transient marker"
  reset; mkdir -p "$STATE/lock-repo"; STUB_TRIAGE="PLAY_PASS" gate_run >/dev/null 2>&1 || true
  cknofile "$TMARKER" "gate-mode contention writes NO transient marker"
echo "9. stale lock reaped"; reset; mkdir -p "$STATE/lock-repo"; touch -t 202001010000 "$STATE/lock-repo"; STUB_TRIAGE="PLAY_PASS" run; ck "reaps stale lock + reviews" "review PASS"
echo "9b. raised PLAY_REVIEW_CALL_TIMEOUT raises the stale floor (77-min lock is LIVE, not reaped)"; reset; mkdir -p "$STATE/lock-repo"
  touch -t "$(date -v-77M +%Y%m%d%H%M.%S)" "$STATE/lock-repo"   # 4620s old: > default 4500 STALE, < the raised floor (3*180+3*1500+360=5400)
  env HOME="$FAKE" PLAY_REVIEW_CALL_TIMEOUT=1500 STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "" 2>/dev/null
  ck "1500s call timeout -> 77-min lock survives (skipped, not reaped)" "review SKIPPED"
  STUB_TRIAGE="PLAY_PASS" run; ck "same lock IS reaped under the default 4500s budget" "review PASS"
echo "8c. oracle fast-skip: a dead oracle is skipped in SECONDS (reach-check), review proceeds on kilabz"; reset
  STUB_CANARY_FAIL=oracle STUB_TRIAGE="PLAY_PASS" run; ck "review PASS on kilabz alone" "review PASS"
  rj="$(ls -t "$RUNS"/*/play.jsonl 2>/dev/null | head -1)"
  if grep -q "oracle-skipped-fast" "$rj" 2>/dev/null; then echo "  ok: fast-skip path taken (reach-check, not the 1200s wait)"; PASS=$((PASS+1)); else echo "  FAIL: oracle-skipped-fast not recorded"; FAIL=$((FAIL+1)); fi
  if grep -q $'^oracle\t1200\t' "$FAKE/.myndaix/mxr-argv.log" 2>/dev/null; then echo "  FAIL: full oracle review call still fired"; FAIL=$((FAIL+1)); else echo "  ok: no 1200s oracle review call after a failed reach-check"; PASS=$((PASS+1)); fi
echo "9c. margin is ENFORCED for explicit PLAY_STALE + leading-zero RCT is base-10"; reset; mkdir -p "$STATE/lock-repo"
  touch -t "$(date -v-85M +%Y%m%d%H%M.%S)" "$STATE/lock-repo"   # 5100s old: > floor-sans-margin 5040, < enforced floor 5400
  env HOME="$FAKE" PLAY_REVIEW_CALL_TIMEOUT=1500 PLAY_STALE=5040 STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "" 2>/dev/null
  ck "PLAY_STALE inside the margin window is rejected (lock survives)" "review SKIPPED"
  reset; mkdir -p "$STATE/lock-repo"; touch -t "$(date -v-85M +%Y%m%d%H%M.%S)" "$STATE/lock-repo"
  env HOME="$FAKE" PLAY_REVIEW_CALL_TIMEOUT=01500 STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "" 2>/dev/null
  ck "RCT '01500' is base-10 (floor 5400, not octal 3036 -> lock survives)" "review SKIPPED"
echo "10. embedded-whitespace token is NOT a pass"; reset; STUB_TRIAGE="P L A Y _ P A S S" run; ck "spaced token -> NEEDS-FIX" "review NEEDS-FIX"
echo "11. unconfirmed push is NOT deduped"; reset; STUB_TRIAGE="PLAY_PASS" run "/tmp/no-such-remote-$$"; ck "still delivers PASS" "review PASS"; cknofile "$DMARKER" "unconfirmed push not marked done"
echo "12. delivery failure is NOT deduped"; reset; mkdir -p "$INBOX"; chmod 000 "$INBOX"; STUB_TRIAGE="PLAY_PASS" run; chmod 755 "$INBOX"; cknofile "$DMARKER" "lost delivery not marked done"
echo "13. empty PLAY_IMESSAGE_TO disables the ping"; reset; PLAY_IMESSAGE_TO="" STUB_TRIAGE="PLAY_PASS" run; ck "still delivers PASS" "review PASS"; cknofile "$FAKE/.myndaix/osascript-calls" "no iMessage send when disabled"
echo "14. same SHA on a DIFFERENT ref does not confirm"; reset; bare="$ROOT/bare.git"; git init -q --bare "$bare"; git -C "$REPO" push -q "$bare" "$TIP:refs/heads/other" 2>/dev/null; STUB_TRIAGE="PLAY_PASS" run "$bare"; cknofile "$DMARKER" "tip on wrong ref not deduped"
echo "15. SHA on the TARGET ref confirms"; reset; bare2="$ROOT/bare2.git"; git init -q --bare "$bare2"; git -C "$REPO" push -q "$bare2" "$TIP:refs/heads/main" 2>/dev/null; STUB_TRIAGE="PLAY_PASS" run "$bare2"; ckfile "$DMARKER" "tip on target ref deduped"

echo "16. PR-0a: scope flags forwarded to mxr (repo bucket + reviewed SHA)"; reset; STUB_TRIAGE="PLAY_PASS" run
  rid="$(basename "$REPO")"; log="$FAKE/.myndaix/mxr-argv.log"
  nscoped="$(grep -c -- "--repo $rid --base-ref $TIP" "$log" 2>/dev/null || true)"; [[ "$nscoped" =~ ^[0-9]+$ ]] || nscoped=0
  if [[ "$nscoped" -eq 3 ]]; then echo "  ok: kilabz+oracle reviews + triage carry --repo + --base-ref"; PASS=$((PASS+1)); else echo "  FAIL: want 3 scoped mxr calls (kilabz+oracle+lobster), got $nscoped"; FAIL=$((FAIL+1)); fi
  if grep -q "READY.*--repo" "$log" 2>/dev/null; then echo "  FAIL: canary must stay cap-exempt"; FAIL=$((FAIL+1)); else echo "  ok: canary cap-exempt (no --repo)"; PASS=$((PASS+1)); fi

echo "16b. +learning Step 4: hint injected into BOTH reviews only (not triage); skipped under gate"; reset
  STUB_ARMED="$(printf '===BEGIN UNTRUSTED armed-skill nonce=z===\nARMEDHINT review-skill body\n===END UNTRUSTED nonce=z===')" STUB_TRIAGE="PLAY_PASS" run
  alog="$FAKE/.myndaix/mxr-argv.log"
  nhit="$(grep -c "ARMEDHINT" "$alog" 2>/dev/null || true)"; [[ "$nhit" =~ ^[0-9]+$ ]] || nhit=0
  if [[ "$nhit" -eq 2 ]]; then echo "  ok: hint reaches exactly the kilabz + oracle prompts (not triage)"; PASS=$((PASS+1)); else echo "  FAIL: want hint in 2 review prompts, got $nhit (triage leak = 3)"; FAIL=$((FAIL+1)); fi
  reset; STUB_ARMED="ARMEDGATE" gate_run >/dev/null 2>&1 || true
  if grep -q "ARMEDGATE" "$FAKE/.myndaix/mxr-argv.log" 2>/dev/null; then echo "  FAIL: hint injected into the MERGE GATE (v0.3 §2 violation)"; FAIL=$((FAIL+1)); else echo "  ok: gate mode injects NO hint (the ! gate skip holds)"; PASS=$((PASS+1)); fi

echo "16c. review-call mxr timeout: push reviews wait 1200, canary stays fast, gate stays 180"; reset; STUB_TRIAGE="PLAY_PASS" run
  tlog="$FAKE/.myndaix/mxr-argv.log"
  if grep -q $'^kilabz\t1200\t' "$tlog" && grep -q $'^oracle\t1200\t' "$tlog" && grep -q $'^lobster\t1200\t' "$tlog"; then
    echo "  ok: all 3 push review calls wait 1200s (covers one full kilabz 900s attempt)"; PASS=$((PASS+1)); else echo "  FAIL: push review calls not bumped to 1200"; FAIL=$((FAIL+1)); fi
  if grep -q $'^kilabz\t180\t' "$tlog" && ! grep -q $'^kilabz\tunset\t' "$tlog"; then echo "  ok: canary EXPLICITLY clamped to 180 (no ambient MXR_TIMEOUT_S inherit)"; PASS=$((PASS+1)); else echo "  FAIL: canary not clamped to 180"; FAIL=$((FAIL+1)); fi
  reset; gate_run >/dev/null 2>&1 || true; glog="$FAKE/.myndaix/mxr-argv.log"
  if grep -q $'^kilabz\t180\t' "$glog" && ! grep -q $'^kilabz\t1200\t' "$glog"; then
    echo "  ok: gate review calls stay 180 (fit automerge total budget)"; PASS=$((PASS+1)); else echo "  FAIL: gate review-call timeout wrong"; FAIL=$((FAIL+1)); fi

echo "17. PR-1a: front re-execs the FIXED installed worker, not the worktree copy"; reset
  mkdir -p "$FAKE/.myndaix/orchestrator"
  fixed="$FAKE/.myndaix/orchestrator/play-review.sh"
  printf '%s\n' '#!/usr/bin/env bash' 'mkdir -p "$HOME/.myndaix" 2>/dev/null' \
    'printf "%s" "$0" > "$HOME/.myndaix/which-self"' 'exit 0' > "$fixed"
  chmod +x "$fixed"
  ( cd "$REPO" && printf '%s %s %s %s\n' refs/heads/main "$TIP" refs/heads/main \
      0000000000000000000000000000000000000000 | env HOME="$FAKE" bash "$SCRIPT" origin "" ) >/dev/null 2>&1
  for _ in $(seq 1 30); do [[ -f "$FAKE/.myndaix/which-self" ]] && break; sleep 0.1; done
  if [[ -f "$FAKE/.myndaix/which-self" ]] && grep -q "/.myndaix/orchestrator/play-review.sh" "$FAKE/.myndaix/which-self"; then
    echo "  ok: worker re-exec'd the fixed install path"; PASS=$((PASS+1))
  else echo "  FAIL: worker did not re-exec the fixed path"; FAIL=$((FAIL+1)); fi

# ====================== PLAY_AUTOFIX flip (autonomous-fix trigger) ======================
echo "18. autofix fires with base_sha=TIP and exactly 3 args"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run_af
  if wait_fixer; then
    nargs="$(sed -n 1p "$FAKE/.myndaix/fixer-argv")"; a2="$(sed -n 3p "$FAKE/.myndaix/fixer-argv")"
    [[ "$nargs" == "3" ]] && { echo "  ok: exactly 3 args"; PASS=$((PASS+1)); } || { echo "  FAIL: argc=$nargs"; FAIL=$((FAIL+1)); }
    [[ "$a2" == "$TIP" ]] && { echo "  ok: arg2 == tip"; PASS=$((PASS+1)); } || { echo "  FAIL: arg2=$a2 want $TIP"; FAIL=$((FAIL+1)); }
    [[ "$a2" != "$EMPTY" ]] && { echo "  ok: arg2 != base"; PASS=$((PASS+1)); } || { echo "  FAIL: arg2 is base"; FAIL=$((FAIL+1)); }
  else echo "  FAIL: fixer never fired"; FAIL=$((FAIL+3)); fi
echo "19. autofix fires at most once per tip"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run_af; wait_fixer; STUB_TRIAGE="1. fix it" run_af; settle
  ncalls="$(wc -c < "$FAKE/.myndaix/fixer-calls" 2>/dev/null | tr -d ' ')"; [[ "$ncalls" =~ ^[0-9]+$ ]] || ncalls=0
  [[ "$ncalls" == "1" ]] && { echo "  ok: fired once across 2 same-tip runs"; PASS=$((PASS+1)); } || { echo "  FAIL: fired $ncalls times"; FAIL=$((FAIL+1)); }
echo "20. no fire when push unconfirmed"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run_af "/tmp/no-such-remote-$$"; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "unconfirmed push -> no auto-fire"
echo "21. no fire when fail_to_pass non-null"; reset; af_repos "$(printf '{"%s":{"path":"%s","fail_to_pass":["x"]}}' "$(basename "$REPO")" "$REPO")"; STUB_TRIAGE="1. fix it" run_af; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "static fail_to_pass -> no auto-fire"
echo "22. no fire when repo key missing"; reset; af_repos '{}'; STUB_TRIAGE="1. fix it" run_af; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "untracked repo -> no auto-fire"
echo "23. no fire with no trusted install"; reset; af_repos "$NULLCFG"
  env HOME="$FAKE" PLAY_AUTOFIX=1 STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "no \$ORCH/play-fix.sh -> no auto-fire"
echo "24. reject a fixer resolving under the repo"; reset; af_repos "$NULLCFG"
  inrepo="$REPO/evil-play-fix.sh"; printf '%s\n' '#!/usr/bin/env bash' 'printf x >> "$HOME/.myndaix/fixer-calls"' 'exit 0' > "$inrepo"; chmod +x "$inrepo"
  env HOME="$FAKE" PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$inrepo" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-calls" "in-repo fixer rejected -> no fire"; rm -f "$inrepo"
echo "25. PASS branch never auto-fires nor writes fixlist"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="PLAY_PASS" run_af; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "PASS -> no auto-fire"
  if ls "$RUNS"/*/fixlist.txt >/dev/null 2>&1; then echo "  FAIL: PASS wrote fixlist.txt"; FAIL=$((FAIL+1)); else echo "  ok: PASS wrote no fixlist"; PASS=$((PASS+1)); fi
echo "26. armed-but-suppressed still delivers the manual hint"; reset; af_repos "$(printf '{"%s":{"path":"%s","fail_to_pass":["x"]}}' "$(basename "$REPO")" "$REPO")"; STUB_TRIAGE="1. fix it" run_af; settle
  ck "manual hint present despite suppressed fire" "to fix: play-fix.sh"
  cknofile "$FAKE/.myndaix/fixer-argv" "suppressed -> no fire"
echo "27. PLAY_AUTOFIX unset -> no fire, hint present"; reset; af_repos "$NULLCFG"; STUB_TRIAGE="1. fix it" run; settle
  ck "manual hint present" "to fix: play-fix.sh"; cknofile "$FAKE/.myndaix/fixer-argv" "not armed -> no fire"
echo "29. reject a SYMLINKED fixer (codex BLOCKER: symlink -> in-repo copy)"; reset; af_repos "$NULLCFG"
  ln -sf "$FIXER" "$ROOT/link-fixer.sh"
  env HOME="$FAKE" PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$ROOT/link-fixer.sh" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "symlinked fixer rejected -> no fire"; rm -f "$ROOT/link-fixer.sh"
# (case 28 REMOVED: the "play-fix.sh byte-identical to origin/main (frozen)" tripwire was a
#  point-in-time assertion from the autofix-FLIP PR (21c4605) proving THAT change touched only
#  play-review.sh. play-fix is under active development now — the apply rung (#133) and this
#  findings-fold both change it deliberately — so the freeze is obsolete and fires falsely on any
#  legit play-fix edit. It was never a CI gate (CI runs pytest + substrate/test.sh + bats only).)

echo "30. durable flag-file enables auto-fire WITHOUT PLAY_AUTOFIX env"; reset; af_repos "$NULLCFG"
  mkdir -p "$FAKE/.myndaix/orchestrator"; : > "$FAKE/.myndaix/orchestrator/AUTOFIX_ENABLED"
  env HOME="$FAKE" PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$FIXER" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null
  if wait_fixer; then echo "  ok: flag-file armed -> fired"; PASS=$((PASS+1)); else echo "  FAIL: flag-file did not arm"; FAIL=$((FAIL+1)); fi
  a2="$(sed -n 3p "$FAKE/.myndaix/fixer-argv" 2>/dev/null)"; [[ "$a2" == "$TIP" ]] && { echo "  ok: flag-file fire uses base=tip"; PASS=$((PASS+1)); } || { echo "  FAIL: flag-file fire arg2=$a2"; FAIL=$((FAIL+1)); }

echo "31. PLAY_DISABLE_AUTOFIX=1 HARD-overrides the durable flag (controller-loop, codex BLOCKER)"; reset; af_repos "$NULLCFG"
  mkdir -p "$FAKE/.myndaix/orchestrator"; : > "$FAKE/.myndaix/orchestrator/AUTOFIX_ENABLED"
  env HOME="$FAKE" PLAY_DISABLE_AUTOFIX=1 PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$FIXER" STUB_TRIAGE="1. fix it" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "disable flag suppresses fire even with durable flag + PLAY_AUTOFIX"

echo "32. GATE PASS -> structured verdict PASS, exit 0, no inbox/done (automerge gate)"; reset; rm -f "$ROOT/verdict.json"
  STUB_TRIAGE="PLAY_PASS" gate_run; ckexit $? 0 "gate PASS exits 0"
  ck "verdict says PASS" '"verdict":"PASS"' "$ROOT/verdict.json"
  ck "run_id+head threaded into verdict" '"run_id":"run123"' "$ROOT/verdict.json"
  cknofile "$DMARKER" "gate writes NO done marker"
  if [[ -z "$(ls "$INBOX" 2>/dev/null)" ]]; then echo "  ok: gate delivers nothing to inbox"; PASS=$((PASS+1)); else echo "  FAIL: gate delivered to inbox"; FAIL=$((FAIL+1)); fi
echo "33. GATE NEEDS-FIX -> verdict NEEDS-FIX, exit 1"; reset; rm -f "$ROOT/verdict.json"
  STUB_TRIAGE="1. fix the subtraction" gate_run; ckexit $? 1 "gate NEEDS-FIX exits 1"
  ck "verdict says NEEDS-FIX" '"verdict":"NEEDS-FIX"' "$ROOT/verdict.json"
echo "34. GATE requires Oracle -> oracle-down is TRANSIENT (verdict ABORTED, exit 2 -> retry, NOT a permanent NEEDS-FIX)"; reset; rm -f "$ROOT/verdict.json"
  STUB_CANARY_FAIL=oracle STUB_TRIAGE="PLAY_PASS" gate_run; ckexit $? 2 "oracle-down under gate exits 2 (transient)"
  ck "transient verdict (distinct from a real NEEDS-FIX)" '"verdict":"ABORTED"' "$ROOT/verdict.json"

# ====================== outcomes-ledger instrumentation (PR-B) ======================
# OUTCOMES_ENABLED is its OWN flag (no CAPTURE coupling). Default OFF -> no finding: prompt, no
# record. On -> finding: sentence in BOTH review prompts + outcome-record fires post-delivery. Gate
# mode HARD-skips. The follow-up keys file appears ONLY when outcome-record surfaces keys.
ORCHDIR="$FAKE/.myndaix/orchestrator"
arm_outcomes(){ mkdir -p "$ORCHDIR"; : > "$ORCHDIR/OUTCOMES_ENABLED"; }
olog(){ echo "$FAKE/.myndaix/outcome-argv.log"; }
latest_outcomes_file(){ ls -t "$INBOX"/*-outcomes.md 2>/dev/null | head -1; }

echo "35. OUTCOMES default OFF: no finding: prompt, no outcome-record call"; reset; STUB_TRIAGE="1. fix it" run
  mlog="$FAKE/.myndaix/mxr-argv.log"
  if grep -q "finding:<tag>" "$mlog" 2>/dev/null; then echo "  FAIL: finding: prompt emitted while OFF"; FAIL=$((FAIL+1)); else echo "  ok: no finding: prompt (default OFF)"; PASS=$((PASS+1)); fi
  cknofile "$(olog)" "no outcome-record call when OUTCOMES_ENABLED absent"

echo "36. OUTCOMES ON: finding: sentence in BOTH reviews only (not triage) + record fires on NEEDS-FIX"; reset; arm_outcomes; STUB_TRIAGE="1. fix it" run
  mlog="$FAKE/.myndaix/mxr-argv.log"
  nfind="$(grep -c "finding:<tag>" "$mlog" 2>/dev/null || true)"; [[ "$nfind" =~ ^[0-9]+$ ]] || nfind=0
  if [[ "$nfind" -eq 2 ]]; then echo "  ok: finding: sentence reaches exactly kilabz + oracle (not triage)"; PASS=$((PASS+1)); else echo "  FAIL: want finding: in 2 review prompts, got $nfind"; FAIL=$((FAIL+1)); fi
  ckfile "$(olog)" "outcome-record fired post-delivery on NEEDS-FIX"

echo "37. OUTCOMES ON + PASS branch: CLOSE phase still records (runs on a clean PASS)"; reset; arm_outcomes; STUB_TRIAGE="PLAY_PASS" run
  ckfile "$(olog)" "outcome-record fires on a clean PASS too (CLOSE-on-PASS)"

echo "38. gate mode HARD-skips outcomes (no finding: prompt, no record)"; reset; arm_outcomes; STUB_TRIAGE="PLAY_PASS" gate_run >/dev/null 2>&1 || true
  glog="$FAKE/.myndaix/mxr-argv.log"
  if grep -q "finding:<tag>" "$glog" 2>/dev/null; then echo "  FAIL: finding: prompt injected into the merge GATE"; FAIL=$((FAIL+1)); else echo "  ok: gate mode injects NO finding: prompt"; PASS=$((PASS+1)); fi
  cknofile "$(olog)" "gate mode fires NO outcome-record"

echo "39. follow-up keys file written ONLY when keys recorded"; reset; arm_outcomes
  STUB_OUTCOME_KEYS="$(printf 'deadbeef0123\tkilabz\tfail-open\tsrc/a.py')" STUB_TRIAGE="1. fix it" run
  kf="$(latest_outcomes_file)"
  ckfile "$kf" "keys file written when outcome-record surfaced a key"
  ck "keys file lists the paste-ready dismiss command" "mxr outcome deadbeef0123 fp" "$kf"
  ck "keys file lists the paste-ready confirm command (PR-A)" "mxr outcome deadbeef0123 real" "$kf"
  ck "keys file carries the batch hint (PR-A)" "mxr outcome real deadbeef0123" "$kf"
  ck "keys file lists the finding line" "finding:fail-open @ src/a.py" "$kf"

echo "40. NO follow-up keys file when nothing recorded (empty outcome-record output)"; reset; arm_outcomes
  STUB_TRIAGE="1. fix it" run   # STUB_OUTCOME_KEYS unset -> stub emits nothing -> no keys file
  if ls "$INBOX"/*-outcomes.md >/dev/null 2>&1; then echo "  FAIL: keys file written with no recorded keys"; FAIL=$((FAIL+1)); else echo "  ok: no keys file when nothing recorded"; PASS=$((PASS+1)); fi

echo "41. keys file does NOT touch the verdict file (verdict untouched)"; reset; arm_outcomes
  STUB_OUTCOME_KEYS="$(printf 'cafebabe4567\toracle\ttoctou-race\tf.py')" STUB_TRIAGE="1. fix it" run
  # shellcheck disable=SC2010  # ls -t sorts by mtime (a glob can't); test-only newest-match
  vf="$(ls -t "$INBOX"/*.md 2>/dev/null | grep -v -- '-outcomes.md' | head -1)"
  if [[ -n "$vf" ]] && ! grep -q "cafebabe4567" "$vf"; then echo "  ok: verdict file has no injected keys (separate file)"; PASS=$((PASS+1)); else echo "  FAIL: keys leaked into the verdict file"; FAIL=$((FAIL+1)); fi

# ====================== PR-2: review-context snapshot staging ======================
# play-review stages a de-linked read-only snapshot of the reviewed tip as the CONFINED
# reviewers' cwd (kilabz + lobster; oracle inline-only, D5). Push mode degrades LOUDLY on a
# staging failure; gate mode fails CLOSED. Teardown on the terminal path; reaper on leaks.
mlog(){ echo "$FAKE/.myndaix/mxr-argv.log"; }

echo "42. PR-2: --staged-workdir reaches kilabz + lobster (2), NOT oracle"; reset; STUB_TRIAGE="PLAY_PASS" run
  # the argv (with its multi-line prompt) logs as one multi-line entry; the trailing flags land
  # on the SAME physical line as --repo/--base-ref (like test 16). So count the flag-tail lines:
  # kilabz + lobster carry `--base-ref $TIP --staged-workdir`; oracle carries --base-ref WITHOUT it.
  L="$(mlog)"
  ntotal="$(grep -c -- "--base-ref $TIP" "$L" 2>/dev/null || true)"; [[ "$ntotal" =~ ^[0-9]+$ ]] || ntotal=0
  nstaged="$(grep -c -- "--base-ref $TIP --staged-workdir" "$L" 2>/dev/null || true)"; [[ "$nstaged" =~ ^[0-9]+$ ]] || nstaged=0
  [[ "$nstaged" -eq 2 ]] && { echo "  ok: exactly kilabz + lobster carry --staged-workdir"; PASS=$((PASS+1)); } || { echo "  FAIL: staged review calls = $nstaged (want 2)"; FAIL=$((FAIL+1)); }
  [[ "$ntotal" -eq 3 && $((ntotal - nstaged)) -eq 1 ]] && { echo "  ok: oracle stays inline-only (1 review call has no staged-workdir, D5)"; PASS=$((PASS+1)); } || { echo "  FAIL: inline (oracle) review calls = $((ntotal - nstaged)) (want 1)"; FAIL=$((FAIL+1)); }

echo "43. PR-2: snapshot_intro (trusted sentence) reaches the staged prompts, references the tip"; reset; STUB_TRIAGE="PLAY_PASS" run
  L="$(mlog)"
  if grep -q "de-linked, non-writable snapshot of this repo at the reviewed tip $TIP" "$L" 2>/dev/null; then echo "  ok: snapshot intro references the reviewed tip"; PASS=$((PASS+1)); else echo "  FAIL: snapshot intro absent/wrong"; FAIL=$((FAIL+1)); fi

echo "44. PR-2: review-stage called once; review-teardown fires on the terminal success path"; reset; STUB_TRIAGE="PLAY_PASS" run
  L="$(mlog)"
  nstage="$(grep -c $'^review-stage\t' "$L" 2>/dev/null || true)"; [[ "$nstage" =~ ^[0-9]+$ ]] || nstage=0
  [[ "$nstage" -eq 1 ]] && { echo "  ok: review-stage called exactly once"; PASS=$((PASS+1)); } || { echo "  FAIL: review-stage called $nstage times"; FAIL=$((FAIL+1)); }
  ckfile "$FAKE/.myndaix/teardown-argv.log" "review-teardown fired on the terminal (post-triage) path"

echo "45. PR-2: leaked-snapshot reaper runs at worker startup"; reset; STUB_TRIAGE="PLAY_PASS" run
  ckfile "$FAKE/.myndaix/reap-calls" "review-reap invoked at startup"

echo "46. PR-2: staging FAILURE degrades a PUSH review LOUDLY (still delivers, header marks it)"; reset
  STUB_STAGE_FAIL=1 STUB_TRIAGE="1. fix it" run
  ck "push review still delivers despite staging failure" "review NEEDS-FIX"
  ck "verdict header carries the degradation marker" "reviewed WITHOUT snapshot"
  L="$(mlog)"
  if grep -q $'^kilabz\t.*--staged-workdir' "$L" 2>/dev/null; then echo "  FAIL: staged-workdir passed after a staging failure"; FAIL=$((FAIL+1)); else echo "  ok: no staged-workdir after a staging failure (inline-only)"; PASS=$((PASS+1)); fi

echo "46b. PR-2: degradation reason is control-stripped (ESC from the stub reason never lands)"; reset
  STUB_STAGE_FAIL=1 STUB_TRIAGE="PLAY_PASS" run
  df="$(latest)"
  if [[ -n "$df" ]] && LC_ALL=C grep -q $'\033' "$df" 2>/dev/null; then echo "  FAIL: ESC from the staging reason survived into the verdict"; FAIL=$((FAIL+1)); else echo "  ok: ESC stripped from the degradation reason"; PASS=$((PASS+1)); fi

echo "47. PR-2: staging FAILURE fails the GATE CLOSED (ABORTED, exit 2 -> retry)"; reset; rm -f "$ROOT/verdict.json"
  STUB_STAGE_FAIL=1 STUB_TRIAGE="PLAY_PASS" gate_run; ckexit $? 2 "gate staging-fail exits 2 (transient)"
  ck "gate verdict ABORTED on staging failure" '"verdict":"ABORTED"' "$ROOT/verdict.json"

echo "47b. PR-2 (kilabz HIGH): review-stage that PRINTS a path but EXITS NON-ZERO still fails closed"; reset; rm -f "$ROOT/verdict.json"
  STUB_STAGE_FAIL_WITH_PATH=1 STUB_TRIAGE="PLAY_PASS" gate_run; ckexit $? 2 "gate keys on exit status, not stdout shape (exit 2)"
  ck "verdict ABORTED despite a printed staging path" '"verdict":"ABORTED"' "$ROOT/verdict.json"
  reset; STUB_STAGE_FAIL_WITH_PATH=1 STUB_TRIAGE="PLAY_PASS" run
  L="$(mlog)"
  if grep -q $'^kilabz\t.*--staged-workdir' "$L" 2>/dev/null; then echo "  FAIL: staged-workdir passed after a nonzero-exit stage"; FAIL=$((FAIL+1)); else echo "  ok: push review degrades (no staged-workdir) on nonzero-exit stage"; PASS=$((PASS+1)); fi
  ck "push verdict marks the degradation" "reviewed WITHOUT snapshot"

echo "48. PR-2: scope-flag count is still 3 (staged-workdir is additive, not a new scoped call)"; reset; STUB_TRIAGE="PLAY_PASS" run
  rid="$(basename "$REPO")"; L="$(mlog)"
  nscoped="$(grep -c -- "--repo $rid --base-ref $TIP" "$L" 2>/dev/null || true)"; [[ "$nscoped" =~ ^[0-9]+$ ]] || nscoped=0
  [[ "$nscoped" -eq 3 ]] && { echo "  ok: still exactly 3 scoped review calls"; PASS=$((PASS+1)); } || { echo "  FAIL: scoped calls = $nscoped (want 3)"; FAIL=$((FAIL+1)); }


# ====================== skip-range fold-in (skipped-marker adoption) ======================
# The FRONT walk adopts a recorded skip-base so a contention-skipped range folds into the
# next hook push. Observed via an argv RECORDER at the FIXED install path (the same seam
# test 17 proves: FRONT prefers $ORCH/play-review.sh over the worktree copy).
SKIPSLUG="repo-refs-heads-main"   # hardcoded bash<->front contract, same as TMARKER/DMARKER
FRONT_ARGV="$FAKE/.myndaix/front-worker-argv"
install_front_recorder(){ mkdir -p "$FAKE/.myndaix/orchestrator"
  printf '%s\n' '#!/usr/bin/env bash' 'mkdir -p "$HOME/.myndaix" 2>/dev/null' \
    'printf "%s\n" "$@" > "$HOME/.myndaix/front-worker-argv"' 'exit 0' \
    > "$FAKE/.myndaix/orchestrator/play-review.sh"
  chmod +x "$FAKE/.myndaix/orchestrator/play-review.sh"; }
front_push(){ # front_push <localsha> <remotesha> — one pre-push stdin line into FRONT (hook shape:
  # NON-empty remote_url — the walk is gated on it), wait for the recorder artifact
  rm -f "$FRONT_ARGV"
  ( cd "$REPO" && printf '%s %s %s %s\n' refs/heads/main "$1" refs/heads/main "$2" \
      | env HOME="$FAKE" bash "$SCRIPT" origin "stub://remote" ) >/dev/null 2>&1
  local _; for _ in $(seq 1 30); do [[ -f "$FRONT_ARGV" ]] && return 0; sleep 0.1; done; return 1; }
front_base(){ sed -n 3p "$FRONT_ARGV" 2>/dev/null; }   # recorder argv: --worker repo BASE tip ref url orig

# fixture commits: TIP -> TIP2 (the skipped push's tip) -> TIP3 (the retrigger push)
git -C "$REPO" commit -q --allow-empty -m skipped-tip; TIP2="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" commit -q --allow-empty -m retrigger;  TIP3="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" reset -q --hard "$TIP"   # restore; objects stay reachable (same trick as 7d)

echo "49. FRONT folds a skipped range: remotesha == skipped tip -> worker gets the recorded base"; reset; install_front_recorder
  mkdir -p "$STATE"; printf '%s' "$TIP" > "$STATE/skipped-$SKIPSLUG-$TIP2"
  if front_push "$TIP3" "$TIP2"; then
    b="$(front_base)"
    if [[ "$b" == "$TIP" ]]; then echo "  ok: worker dispatched with the recorded base (range folded in)"; PASS=$((PASS+1)); else echo "  FAIL: base=$b want $TIP"; FAIL=$((FAIL+1)); fi
    if [[ "$(sed -n 4p "$FRONT_ARGV")" == "$TIP3" ]]; then echo "  ok: tip stays the pushed localsha"; PASS=$((PASS+1)); else echo "  FAIL: tip mangled"; FAIL=$((FAIL+1)); fi
    if [[ "$(sed -n 7p "$FRONT_ARGV")" == "$TIP2" ]]; then echo "  ok: arg7 carries the push's own base (over-cap fallback)"; PASS=$((PASS+1)); else echo "  FAIL: arg7=$(sed -n 7p "$FRONT_ARGV") want $TIP2"; FAIL=$((FAIL+1)); fi
  else echo "  FAIL: front never dispatched a worker"; FAIL=$((FAIL+3)); fi

echo "49b. chain fold (skip-of-skip walks 2 hops) + a reviewed sha (marker consumed) stops the walk"; reset; install_front_recorder
  mkdir -p "$STATE"; printf '%s' "$TIP" > "$STATE/skipped-$SKIPSLUG-$TIP2"; printf '%s' "$TIP2" > "$STATE/skipped-$SKIPSLUG-$TIP3"
  front_push "$TIP3" "$TIP3" >/dev/null 2>&1 || true   # remotesha=TIP3 (its marker points to TIP2, whose marker points to TIP)
  b="$(front_base)"
  if [[ "$b" == "$TIP" ]]; then echo "  ok: 2-hop chain walked to the deepest recorded base"; PASS=$((PASS+1)); else echo "  FAIL: chain base=$b want $TIP"; FAIL=$((FAIL+1)); fi
  # TIP2 got reviewed after all: mark_done writes done- AND consumes its skip marker — the
  # walk keys on marker PRESENCE (done+skipped coexist only on a deliberate backlog re-queue).
  : > "$STATE/done-$SKIPSLUG-$TIP2"; rm -f "$STATE/skipped-$SKIPSLUG-$TIP2"
  front_push "$TIP3" "$TIP3" >/dev/null 2>&1 || true
  b="$(front_base)"
  if [[ "$b" == "$TIP2" ]]; then echo "  ok: reviewed sha (no marker) stops the walk"; PASS=$((PASS+1)); else echo "  FAIL: reviewed-stop base=$b want $TIP2"; FAIL=$((FAIL+1)); fi

echo "50. fail-safe: bad markers fall back to base=remotesha (and the push is never aborted)"; reset; install_front_recorder; mkdir -p "$STATE"
  printf 'not-a-sha\n' > "$STATE/skipped-$SKIPSLUG-$TIP2"
  front_push "$TIP3" "$TIP2" || true
  if [[ "$(front_base)" == "$TIP2" ]]; then echo "  ok: garbage content -> base=remotesha"; PASS=$((PASS+1)); else echo "  FAIL: garbage content adopted ($(front_base))"; FAIL=$((FAIL+1)); fi
  SIDETREE="$(git -C "$REPO" rev-parse "$TIP^{tree}")"
  SIDE="$(git -C "$REPO" commit-tree -p "$TIP" -m side "$SIDETREE" 2>/dev/null)"   # real commit, NOT an ancestor of TIP3 (force-push shape)
  printf '%s' "$SIDE" > "$STATE/skipped-$SKIPSLUG-$TIP2"
  front_push "$TIP3" "$TIP2" || true
  if [[ "$(front_base)" == "$TIP2" ]]; then echo "  ok: non-ancestor recorded base -> base=remotesha"; PASS=$((PASS+1)); else echo "  FAIL: non-ancestor base adopted"; FAIL=$((FAIL+1)); fi
  printf '%040d' 1 > "$STATE/skipped-$SKIPSLUG-$TIP2"                              # 40-hex, but no such object
  front_push "$TIP3" "$TIP2" || true
  if [[ "$(front_base)" == "$TIP2" ]]; then echo "  ok: missing-commit base -> base=remotesha"; PASS=$((PASS+1)); else echo "  FAIL: missing-commit base adopted"; FAIL=$((FAIL+1)); fi
  printf '%s' "$TIP3" > "$STATE/skipped-$SKIPSLUG-$TIP2"                           # marker pointing AT localsha would empty the diff
  front_push "$TIP3" "$TIP2" || true
  if [[ "$(front_base)" == "$TIP2" ]]; then echo "  ok: marker==localsha rejected (empty-diff guard)"; PASS=$((PASS+1)); else echo "  FAIL: localsha marker adopted"; FAIL=$((FAIL+1)); fi

echo "50b. controller shape (EMPTY remote_url) never walks — ledger cursor stays authoritative"; reset; install_front_recorder
  mkdir -p "$STATE"; printf '%s' "$TIP" > "$STATE/skipped-$SKIPSLUG-$TIP2"
  rm -f "$FRONT_ARGV"
  ( cd "$REPO" && printf '%s %s %s %s\n' refs/heads/main "$TIP3" refs/heads/main "$TIP2" \
      | env HOME="$FAKE" bash "$SCRIPT" origin "" ) >/dev/null 2>&1
  for _ in $(seq 1 30); do [[ -f "$FRONT_ARGV" ]] && break; sleep 0.1; done
  if [[ "$(front_base)" == "$TIP2" ]]; then echo "  ok: empty remote_url -> no walk (base=remotesha)"; PASS=$((PASS+1)); else echo "  FAIL: controller-shape dispatch walked ($(front_base))"; FAIL=$((FAIL+1)); fi

# ============ new-branch trunk resolution (the stale-local-main range blowup) ============
# A new branch's first push carries remotesha=ZERO, so the base is merge-base(trunk, localsha).
# That trunk must be the REMOTE-tracking ref: a local `main` left behind by a GitHub merge drags
# the range back over every already-merged commit. 2026-09-14: local main sat 29 commits behind,
# so a 42-line branch computed as 3625 lines and ABORTED on MAX_DIFF_LINES — while also being a
# re-review of already-outcome-labeled code. Fixture: local main STALE at $TIP, remote trunk at
# $TRUNKTIP, feature branch at $FEATTIP (descends from both).
ZERO40=0000000000000000000000000000000000000000
# REAL content (not --allow-empty): 50h/50i need a diff that can actually breach the line cap —
# an empty diff aborts on a different branch and would stop testing the cap path.
printf 'trunk\n' > "$REPO/trunk.txt"; git -C "$REPO" add -A
git -C "$REPO" commit -qm trunk-advance; TRUNKTIP="$(git -C "$REPO" rev-parse HEAD)"
printf 'feat\n' > "$REPO/feat.txt";  git -C "$REPO" add -A
git -C "$REPO" commit -qm feature;    FEATTIP="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" reset -q --hard "$TIP"   # local main goes stale; objects stay reachable (same trick as 49)
# CONFIGURED remotes: a real repo pushing to `origin` has remote.origin.url set, and the FRONT now
# accepts $1 only when it resolves to a configured remote (else it treats $1 as a URL). Without
# these the cases below would exercise the unknown-destination path, not the named-remote path.
git -C "$REPO" remote add origin   stub://origin   2>/dev/null || true
git -C "$REPO" remote add upstream stub://upstream 2>/dev/null || true
front_push_new(){ # front_push_new <localsha> <remote_name> — NEW-branch push shape (remotesha=ZERO)
  rm -f "$FRONT_ARGV"
  ( cd "$REPO" && printf '%s %s %s %s\n' refs/heads/feat "$1" refs/heads/feat "$ZERO40" \
      | env HOME="$FAKE" bash "$SCRIPT" "$2" "stub://remote" ) >/dev/null 2>&1
  local _; for _ in $(seq 1 30); do [[ -f "$FRONT_ARGV" ]] && return 0; sleep 0.1; done; return 1; }

echo "50c. new branch: a STALE local main is ignored in favor of refs/remotes/origin/main"; reset; install_front_recorder
  git -C "$REPO" update-ref refs/remotes/origin/main "$TRUNKTIP"
  if front_push_new "$FEATTIP" origin; then
    b="$(front_base)"
    if [[ "$b" == "$TRUNKTIP" ]]; then echo "  ok: base = remote trunk (stale local main NOT used)"; PASS=$((PASS+1)); else echo "  FAIL: base=$b want $TRUNKTIP (stale local main is $TIP)"; FAIL=$((FAIL+1)); fi
  else echo "  FAIL: front never dispatched a worker"; FAIL=$((FAIL+1)); fi

echo "50d. the remote actually being PUSHED TO outranks origin"; reset; install_front_recorder
  git -C "$REPO" update-ref refs/remotes/upstream/main "$TIP"   # differs from origin/main ($TRUNKTIP) -> discriminating
  front_push_new "$FEATTIP" upstream || true
  if [[ "$(front_base)" == "$TIP" ]]; then echo "  ok: refs/remotes/upstream/main preferred over origin"; PASS=$((PASS+1)); else echo "  FAIL: base=$(front_base) want $TIP (the upstream trunk)"; FAIL=$((FAIL+1)); fi
  git -C "$REPO" update-ref -d refs/remotes/upstream/main

echo "50e. anonymous URL push resolves BACK to the configured remote (githooks: \$1 is the URL)"; reset; install_front_recorder
  git -C "$REPO" update-ref refs/remotes/upstream/main "$TIP"   # upstream trunk differs from origin's ($TRUNKTIP)
  front_push_new "$FEATTIP" "stub://upstream" || true           # git push <url> shape
  if [[ "$(front_base)" == "$TIP" ]]; then echo "  ok: URL mapped to upstream (not silently origin)"; PASS=$((PASS+1)); else echo "  FAIL: base=$(front_base) want $TIP — a URL fell through to origin"; FAIL=$((FAIL+1)); fi

# 50f/50g/50j all land on the SAME post-F4 contract: whenever the resolved trunk is not a
# refs/remotes/* ref, the base is EMPTY_TREE. They stay three tests because they arrive there by
# three different routes (unknown URL, no tracking ref at all, known non-origin remote), and a
# regression in any one route is invisible from the other two. Each still discriminates against
# the specific wrong answer it was written for: borrowing origin's trunk yields $TRUNKTIP, which
# is not EMPTY_TREE, so the assertion below fails exactly as loudly as it used to.
echo "50f. push to an UNCONFIGURED url never guesses origin (no silently-dropped commits)"; reset; install_front_recorder
  front_push_new "$FEATTIP" "stub://nowhere" || true            # destination is provably NOT origin
  if [[ "$(front_base)" == "$EMPTY" ]]; then echo "  ok: unknown destination -> whole-tree (over-review)"; PASS=$((PASS+1)); else echo "  FAIL: base=$(front_base) want EMPTY_TREE — guessed origin ($TRUNKTIP) or trusted local main ($TIP), either of which can DROP commits"; FAIL=$((FAIL+1)); fi
  git -C "$REPO" update-ref -d refs/remotes/upstream/main

# kilabz r3 F4 regression — FAILS without the fix (old code answered $TIP, the local merge-base).
# A local trunk proves NOTHING about what the destination contains. The old comment claimed the
# local fallback merely over-reviews because local main is stale; that holds only when local main
# is BEHIND the destination. When it is AHEAD — local commits not yet pushed, or another machine
# fast-forwarded it — the merge-base moves FORWARD and every commit between the destination tip and
# local main falls outside the range: a silently lost range, the one outcome this file forbids.
# Nothing visible from inside the repo distinguishes ahead from behind, so whole-tree is the only
# honest read. This fixture has local main BEHIND (the easy case) on purpose: the fix must hold
# even when the old answer would have been harmless, because the FRONT cannot tell which case it is in.
echo "50g. no remote-tracking trunk -> WHOLE-TREE, never the local ref (it may be AHEAD)"; reset; install_front_recorder
  git -C "$REPO" update-ref -d refs/remotes/origin/main
  front_push_new "$FEATTIP" origin || true
  if [[ "$(front_base)" == "$EMPTY" ]]; then echo "  ok: local-only trunk -> whole-tree"; PASS=$((PASS+1)); else echo "  FAIL: base=$(front_base) want EMPTY_TREE — trusted local main, which drops every commit the destination has that local main does not"; FAIL=$((FAIL+1)); fi

echo "50j. a KNOWN non-origin destination with no tracking ref never borrows origin's trunk"; reset; install_front_recorder
  git -C "$REPO" update-ref refs/remotes/origin/main "$TRUNKTIP"                 # origin is AHEAD of the real destination
  git -C "$REPO" update-ref -d refs/remotes/upstream/main 2>/dev/null || true    # upstream configured but never fetched
  front_push_new "$FEATTIP" upstream || true
  if [[ "$(front_base)" == "$EMPTY" ]]; then echo "  ok: whole-tree instead of guessing origin"; PASS=$((PASS+1)); else echo "  FAIL: base=$(front_base) want EMPTY_TREE — borrowed origin ($TRUNKTIP), dropping $TIP..$TRUNKTIP from the review"; FAIL=$((FAIL+1)); fi

echo "50k. a tip ALREADY contained in the trunk is skipped, not whole-tree diffed"; reset; install_front_recorder
  git -C "$REPO" update-ref refs/remotes/origin/main "$FEATTIP"                  # merge-base(trunk,tip) == tip
  if front_push_new "$FEATTIP" origin; then
    echo "  FAIL: dispatched a worker (base=$(front_base); EMPTY_TREE = whole-tree re-review of the repo)"; FAIL=$((FAIL+1))
  else echo "  ok: nothing new vs the trunk -> no review dispatched"; PASS=$((PASS+1)); fi

# kilabz r3 F5+F6 regression — FAILS against the version that PRESCRIBED a fast-forward here.
# The same ancestry signal is emitted by a stale base AND by an ordinary incremental push to the
# trunk, so the message must carry both readings and decide neither. The POSITIVE checks below (both
# readings + "cannot tell them apart") are the contract; the final cknot is a belt against a future
# edit re-adding the specific retired "LIKELY CAUSE" phrasing (kilabz r4: a dead-string needle alone
# is not the load-bearing assertion — the hedge is).
echo "50h. an over-cap abort whose range CONTAINS trunk commits reports the ambiguity, not a cause"; reset
  git -C "$REPO" update-ref refs/remotes/origin/main "$TRUNKTIP"   # trunk is an ancestor of $FEATTIP
  env HOME="$FAKE" PLAY_MAX_DIFF_LINES=0 bash "$SCRIPT" --worker "$REPO" "$TIP" "$FEATTIP" refs/heads/main "" "" 2>/dev/null
  ck "reports the containment observation" "are also contained in refs/remotes/origin/main"
  ck "offers the stale-base reading" "the base is stale"
  ck "offers the push-to-the-trunk reading" "push to the trunk itself"
  ck "admits the signal cannot decide between them" "Ancestry cannot tell them apart"
  ck "still reports what it measured" "cap 0"
  cknot "no 'LIKELY CAUSE' prescription re-added — the reader adjudicates (r3 F5+F6)" "LIKELY CAUSE"

echo "50i. a normal over-cap abort (trunk NOT inside the range) stays a plain cap message"; reset
  env HOME="$FAKE" PLAY_MAX_DIFF_LINES=0 bash "$SCRIPT" --worker "$REPO" "$TRUNKTIP" "$FEATTIP" refs/heads/main "" "" 2>/dev/null
  ck "plain cap abort still delivered" "ABORTED — diff"
  # greps the CURRENT marker text, not the retired "LIKELY CAUSE": a needle that no code path can
  # emit would make this test pass even if trunk_lag stopped firing entirely.
  cknot "no false trunk-containment alarm on a clean range" "OBSERVED:"
  git -C "$REPO" update-ref -d refs/remotes/origin/main

echo "51. over-cap fold falls back to the push's own range with a LOUD backlog banner"; reset
  seq 1 5000 > "$REPO/lines.txt"; git -C "$REPO" add -A; git -C "$REPO" commit -qm bigfold; FOLDTIP="$(git -C "$REPO" rev-parse HEAD)"   # over the line-cap default (see 7d)
  printf 'x=1\n' > "$REPO/own.py"; git -C "$REPO" add -A; git -C "$REPO" commit -qm own; OWNTIP="$(git -C "$REPO" rev-parse HEAD)"   # own range = a REAL small diff
  # worker invoked as the FRONT would after a walk: base=$EMPTY (folded, over-cap), arg7=$FOLDTIP (own base, tiny diff)
  env HOME="$FAKE" STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$OWNTIP" refs/heads/main "" "$FOLDTIP" 2>/dev/null
  ck "fallback still reviews (PASS delivered)" "review PASS"
  ck "verdict leads with the unreviewed-backlog banner" "STILL UNREVIEWED"
  ck "banner names the manual xreview command" "xreview.sh"
  BQ="$STATE/skipped-$SKIPSLUG-$OWNTIP"
  ckfile "$BQ" "backlog RE-QUEUED on the reviewed tip (not banner-only recovery)"
  if [[ "$(cat "$BQ" 2>/dev/null)" == "$FOLDTIP" ]]; then echo "  ok: re-queue content = the push's OWN base (own hop, r3 — deeper coverage lives in deeper markers)"; PASS=$((PASS+1)); else echo "  FAIL: re-queue content '$(cat "$BQ" 2>/dev/null)' want $FOLDTIP"; FAIL=$((FAIL+1)); fi
  git -C "$REPO" reset -q --hard "$TIP"   # restore

echo "51b. own range ALSO over-cap still aborts (no infinite fallback)"; reset
  env HOME="$FAKE" PLAY_MAX_DIFF=8 bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "" "$EMPTY" 2>/dev/null
  ck "own-range over-cap aborts as before" "ABORTED — diff"

echo "52. mark_done clears the tip's skipped marker (confirmed push)"; reset; mkdir -p "$STATE"
  printf '%s' "$EMPTY" > "$STATE/skipped-$SKIPSLUG-$TIP"
  STUB_TRIAGE="PLAY_PASS" run
  cknofile "$STATE/skipped-$SKIPSLUG-$TIP" "reviewed tip's skipped marker removed"

echo "53. gate contention writes NO skip marker (gate retries itself; main slug stays clean)"; reset; mkdir -p "$STATE/lock-repo"
  STUB_TRIAGE="PLAY_PASS" gate_run >/dev/null 2>&1 || true
  cknofile "$STATE/skipped-$SKIPSLUG-$TIP" "gate-mode contention leaves no skipped marker"

echo "54. worker RE-walks under the lock (kilabz TOCTOU: marker written after the FRONT walked)"; reset
  # real-content commits so the folded diff is non-empty: TIPB (skipped push), TIPC (this push)
  printf 'def add(a,b): return a+b\n' > "$REPO/m.py"; git -C "$REPO" add -A; git -C "$REPO" commit -qm fixadd; TIPB="$(git -C "$REPO" rev-parse HEAD)"
  printf 'y=2\n' > "$REPO/n.py"; git -C "$REPO" add -A; git -C "$REPO" commit -qm more; TIPC="$(git -C "$REPO" rev-parse HEAD)"
  BARE54="$ROOT/bare54.git"; git init -q --bare "$BARE54"; git -C "$REPO" push -q "$BARE54" "$TIPC:refs/heads/main" 2>/dev/null
  mkdir -p "$STATE"; printf '%s' "$TIP" > "$STATE/skipped-$SKIPSLUG-$TIPB"
  # worker gets base=TIPB (the FRONT missed the marker — it did NOT walk); the worker must re-walk
  env HOME="$FAKE" STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$TIPB" "$TIPC" refs/heads/main "$BARE54" "$TIPB" 2>/dev/null
  ck "verdict range shows the RE-walked base (marker adopted under the lock)" "range: $TIP"
  ck "still a completed review" "review PASS"
  git -C "$REPO" reset -q --hard "$TIP"   # restore

echo "55. contention with an UNWRITABLE state dir delivers the honest NOT-recorded notice"; reset; mkdir -p "$STATE/lock-repo"
  chmod a-w "$STATE"
  STUB_TRIAGE="PLAY_PASS" run
  chmod u+w "$STATE"
  ck "delivers SKIPPED" "review SKIPPED"
  ck "notice admits the range was NOT recorded" "NOT fold in automatically"
  cknofile "$STATE/skipped-$SKIPSLUG-$TIP" "no marker written when the state dir is unwritable"

# ====================== per-repo review locks (cross-repo parallelism) ======================
echo "56. a FOREIGN repo's held lock does not block this repo's review"; reset; mkdir -p "$STATE/lock-otherrepo"
  STUB_TRIAGE="PLAY_PASS" run
  ck "review proceeds under a foreign repo's lock" "review PASS"

echo "57. the LEGACY global lock dir is ignored (migration semantics; deploy tidies it)"; reset; mkdir -p "$STATE/lock"
  STUB_TRIAGE="PLAY_PASS" run
  ck "review proceeds despite an orphaned legacy \$STATE/lock" "review PASS"

echo "58. the daily cap is PER-REPO — a foreign repo's spend never blocks this repo"; reset; mkdir -p "$STATE"
  printf 9999 > "$STATE/count-otherrepo-$(date +%Y%m%d)"
  STUB_TRIAGE="PLAY_PASS" run
  ck "foreign repo's exhausted cap does not block" "review PASS"
  reset; mkdir -p "$STATE"; printf 9999 > "$STATE/count-repo-$(date +%Y%m%d)"
  STUB_TRIAGE="PLAY_PASS" run
  ck "own repo's exhausted cap still aborts" "ABORTED — cap"


# ====================== pre-record: skipped-until-DELIVERED (r2 H-1/H-3/M-4) ======================
echo "59. an ABORTED hook review leaves its range QUEUED (crash/abort window closed)"; reset
  bare59="$ROOT/bare59.git"; git init -q --bare "$bare59"; git -C "$REPO" push -q "$bare59" "$TIP:refs/heads/main" 2>/dev/null
  env HOME="$FAKE" STUB_KILABZ_FAIL=1 bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "$bare59" "$EMPTY" 2>/dev/null
  ck "review aborted" "ABORTED"
  PRM="$STATE/skipped-$SKIPSLUG-$TIP"
  ckfile "$PRM" "pre-record survives the abort (range stays queued)"
  if [[ "$(cat "$PRM" 2>/dev/null)" == "$EMPTY" ]]; then echo "  ok: pre-record content = the reviewed base"; PASS=$((PASS+1)); else echo "  FAIL: pre-record content '$(cat "$PRM" 2>/dev/null)'"; FAIL=$((FAIL+1)); fi

echo "60. a DELIVERED hook review CONSUMES the pre-record (no stale marker)"; reset
  env HOME="$FAKE" STUB_TRIAGE="PLAY_PASS" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "$bare59" "$EMPTY" 2>/dev/null
  ck "review delivered" "review PASS"
  cknofile "$STATE/skipped-$SKIPSLUG-$TIP" "pre-record consumed on delivery"
  ckfile "$DMARKER" "done marker written"

echo "61. cap abort keeps the pre-record (r3 fail-open: capped ranges stay queued)"; reset; mkdir -p "$STATE"
  printf 9999 > "$STATE/count-repo-$(date +%Y%m%d)"
  env HOME="$FAKE" bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/main "$bare59" "$EMPTY" 2>/dev/null
  ck "aborts on cap" "ABORTED — cap"
  ckfile "$STATE/skipped-$SKIPSLUG-$TIP" "pre-record survives the cap abort (range stays queued)"

echo "62. pre-record claims the OWN hop, not the folded base (r3 toctou)"; reset; mkdir -p "$STATE"
  env HOME="$FAKE" STUB_KILABZ_FAIL=1 bash "$SCRIPT" --worker "$REPO" "$TIP" "$TIP3" refs/heads/main "$bare59" "$TIP2" 2>/dev/null || true
  got="$(cat "$STATE/skipped-$SKIPSLUG-$TIP3" 2>/dev/null)"
  if [[ "$got" == "$TIP2" ]]; then echo "  ok: marker content = orig_base (own hop), not the folded base"; PASS=$((PASS+1)); else echo "  FAIL: content '$got' want $TIP2 (own hop)"; FAIL=$((FAIL+1)); fi

# ====================== apply-rung findings fold (post-merge review 20260911114126) ============
echo "63. autofix_fire forwards the triggering remote as MYNDAIX_FIX_REMOTE (review #1)"; reset; af_repos "$NULLCFG"
  bareR="$ROOT/bare-remote63.git"; git init -q --bare "$bareR"; git -C "$REPO" push -q "$bareR" "$TIP:refs/heads/main" 2>/dev/null
  STUB_TRIAGE="1. fix it" run_af "$bareR"; wait_fixer
  if grep -q "REMOTE=$bareR" "$FAKE/.myndaix/fixer-env" 2>/dev/null; then echo "  ok: triggering remote forwarded to the fixer"; PASS=$((PASS+1)); else echo "  FAIL: MYNDAIX_FIX_REMOTE not forwarded (env: $(tr '\n' ' ' < "$FAKE/.myndaix/fixer-env" 2>/dev/null))"; FAIL=$((FAIL+1)); fi
echo "64. autofix is main-only: a NEEDS-FIX on a feature branch never fires (and says why)"; reset; af_repos "$NULLCFG"
  bareR="$ROOT/bare-remote64.git"; git init -q --bare "$bareR"; git -C "$REPO" push -q "$bareR" "$TIP:refs/heads/feat/x" 2>/dev/null
  env HOME="$FAKE" PLAY_AUTOFIX=1 PLAY_AUTOFIX_TEST_MODE=1 PLAY_FIX_SELF="$FIXER" STUB_TRIAGE="1. fix it" \
    bash "$SCRIPT" --worker "$REPO" "$EMPTY" "$TIP" refs/heads/feat/x "$bareR" 2>/dev/null; settle
  cknofile "$FAKE/.myndaix/fixer-argv" "feature-branch NEEDS-FIX -> no auto-fire"
  # the skip must be THIS gate (not e.g. an unconfirmed push) — else the case passes vacuously
  rj="$(ls -t "$RUNS"/*/play.jsonl 2>/dev/null | head -1)"
  ck "skip reason is the main-only gate" "autofix is main-only" "$rj"

echo; echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
