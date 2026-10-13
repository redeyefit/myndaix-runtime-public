#!/usr/bin/env python3
"""Block ruff findings that are NEW vs a base commit; never block pre-existing debt.

The repo carries ~48 pre-existing findings under the current [tool.ruff.lint] select (see
`ruff check --statistics .`) — too many to gate whole-repo without freezing every future PR that
merely touches a file with old debt in it. This is a finding-diff RATCHET, the same semantics as
semgrep --baseline-commit: for every .py file the change touches, findings present in BOTH the
base commit's version and the current version are "inherited" (never block); findings present
only in the current version are "new" (block). A file with zero .py changes is never linted at
all, so untouched debt anywhere else in the tree is invisible to this gate — see
`ruff check --exit-zero --statistics .` in CI for the whole-repo burn-down number.

Matching is by (file, rule code, message) as a multiset, NOT by line number: editing anywhere
else in a file shifts line numbers for pre-existing findings below the edit, and a naive
line-keyed diff would misreport untouched debt as "new" on every such edit (this is why
touching controller.py's PLW1510-heavy region for an unrelated fix must still report 0 new).
Message text includes enough specificity (variable/import names) that two distinct real
violations of the same rule in the same file essentially never collide.

Base-commit content is linted from a temp mirror, not a full `git worktree`/checkout, because
running ruff needs the SAME pyproject.toml applied to old and new content — a difference in
findings must come from content, never from the rule set changing between commits (this PR's own
diff, which is the FIRST commit to populate [tool.ruff.lint] select, would otherwise show every
pre-existing finding as "new" simply because the base commit's pyproject.toml had no select at
all). The mirror preserves each file's CURRENT (head) relative path — not its base-commit path —
so path-based config like `[tool.ruff.lint.per-file-ignores]` ("tests/**") is evaluated
consistently for both sides of a renamed file.

Usage:
    python tools/ruff_new.py <base-ref> [--config PATH]

Exit codes:
    0 — no changed .py files, or changed files produced zero new findings.
    1 — one or more new findings (printed to stdout).
    2 — could not run: ruff missing from PATH, base ref unresolvable, a git/ruff invocation
        failed, or ruff's JSON output was missing or unparseable. This path NEVER prints "0 new findings" —
        an inability to check is not a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import NoReturn

# ruff's own field names in --output-format=json; keying findings on these three (never the
# row/column, which drifts with unrelated edits elsewhere in the same file) is the whole ratchet.
FindingKey = tuple[str, str, str]  # (relative_path, code, message)


def die(message: str) -> NoReturn:
    print(f"::error::ruff_new: {message}", file=sys.stderr)
    sys.exit(2)


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False, env=env)
    except OSError as exc:
        die(f"failed to run {cmd[0]!r}: {exc}")


def git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=repo_root)


def resolve_repo_root() -> Path:
    proc = run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd())
    if proc.returncode != 0:
        die(f"not inside a git repository: {proc.stderr.strip()}")
    return Path(proc.stdout.strip())


def resolve_base_commit(repo_root: Path, base_ref: str) -> str:
    proc = git(repo_root, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
    if proc.returncode != 0 or not proc.stdout.strip():
        die(
            f"base ref {base_ref!r} does not resolve to a commit here — likely a shallow clone "
            f"missing history (CI: checkout needs fetch-depth >= 2). git said: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


class ChangedFile:
    __slots__ = ("status", "old_path", "new_path")

    def __init__(self, status: str, old_path: str | None, new_path: str) -> None:
        self.status = status  # 'A', 'M', 'T', 'R', 'C' ('D' is filtered out by the caller)
        self.old_path = old_path
        self.new_path = new_path

    @property
    def is_rename(self) -> bool:
        return self.status[0] in ("R", "C")


def changed_py_files(repo_root: Path, base_commit: str) -> list[ChangedFile]:
    # One ref (not base..HEAD): `git diff <commit>` diffs that commit against the WORKING TREE
    # (index + unstaged), so a local uncommitted edit is caught same as a committed one — the
    # builder-verification negative case edits a file without requiring a commit first.
    proc = git(repo_root, "diff", "--name-status", "-M", "-z", base_commit, "--", "*.py")
    if proc.returncode != 0:
        die(f"git diff against {base_commit} failed: {proc.stderr.strip()}")
    tokens = [t for t in proc.stdout.split("\0") if t != ""]
    files: list[ChangedFile] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if status[0] in ("R", "C"):
            old_path, new_path = tokens[i], tokens[i + 1]
            i += 2
            files.append(ChangedFile(status, old_path, new_path))
        elif status[0] == "D":
            i += 1  # deleted file: nothing left to lint, drop it
        else:  # A, M, T
            path = tokens[i]
            i += 1
            files.append(ChangedFile(status, None, path))

    # Untracked new .py files (created but never `git add`ed) are invisible to `git diff` against
    # a commit — it only compares tracked content. Fold them in as bare adds so a local pre-commit
    # check catches them the same way CI would once they land in a commit.
    # -z (NUL-separated, unquoted paths) instead of the default quoted-line format: a path with a
    # space or other special char is otherwise wrapped in double quotes by porcelain, and
    # `.endswith(".py")` then fails on the trailing quote char, silently dropping the file.
    # --untracked-files=all is REQUIRED even though the `-- "*.py"` pathspec already makes git
    # recurse into a new directory: a local/CI `status.showUntrackedFiles=no` otherwise drops every
    # untracked row with no error, and a new file's findings vanish from the gate (review finding).
    # A failed status is a die(), like the diff above: an inability to list files is not a pass.
    proc = git(repo_root, "status", "--porcelain", "--no-renames", "--untracked-files=all", "-z",
               "--", "*.py")
    if proc.returncode != 0:
        die(f"git status (untracked .py discovery) failed: {proc.stderr.strip()}")
    known = {f.new_path for f in files}
    for token in proc.stdout.split("\0"):
        if not token.startswith("??"):
            continue
        path = token[3:]
        if path.endswith(".py") and path not in known:
            files.append(ChangedFile("A", None, path))
    return files


def resolve_config(repo_root: Path, explicit: str | None) -> Path | None:
    if explicit:
        cfg = Path(explicit)
        if not cfg.is_absolute():
            cfg = repo_root / cfg
        if not cfg.is_file():
            die(f"--config {explicit!r} does not exist")
        return cfg
    auto = repo_root / "pyproject.toml"
    return auto if auto.is_file() else None


def ruff_json(
    ruff_bin: str, config: Path | None, cwd: Path, rel_paths: list[str]
) -> list[dict]:
    if not rel_paths:
        return []
    cmd = [ruff_bin, "check", "--output-format=json", "--exit-zero", "--no-cache"]
    if config is not None:
        cmd += ["--config", str(config)]
    cmd += ["--", *rel_paths]  # a path beginning with "-" must never parse as a ruff option
    # ruff honors RUFF_OUTPUT_FILE and writes the JSON THERE, leaving stdout empty — which read as
    # "zero findings", a false pass (verified). Strip it from the child env; and because ruff
    # prints "[]" for zero findings, an EMPTY stdout can only mean the output went elsewhere.
    env = {k: v for k, v in os.environ.items() if k != "RUFF_OUTPUT_FILE"}
    proc = run(cmd, cwd=cwd, env=env)
    # --exit-zero makes ruff return 0 for lint findings (incl. E9 syntax errors, which are
    # findings under our select) — a NON-zero return here means ruff itself couldn't run:
    # a broken pyproject.toml, an unknown rule code, a bad CLI arg. Fail closed, never guess.
    if proc.returncode != 0:
        die(f"ruff invocation failed (config or CLI problem): {proc.stderr.strip()}")
    if not proc.stdout.strip():
        die("ruff printed no JSON on stdout (output redirected?) — refusing to read that as zero findings")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        die(f"could not parse ruff JSON output: {exc}")


def to_relative(findings: list[dict], root: Path) -> Counter[FindingKey]:
    counts: Counter[FindingKey] = Counter()
    for f in findings:
        try:
            rel = str(Path(f["filename"]).resolve().relative_to(root.resolve()))
        except ValueError:
            rel = f["filename"]
        counts[(rel, f["code"], f["message"])] += 1
    return counts


def main(argv: list[str]) -> int:
    # __doc__ is str | None to the type checker (a module can lack one) — this one never does,
    # but Pyright's report is real (reportOptionalMemberAccess), so fall back instead of
    # asserting the impossible away.
    doc_lines = (__doc__ or "").splitlines()
    description = doc_lines[0] if doc_lines else "ruff finding-diff ratchet"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("base_ref", help="git ref/commit to diff against (e.g. HEAD^1, origin/main)")
    parser.add_argument("--config", help="path to the ruff config (default: auto-detect pyproject.toml at repo root)")
    args = parser.parse_args(argv[1:])

    ruff_bin = shutil.which("ruff")
    if ruff_bin is None:
        die("ruff not found on PATH")

    repo_root = resolve_repo_root()
    base_commit = resolve_base_commit(repo_root, args.base_ref)
    config = resolve_config(repo_root, args.config)

    files = changed_py_files(repo_root, base_commit)
    if not files:
        print("ruff_new: no changed .py files")
        return 0

    # Base-commit content is materialized into a throwaway mirror at each file's CURRENT
    # (head) relative path, then linted with the CURRENT pyproject.toml — see module docstring
    # for why both of those must match head's world, not base's.
    with tempfile.TemporaryDirectory(prefix="ruff_new_base_") as tmp:
        tmp_root = Path(tmp)
        base_targets: list[str] = []
        for cf in files:
            source_path = cf.old_path if cf.old_path is not None else cf.new_path
            show = git(repo_root, "show", f"{base_commit}:{source_path}")
            if show.returncode != 0:
                continue  # file didn't exist at base (e.g. a genuinely new file) -> no base debt
            dest = tmp_root / cf.new_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(show.stdout, encoding="utf-8")
            base_targets.append(cf.new_path)
        if config is not None:
            # Preserve the repo-relative path (needed so a config that itself lives under a
            # sub-package still discovers correctly) when config IS under repo_root; an
            # explicit --config pointing OUTSIDE repo_root (the flag exists for exactly this)
            # has no repo-relative path to preserve, so just mirror it by filename instead.
            try:
                mirrored_config = tmp_root / config.resolve().relative_to(repo_root.resolve())
            except ValueError:
                mirrored_config = tmp_root / config.name
            mirrored_config.parent.mkdir(parents=True, exist_ok=True)
            mirrored_config.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
            base_config = mirrored_config
        else:
            base_config = None

        base_raw = ruff_json(ruff_bin, base_config, tmp_root, base_targets)
        base_counts = to_relative(base_raw, tmp_root)

    # Head content is linted in place (real repo tree, real cwd) so per-file-ignores, excludes
    # and any relative config paths resolve exactly as they do for a normal `ruff check .`.
    head_targets = [cf.new_path for cf in files if (repo_root / cf.new_path).is_file()]
    head_raw = ruff_json(ruff_bin, config, repo_root, head_targets)
    head_counts = to_relative(head_raw, repo_root)

    rename_paths = {cf.new_path for cf in files if cf.is_rename}

    # inherited counts only non-renamed touched files: a renamed file's own carried-over debt is
    # real but kept OUT of this headline number, so "N new / M inherited" always answers "did this
    # edit add debt to the files it actually edited" — a rename's own pre-existing findings are a
    # separate concern (whether the rename itself smuggled in change), reported via rename_new.
    new_findings: Counter[FindingKey] = Counter()
    inherited = 0
    rename_new = 0
    for key, head_n in head_counts.items():
        base_n = base_counts.get(key, 0)
        # Deliberate: this nets counts per (file, rule, message), not per specific occurrence —
        # fixing one instance and introducing another with the identical message in the same
        # file cancels out to new_n=0. Accepted tradeoff, not a bug: line numbers shift too much
        # across an edit to match findings by location instead, and net counting still holds the
        # ratchet invariant that matters (existing debt can never grow unnoticed).
        new_n = max(0, head_n - base_n)
        if key[0] in rename_paths:
            # Still blocking, and still recorded in new_findings (below) so the per-finding
            # print loop reports its file/rule/message like any other new finding — only the
            # `inherited` headline count skips renamed files (see comment above).
            if new_n:
                new_findings[key] = new_n
                rename_new += new_n
            continue
        inherited += min(head_n, base_n)
        if new_n:
            new_findings[key] = new_n

    # rename_new entries are already inside new_findings (added above), so total_new alone
    # is the full blocking count — do not add rename_new again or it double-counts.
    total_new = sum(new_findings.values())
    total_blocking = total_new

    if total_blocking == 0:
        suffix = f", {rename_new} in renamed files" if rename_paths else ""
        print(
            f"ruff_new: 0 new findings ({inherited} inherited, {len(files)} changed file(s){suffix})"
        )
        return 0

    # Report every new finding with enough to act on it: path, line, rule, message.
    by_file: dict[str, list[dict]] = {}
    for f in head_raw:
        rel = str(Path(f["filename"]).resolve().relative_to(repo_root.resolve()))
        if (rel, f["code"], f["message"]) in new_findings and new_findings[(rel, f["code"], f["message"])] > 0:
            by_file.setdefault(rel, []).append(f)
            new_findings[(rel, f["code"], f["message"])] -= 1
    for rel in sorted(by_file):
        for f in sorted(by_file[rel], key=lambda x: (x["location"]["row"], x["location"]["column"])):
            row, col = f["location"]["row"], f["location"]["column"]
            print(f"{rel}:{row}:{col}: {f['code']} {f['message']}")

    print(
        f"ruff_new: {total_blocking} new finding(s) — {inherited} inherited debt unaffected"
        + (f", {rename_new} new in renamed files" if rename_paths else "")
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise  # die() and argparse already set the right code (2 / argparse's own usage code)
    except Exception as exc:  # deliberately broad: last-resort fail-closed, not normal-flow control
        # Any bug or unhandled edge case here must never look like "0 new findings" (rc 0) or
        # get confused with a real "new findings" result (rc 1) — die closed, same as the
        # explicit die() paths above.
        print(f"::error::ruff_new: unexpected failure: {exc!r}", file=sys.stderr)
        sys.exit(2)
