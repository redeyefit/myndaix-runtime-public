#!/usr/bin/env bats
# lint.bats — the repo's bash stays clean. Gates: shellcheck clean + no ERROR-level custom bug-rule.
# Each test SKIPS if its tool isn't installed, so a machine without the dep doesn't hard-fail the suite.
# Run: `bats tests/bash/`  (install: `brew install bats-core shellcheck semgrep`)

setup() {
  REPO="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  cd "$REPO" || return 1
}

@test "shellcheck: no ERROR-level findings across tracked shell scripts" {
  command -v shellcheck >/dev/null || skip "shellcheck not installed"
  # ERROR gate (green on the existing warning/style debt; the write-time hook keeps new files clean)
  run bash -c "git ls-files '*.sh' '*.bash' | grep -v '^\.venv/' | xargs shellcheck --severity=error"
  [ "$status" -eq 0 ]
}

@test "no ERROR-level findings from our custom bash bug-rules (semgrep)" {
  command -v semgrep >/dev/null || skip "semgrep not installed"
  run bash -c "git ls-files '*.sh' '*.bash' | grep -v '^\.venv/' | xargs semgrep --quiet --error --severity ERROR --config tools/bash-rules.semgrep.yml"
  [ "$status" -eq 0 ]
}

@test "every tracked shell script has a pipefail safety header" {
  run bash tools/bash-check.sh
  # surface the report; the pipefail check inside bash-check gates rc, so assert it passed
  echo "$output"
  [[ "$output" != *"MISSING pipefail"* ]]
}
