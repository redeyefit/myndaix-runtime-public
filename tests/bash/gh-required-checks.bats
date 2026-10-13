#!/usr/bin/env bats
# orchestrator/gh-required-checks.sh against a stub `gh` that holds the branch-protection state in a
# file and applies PATCHes the way GitHub does (the whole `checks` list is replaced). MISBEHAVE=drop
# makes the stub silently drop the last check, to prove the post-PATCH verification catches it.

SCRIPT="$BATS_TEST_DIRNAME/../../orchestrator/gh-required-checks.sh"

setup() {
  T="$(mktemp -d)"
  mkdir -p "$T/bin"
  export STATE="$T/state.json" PATCH_LOG="$T/patches.log"
  cat > "$T/bin/gh" <<'EOF'
#!/usr/bin/env bash
# stub: `gh api <ep>` -> state; `gh api -X PATCH <ep> --input f` -> replace checks (GitHub semantics)
if [ "$1 $2" = "api -X" ]; then
  input="$6"
  cat "$input" >> "$PATCH_LOG"; echo >> "$PATCH_LOG"
  python3 - "$input" "$STATE" "${MISBEHAVE:-}" <<'PYEOF'
import json, sys
p = json.load(open(sys.argv[1]))
checks = p["checks"][:-1] if sys.argv[3] == "drop" else p["checks"]
json.dump({"strict": p["strict"], "contexts": [c["context"] for c in checks], "checks": checks},
          open(sys.argv[2], "w"))
PYEOF
  echo '{}'
else
  cat "$STATE"
  # RACE=1: another writer adds a check right after this script's FIRST read
  if [ -n "${RACE:-}" ] && [ ! -e "$STATE.raced" ]; then
    touch "$STATE.raced"
    python3 - "$STATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
d["checks"].append({"context": "other-writer", "app_id": 15368})
d["contexts"].append("other-writer")
json.dump(d, open(sys.argv[1], "w"))
PYEOF
  fi
fi
EOF
  chmod +x "$T/bin/gh"
  # GH_BIN, never PATH: the script prepends the real gh to PATH, so only GH_BIN keeps tests off the
  # live API
  export GH_BIN="$T/bin/gh"
  printf '%s' '{"strict": false, "contexts": ["web", "security-test"], "checks": [{"context": "web", "app_id": 15368}, {"context": "security-test", "app_id": 15368}]}' > "$STATE"
}

teardown() { rm -rf "$T"; }

checks() { jq -r '[.checks[] | "\(.context):\(.app_id)"] | join(" ")' "$STATE"; }

@test "show lists the required checks and changes nothing" {
  run bash "$SCRIPT" o/r show
  [ "$status" -eq 0 ]
  [[ "$output" == *"web  (app 15368)"* && "$output" == *"security-test  (app 15368)"* ]]
  [ ! -e "$PATCH_LOG" ]
}

@test "add keeps every existing pinned check and pins the new one to GitHub Actions" {
  run bash "$SCRIPT" o/r add mx/ai-review
  [ "$status" -eq 0 ]
  [[ "$output" == *"verified: required checks"* ]]
  [ "$(checks)" = "web:15368 security-test:15368 mx/ai-review:15368" ]
}

@test "remove drops exactly one check and keeps the rest pinned" {
  bash "$SCRIPT" o/r add mx/ai-review
  run bash "$SCRIPT" o/r remove mx/ai-review
  [ "$status" -eq 0 ]
  [ "$(checks)" = "web:15368 security-test:15368" ]
}

@test "strict is carried over unchanged" {
  printf '%s' '{"strict": true, "contexts": ["web"], "checks": [{"context": "web", "app_id": 15368}]}' > "$STATE"
  run bash "$SCRIPT" o/r add mx/ai-review
  [ "$status" -eq 0 ]
  grep -q '"strict": true' "$PATCH_LOG"
}

@test "refuses a duplicate add, an unknown remove, and removing the last check — no PATCH sent" {
  run bash "$SCRIPT" o/r add web
  [ "$status" -ne 0 ] && [[ "$output" == *"already required"* ]]
  run bash "$SCRIPT" o/r remove nope
  [ "$status" -ne 0 ] && [[ "$output" == *"not a required check"* ]]
  printf '%s' '{"strict": false, "contexts": ["web"], "checks": [{"context": "web", "app_id": 15368}]}' > "$STATE"
  run bash "$SCRIPT" o/r remove web
  [ "$status" -ne 0 ] && [[ "$output" == *"NO required checks"* ]]
  [ ! -e "$PATCH_LOG" ]
}

@test "refuses when contexts and checks disagree" {
  printf '%s' '{"strict": false, "contexts": ["web", "legacy"], "checks": [{"context": "web", "app_id": 15368}]}' > "$STATE"
  run bash "$SCRIPT" o/r add mx/ai-review
  [ "$status" -ne 0 ] && [[ "$output" == *"disagree"* ]]
  [ ! -e "$PATCH_LOG" ]
}

@test "a result that differs from the intent after PATCH fails loudly" {
  MISBEHAVE=drop run bash "$SCRIPT" o/r add mx/ai-review
  [ "$status" -ne 0 ]
  [[ "$output" == *"MISMATCH after PATCH"* ]]
}

@test "bad arguments are a usage error" {
  run bash "$SCRIPT" "not a repo" show
  [ "$status" -eq 2 ]
  run bash "$SCRIPT" o/r frobnicate
  [ "$status" -eq 2 ]
  run bash "$SCRIPT" o/r add 'x;rm -rf /'
  [ "$status" -eq 2 ]
}

@test "another writer changing the list between read and write aborts with nothing written (P4 r1)" {
  RACE=1 run bash "$SCRIPT" o/r add mx/ai-review
  [ "$status" -ne 0 ]
  [[ "$output" == *"ABORT: required checks changed"* ]]
  [ ! -e "$PATCH_LOG" ]
  [ "$(checks)" = "web:15368 security-test:15368 other-writer:15368" ]
}
