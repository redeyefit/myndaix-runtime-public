---
name: bash-env-trampoline
description: An in-script env scrub cannot protect its own interpreter
path_trigger: "tools/*.sh substrate/*.sh substrate/lab/*.sh orchestrator/*.sh orchestrator/phone/*.sh"
---

bash sources `$BASH_ENV` (non-interactive) and `$ENV` (POSIX mode) during INTERPRETER
STARTUP — before the first line of the script runs. An `unset BASH_ENV ENV ...` inside
the script therefore protects only CHILD processes; hostile code named by those vars has
already executed in the script's own shell.

For any script that runs with inherited, partially-trusted env at a trust boundary (SSH
forced commands, launchd jobs fed operator env, hooks), the scrub must happen BEFORE the
interpreter starts: a first-line self-re-exec trampoline
(`exec /usr/bin/env -i GUARD=1 HOME=... PATH=... /bin/bash -- "$0" "$@"` behind a
recursion-guard variable), or an env-cleaning caller. The `--` is load-bearing: without
it a hostile invocation name (a symlink literally named `-c`, `exec -a -c`) parses as a
bash OPTION and the first argument executes as code — the trampoline must not open an
option-injection hole while closing the env one.

Flag: security-bearing scripts that `unset BASH_ENV`/`ENV`/`PYTHONSTARTUP` mid-script and
treat that as self-protection; scrubs without a trampoline where the interpreter itself
is in the threat model. The unset is still correct as a belt for children — the finding
is claiming it defends the script itself.

(Learned 2026-09-03, twice in one day: Mack's own test suite asserted the wrong layer,
then kilabz caught the identical gap in the mxr-phone wrapper — fixed with the env -i
trampoline. Session memory does not transfer; this skill is the transfer.)
