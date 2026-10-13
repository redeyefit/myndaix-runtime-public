#!/usr/bin/env bash
# test-xreview.sh — smoke test for xreview.sh (phase-routed manual cross-family review) with a STUBBED
# mxr. Proves the routing AND the security/robustness invariants folded from THREE rounds of dogfooding:
#   CODE mode gates on kilabz via `mxr review` (staged snapshot) with a SHA-PINNED range (immune to ref
#   movement); a kilabz failure is a HARD stop that surfaces the gate's stderr root-cause; oracle is a
#   degradable weak backup that gets the REAL fenced diff on the IDENTICAL SHAs, and is SKIPPED LOUDLY
#   (never silently) when the diff would blow ARG_MAX; a staging DEGRADATION is surfaced; raw reviews
#   print before synthesis; the upstream-input fence nonce and the downstream-synthesis nonce DIFFER (no
#   fence escape into lobster); reviewer output is control-char (ESC + CR) sanitized.
#   DESIGN mode leads with oracle, degrades if one is down, but FAILS CLOSED if BOTH are down.
# Run: bash orchestrator/test-xreview.sh
set -uo pipefail
SCRIPT="$(cd "$(dirname "$0")" && pwd)/xreview.sh"
ROOT="$(mktemp -d /tmp/xreview-test.XXXXXX)"; FAKE="$ROOT/home"
PASS=0; FAIL=0
trap 'rm -rf "$ROOT"' EXIT

mkdir -p "$FAKE/.local/bin"
cat > "$FAKE/.local/bin/mxr" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$HOME/mxr-argv.log" 2>/dev/null || true
# --prompt-file dispatch (issue #83): ALSO log the file's CONTENT so content assertions keep working
argvv=("$@"); for ((j=0; j<${#argvv[@]}; j++)); do
  [[ "${argvv[$j]}" == "--prompt-file" ]] && cat "${argvv[$((j+1))]}" >> "$HOME/mxr-argv.log" 2>/dev/null
done
if [[ "${1:-}" == "review-stage" ]]; then      # lobster synthesis snapshot (issue #83 item 2)
  [[ -n "${STUB_STAGE_FAIL:-}" ]] && { echo "staging failed: stub" >&2; exit 1; }
  d="$HOME/stub-staging/review-stub"; mkdir -p "$d"; printf '%s\n' "$d"; exit 0
fi
[[ "${1:-}" == "review-teardown" ]] && exit 0
[[ "${1:-}" == "review-reap" ]] && exit 0       # startup orphan reap (best-effort)
if [[ "${1:-}" == "review" ]]; then            # `mxr review <agent> --repo .. --range ..` = code gate
  [[ -n "${STUB_KILABZ_FAIL:-}" ]] && { printf 'GATE-ROOT-CAUSE-XYZ\n' >&2; exit 1; }   # emit a root cause on stderr
  [[ -n "${STUB_DEGRADE:-}" ]] && printf 'kilabz review ran WITHOUT snapshot (staging failed, inline-only)\n' >&2
  printf '%s\n' "${STUB_KILABZ:-KILABZ code review: real bug at line 1}"; exit 0
fi
# `mxr [--repo VAL].. <agent> <prompt>` — find the agent = first non-flag token (skip flag+value pairs)
argv=("$@"); i=0; agent=""
while [[ $i -lt ${#argv[@]} ]]; do
  case "${argv[$i]}" in
    --repo|--base-ref|--staged-workdir|--tip|--range|--prompt-file) i=$((i+2)); continue ;;
    --*) i=$((i+1)); continue ;;
    *) agent="${argv[$i]}"; break ;;
  esac
done
case "$agent" in
  oracle)  [[ -n "${STUB_ORACLE_FAIL:-}" ]] && exit 1; printf '%s\n' "${STUB_ORACLE:-ORACLE review: reframe the thesis}"; exit 0 ;;
  kilabz)  [[ -n "${STUB_KILABZ_FAIL:-}" ]] && exit 1; printf '%s\n' "${STUB_KILABZ:-KILABZ review: complete}"; exit 0 ;;
  lobster) [[ -n "${STUB_LOBSTER_EMPTY:-}" ]] && exit 0; printf '%s\n' "${STUB_TRIAGE:-1. fix the bug}"; exit 0 ;;
  *) printf 'stub:%s\n' "$agent"; exit 0 ;;
esac
STUB
chmod +x "$FAKE/.local/bin/mxr"
# openssl stub returns a DISTINCT value per call (via a counter) so the two nonces the script mints
# (nonce_in for upstream fences, nonce_syn for the lobster synthesis fence) are observably different.
cat > "$FAKE/.local/bin/openssl" <<'OSTUB'
#!/usr/bin/env bash
c="$HOME/.oc"; n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 )); printf '%s' "$n" > "$c"; printf 'nonce%08d\n' "$n"
OSTUB
chmod +x "$FAKE/.local/bin/openssl"

# a REAL throwaway git repo so code mode's _repo_path resolves (abs path -> has .git), rev-parse pins
# the range to SHAs, and the oracle-backup `git diff <base> <head>` produces a real, non-empty diff.
REPO="$ROOT/repo"; mkdir -p "$REPO"
git -C "$REPO" init -q
printf 'base-line\n' > "$REPO/f.txt"; git -C "$REPO" add f.txt
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m base
printf 'head-line\n' > "$REPO/f.txt"
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -am head
RANGE="HEAD~1..HEAD"

DOC="$ROOT/design.md"; printf '# a design\n\nthesis: X\n' > "$DOC"
run(){ env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" bash "$SCRIPT" "$@" 2>"$ROOT/err"; }
log(){ echo "$FAKE/mxr-argv.log"; }
ck(){ if grep -q "$2" "$1" 2>/dev/null; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3"; FAIL=$((FAIL+1)); fi; }
cke(){ if grep -Eq "$2" "$1" 2>/dev/null; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3"; FAIL=$((FAIL+1)); fi; }
ckx(){ if [[ "$1" == "$2" ]]; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3 (rc $1 != $2)"; FAIL=$((FAIL+1)); fi; }
cko(){ if echo "$1" | grep -q "$2"; then echo "  ok: $3"; PASS=$((PASS+1)); else echo "  FAIL: $3"; FAIL=$((FAIL+1)); fi; }

echo "1. code mode: kilabz GATE via 'mxr review' (SHA-PINNED range), oracle gets the REAL fenced diff, verdict printed"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(run code "$REPO" "$RANGE")"; ckx $? 0 "code review exits 0"
  ck "$(log)" "review kilabz --repo $REPO --range" "kilabz gate dispatched via mxr review (snapshot)"
  cke "$(log)" "review kilabz --repo .* --range [0-9a-f]{7,}\.\.[0-9a-f]{7,}" "gate --range is SHA-pinned (r3 #2 — immune to ref movement)"
  ck "$(log)" "BEGIN UNTRUSTED pushed-diff" "oracle backup gets a nonce-fenced diff"
  ck "$(log)" "head-line" "the fenced diff carries the real change (base->head)"
  cko "$out" "XREVIEW VERDICT (code)" "prints a synthesized code verdict"
  cko "$out" "kilabz-review (authoritative" "raw kilabz review printed to stdout (survives a lobster failure)"
  ck "$(log)" "review-stage $REPO" "lobster synthesis snapshot staged (issue #83 item 2)"
  ck "$(log)" "\-\-staged-workdir" "lobster call carries the staged snapshot cwd"
  ck "$(log)" "review-teardown" "snapshot torn down on the happy path (lobster returned terminal)"
  ck "$(log)" "\-\-prompt-file" "oracle/lobster prompts ride --prompt-file, not argv (issue #83 item 1)"

echo "1c. code mode: a lobster TIMEOUT/empty result must NOT teardown (liveness — a live job may hold the cwd)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_LOBSTER_EMPTY=1 bash "$SCRIPT" code "$REPO" "$RANGE" 2>"$ROOT/err")"; ckx $? 0 "empty-lobster review still exits 0"
  ck "$(log)" "review-stage $REPO" "snapshot still staged"
  if grep -q "review-teardown" "$(log)"; then echo "  FAIL: tore down a snapshot a live lobster job may hold"; FAIL=$((FAIL+1)); else echo "  ok: NO teardown after a lobster timeout (left to the age-reaper)"; PASS=$((PASS+1)); fi
  ck "$ROOT/err" "age-reaper" "left-for-reaper noted on stderr"
  cko "$out" "read the two reviews printed above" "fallback synthesis text on empty lobster"

echo "2. code mode: the UPSTREAM-input nonce and the SYNTHESIS nonce DIFFER (r2 HIGH — no fence escape into lobster)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  run code "$REPO" "$RANGE" >/dev/null
  distinct="$(grep -o 'nonce=nonce[0-9]*' "$(log)" | sort -u | wc -l | tr -d ' ')"
  ckx "$([[ "$distinct" -ge 2 ]] && echo ok)" "ok" "at least 2 distinct fence nonces in play (got $distinct)"

echo "3. code mode: reviewer output is control-char (ESC + CR) sanitized on print (r2/r3 #4)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  esc="$(printf '\033')"; cr="$(printf '\r')"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_KILABZ="$(printf '\033[31mANSI-HACK\033[0m real\rREWRITE bug')" bash "$SCRIPT" code "$REPO" "$RANGE" 2>/dev/null)"
  cko "$out" "ANSI-HACK" "review text content survives sanitization"
  if printf '%s' "$out" | LC_ALL=C grep -q "$esc"; then echo "  FAIL: raw ESC reached stdout"; FAIL=$((FAIL+1)); else echo "  ok: ESC stripped from printed output"; PASS=$((PASS+1)); fi
  if printf '%s' "$out" | LC_ALL=C grep -q "$cr"; then echo "  FAIL: raw CR reached stdout"; FAIL=$((FAIL+1)); else echo "  ok: CR stripped from printed output"; PASS=$((PASS+1)); fi

echo "4. code mode: an OVERSIZED diff SKIPS the oracle backup LOUDLY, never a silent ARG_MAX drop (r3 #1 HIGH)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" XREVIEW_DIFF_CAP=10 bash "$SCRIPT" code "$REPO" "$RANGE" 2>"$ROOT/err")"; ckx $? 0 "oversized-diff review still exits 0"
  ck "$ROOT/err" "SKIPPING the oracle backup" "oversize skip warned LOUDLY on stderr"
  cko "$out" "oracle backup SKIPPED" "verdict records the oracle skip (not silent)"
  cko "$out" "XREVIEW VERDICT" "gate-only verdict still produced"

echo "5. code mode: a kilabz failure is a HARD stop AND surfaces the gate's stderr root-cause (r3 #5)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_KILABZ_FAIL=1 bash "$SCRIPT" code "$REPO" "$RANGE" >/dev/null 2>"$ROOT/err"; ckx $? 2 "kilabz gate failure -> exit 2"
  ck "$ROOT/err" "code gate" "error names the code gate"
  ck "$ROOT/err" "GATE-ROOT-CAUSE-XYZ" "gate stderr root-cause surfaced before the temp is deleted"

echo "6. code mode: oracle down still produces a verdict (weak backup, review proceeds)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 bash "$SCRIPT" code "$REPO" "$RANGE" 2>/dev/null)"; ckx $? 0 "oracle-down code review still exits 0"
  cko "$out" "XREVIEW VERDICT" "verdict produced on the kilabz gate alone"

echo "7. code mode: a staging DEGRADATION is surfaced LOUDLY, not swallowed (r1 #1)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_DEGRADE=1 bash "$SCRIPT" code "$REPO" "$RANGE" >/dev/null 2>"$ROOT/err"; ckx $? 0 "degraded gate still exits 0"
  ck "$ROOT/err" "DEGRADED" "staging degradation warned on stderr"

echo "8. code mode: a MISSING objective-file does NOT abort the gate (r2 #4)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(run code "$REPO" "$RANGE" /no/such/obj.txt)"; ckx $? 0 "missing objf still exits 0"
  # the GATE line specifically must not carry --prompt-file (oracle/lobster legitimately use it now)
  if grep "^review kilabz" "$(log)" | grep -q -- "--prompt-file"; then echo "  FAIL: gate passed --prompt-file for a missing objf"; FAIL=$((FAIL+1)); else echo "  ok: no --prompt-file on the GATE for a missing objf"; PASS=$((PASS+1)); fi

echo "8b. code mode: lobster snapshot staging FAILURE degrades LOUDLY to reconcile-only (issue #83)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_STAGE_FAIL=1 bash "$SCRIPT" code "$REPO" "$RANGE" 2>"$ROOT/err")"; ckx $? 0 "stage-fail still exits 0 (additive, degradable)"
  ck "$ROOT/err" "reconcile-only" "degradation warned loudly"
  if grep -q -- "--staged-workdir" "$(log)"; then echo "  FAIL: lobster got a workdir despite stage failure"; FAIL=$((FAIL+1)); else echo "  ok: no --staged-workdir after a stage failure"; PASS=$((PASS+1)); fi
  cko "$out" "XREVIEW VERDICT" "verdict still produced reconcile-only"

echo "9. code mode: an unresolvable repo is a usage error (exit 2)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  run code /no/such/repo "$RANGE" >/dev/null 2>&1; ckx $? 2 "unresolvable repo -> exit 2"

echo "9b. code mode: a git WORKTREE path resolves (its .git is a FILE, not a dir)"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  WT="$ROOT/wt"; git -C "$REPO" worktree add -q --detach "$WT" HEAD
  run code "$WT" "$RANGE" >/dev/null; ckx $? 0 "code review from a worktree exits 0"
  ck "$(log)" "review kilabz --repo $WT --range" "kilabz gate dispatched against the worktree path"

echo "10. design mode: oracle LEADS (dispatched), verdict printed, no snapshot verb"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(run design "$DOC")"; ckx $? 0 "design review exits 0"
  if grep -q "oracle" "$(log)" && grep -q "kilabz" "$(log)"; then echo "  ok: both oracle (lead) + kilabz dispatched"; PASS=$((PASS+1)); else echo "  FAIL: design routing missing a family"; FAIL=$((FAIL+1)); fi
  if grep -q "^review " "$(log)"; then echo "  FAIL: design mode used the code snapshot verb"; FAIL=$((FAIL+1)); else echo "  ok: design mode embeds the doc (no snapshot verb)"; PASS=$((PASS+1)); fi

echo "11. design mode: ONE reviewer down degrades (not a hard stop) — still exits 0 with a verdict"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  out="$(env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 bash "$SCRIPT" design "$DOC" 2>"$ROOT/err")"; ckx $? 0 "oracle-down design review degrades, exits 0"
  ck "$ROOT/err" "DEGRADED" "degradation warned loudly"

echo "12. design mode: BOTH reviewers down -> FAIL CLOSED (r2 #2), no verdict"; rm -f "$FAKE/mxr-argv.log" "$FAKE/.oc"
  env HOME="$FAKE" PATH="$FAKE/.local/bin:$PATH" STUB_ORACLE_FAIL=1 STUB_KILABZ_FAIL=1 bash "$SCRIPT" design "$DOC" >/dev/null 2>"$ROOT/err"; ckx $? 2 "both design reviewers down -> exit 2"
  ck "$ROOT/err" "both design reviewers" "error names the both-reviewer failure"

echo "13. usage errors exit 2"; run bogus >/dev/null 2>&1; ckx $? 2 "bad mode -> exit 2"
  run code "$REPO" not-a-range >/dev/null 2>&1; ckx $? 2 "code without a range -> exit 2"
  run design /no/such/doc >/dev/null 2>&1; ckx $? 2 "design without a real doc -> exit 2"

echo; echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
