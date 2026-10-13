# Bash tooling

We're bash-heavy (22+ shell scripts). CodeQL doesn't cover bash, so this is the net for it — three
layers, one command.

## Install (once)
```
brew install bats-core shellcheck semgrep
```

## Run
```
./tools/bash-check.sh      # shellcheck + our bug-rules + pipefail check, across every tracked *.sh
bats tests/bash/           # the bash test suite (lint gate + your unit tests)
```

## What's here
- **`ruff_new.py`** — ruff finding-diff ratchet used by CI (`python tools/ruff_new.py HEAD^1`): blocks findings NEW vs the base commit; the pyproject `[tool.ruff.lint] select` is explicit and ruff is pinned in the dev extra. Whole-repo debt: `ruff check --statistics .`.
- **`bash-rules.semgrep.yml`** — MyndAIX's *own* recurring bash bugs (from `~/.claude/rules/*.md`) as
  enforceable rules: `python3 -c` interpolation, `curl|bash`, `eval`, unguarded `rm -rf $var`, macOS
  `date -jf`. Shellcheck catches the *generic* bugs; this catches *ours*.
- **`bash-check.sh`** — the one command. **ERROR-level gates** (fails the run); shellcheck warnings +
  semgrep WARNING/INFO are *surfaced* but don't block (green on the existing debt; the write-time
  shellcheck hook keeps new files clean). Style/notes are muted.
- **`../tests/bash/lint.bats`** — asserts the repo stays ERROR-clean. **`example.bats`** — a copy-me
  template for unit-testing bash (and the leading-zero octal-trap regression).

## Deliberately NOT auto-flagged (needs human context, would be noise)
- `2>/dev/null || true` — fires on ~10 legit defensive uses (mkdir, lock cleanup, kill guards); a regex
  can't tell a benign best-effort call from a masked security failure. **Review checklist item.**
- `kill "$pid"` PID-validation — dataflow question, and it false-matches `launchctl kill`. **Review item.**

## Add a rule / silence a hit
- New bug pattern → add a rule to `bash-rules.semgrep.yml` (regex, `languages: [generic]`, cite the source).
- A legit hit → end the line with `# nosemgrep: <rule-id>`.

## Write a bash test
Copy a `@test` from `example.bats`. `run <cmd>` captures `$status` + `$output`; `setup_file`/`teardown_file`
for a temp workspace. Keep bash **thin** — when a script grows real logic, port it to Python (the ruff
ratchet + pytest run on it in CI).
