"""C5 - workspace isolation.

A `workspace-actor` job runs in an **ephemeral git worktree** created from a
pinned ref. It mutates ONLY that tree; its changes are captured as a diff
artifact (never auto-merged); the live repo is never touched mid-flight. Two
concurrent actors get separate worktrees, so they can't corrupt a shared repo -
the failure a single-threaded runtime only prevented by accident.

The runner enforces cwd=worktree (C5); this class manages the worktree lifecycle.

Crash recovery (PR-1c): the graceful path removes a worktree in the worker's
`finally`, but a HARD crash (SIGKILL, power loss) leaves an orphan. So the root
is STABLE and shared (from $MYNDAIX_WORKTREE_ROOT or a fixed tmp path, NOT a
per-process mkdtemp), worktree dirs are named by `attempt_id`, and `sweep()` GCs
any whose attempt is no longer open - which a NEW process can run over a DEAD
one's leftovers precisely because the root is shared.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Iterable, Optional

_ROOT_ENV = "MYNDAIX_WORKTREE_ROOT"
_PREFIX = "wt-"                       # worktree dir = wt-<attempt_id>; sweep() keys off this


def _git_timeout() -> int:
    """Wall-clock cap for a single git worktree op. A wedged git (index.lock held, NFS/APFS I/O
    stall, a huge add/diff, a credential/hook prompt) MUST NOT hang forever: core-audit HIGH found
    _git had no timeout AND ran on the event loop, so one stall froze every worker + the janitor +
    all heartbeats until git returned (indefinitely if truly wedged) — and expired leases could then
    be reclaimed and DOUBLE-run. worker/pool now also run these off the loop (asyncio.to_thread), so
    this timeout is what actually frees the thread. Default 120s; $MYNDAIX_WORKTREE_GIT_TIMEOUT overrides."""
    v = os.environ.get("MYNDAIX_WORKTREE_GIT_TIMEOUT", "")
    try:
        n = int(v)                                    # int() (not v.isdigit(): that is True for Unicode
    except ValueError:                                # digits like '²' that int() REJECTS -> would escape)
        return 120
    return n if n > 0 else 120                         # "0"/"00"/negative -> 120: never disable the guard


def _git(args: list[str], cwd: str) -> str:
    # stdin=DEVNULL + GIT_TERMINAL_PROMPT=0: a credential/auth prompt is one of the wedge causes above;
    # this makes it fail INSTANTLY instead of blocking the full timeout (the timeout becomes a backstop,
    # not the primary defense), and stops git inheriting the parent's stdin (fresh-eyes review).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, timeout=_git_timeout(), stdin=subprocess.DEVNULL, env=env).stdout


class WorkspaceManager:
    """Creates, captures, and tears down ephemeral git worktrees per job."""

    def __init__(self, worktree_root: Optional[str] = None):
        # STABLE root (not per-process mkdtemp) so a restarted process can sweep a
        # crashed one's orphans. Explicit arg > $MYNDAIX_WORKTREE_ROOT > a fixed tmp path.
        root = (worktree_root or os.environ.get(_ROOT_ENV)
                or str(Path(tempfile.gettempdir()) / "mdx-worktrees"))
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def worktree_path(self, attempt_id: str) -> str:
        """The DETERMINISTIC path create() uses for this attempt_id. Lets a caller know the worktree
        path BEFORE create() runs — so cleanup can find it even if create is CANCELLED mid-run (the
        to_thread offload made create an await point, and a cancellation there would else leave the
        caller's worktree var unset while the thread still created the dir — oracle). Same wt-<attempt_id>
        naming sweep() correlates by."""
        return str(self.root / f"{_PREFIX}{attempt_id}")

    def create(self, repo_path: str, base_ref: str = "HEAD",
               attempt_id: Optional[str] = None) -> str:
        """git worktree add a fresh, isolated, detached checkout at base_ref.
        Returns the worktree path. The agent mutates only this directory. Naming
        by attempt_id lets sweep() correlate a leftover dir to its (closed) attempt;
        a random suffix is used only when no attempt_id is given (ad-hoc/tests)."""
        wt = (Path(self.worktree_path(attempt_id)) if attempt_id
              else self.root / f"{_PREFIX}{uuid.uuid4().hex[:12]}")
        _git(["worktree", "add", "--detach", str(wt), base_ref], cwd=repo_path)
        return str(wt)

    def capture_diff(self, worktree_path: str) -> Optional[str]:
        """Capture the worktree's changes (including new files) as a .patch
        artifact relative to its base ref. Returns the patch path, or None if
        nothing changed. This is the artifact_ref surfaced for review - it is
        NEVER auto-merged into the live tree."""
        _git(["add", "-A"], cwd=worktree_path)            # stage incl. untracked
        diff = _git(["diff", "--cached"], cwd=worktree_path)
        if not diff.strip():
            return None
        patch = self.root / f"{Path(worktree_path).name}.patch"
        patch.write_text(diff)
        return str(patch)

    def cleanup(self, repo_path: str, worktree_path: str) -> None:
        """Remove the worktree. Failure to deregister falls back to a hard rm -
        the live repo is never affected."""
        try:
            _git(["worktree", "remove", "--force", worktree_path], cwd=repo_path)
        except subprocess.SubprocessError:            # CalledProcessError OR TimeoutExpired (wedged git)
            shutil.rmtree(worktree_path, ignore_errors=True)

    def sweep(self, reapable_attempt_ids: Iterable[str]) -> int:
        """GC orphaned worktrees after a crash. Removes ONLY the wt-<attempt_id> dirs
        whose attempt is in reapable_attempt_ids — a FAIL-SAFE allowlist the caller (the
        ledger) defines as an attempt that is CLOSED and has stayed closed past a grace
        window. Anything else — an open lease, a just-closed attempt, or an unknown dir —
        is kept. Liveness is decided by ATTEMPT STATE, never directory mtime: an agent
        editing files in place does not refresh the worktree-root mtime, so an mtime-based
        guard would leave a long-running worktree unprotected. With this gate a worktree
        whose worker may still be writing (open, or a lease lost only moments ago) can
        never be reaped. Also drops the matching .patch artifact. Live repo untouched.

        Self-contained: each worktree's parent repo is recovered from its own `.git`
        pointer so the dir is properly deregistered, with a hard rmtree fallback."""
        if not self.root.exists():
            return 0
        reapable = set(reapable_attempt_ids)
        if not reapable:
            return 0
        removed = 0
        for wt in self.root.iterdir():
            if not wt.is_dir() or not wt.name.startswith(_PREFIX):
                continue
            if wt.name[len(_PREFIX):] not in reapable:
                continue                              # not provably-dead -> keep (fail-safe)
            repo = self._worktree_repo(wt)
            removed_ok = False
            if repo is not None:
                try:
                    _git(["worktree", "remove", "--force", str(wt)], cwd=repo)
                    removed_ok = True
                except subprocess.SubprocessError:     # CalledProcessError OR TimeoutExpired
                    pass
            if not removed_ok:
                shutil.rmtree(wt, ignore_errors=True)  # fallback: hard remove the orphan
                if repo is not None:
                    try:
                        _git(["worktree", "prune"], cwd=repo)   # drop the now-stale admin entry
                    except subprocess.SubprocessError:
                        pass
            patch = self.root / f"{wt.name}.patch"     # the captured-diff artifact, if any
            try:
                patch.unlink()
            except OSError:
                pass
            removed += 1
        return removed

    @staticmethod
    def _worktree_repo(wt: Path) -> Optional[str]:
        """Recover a worktree's main repo path from its `.git` pointer file
        (`gitdir: <repo>/.git/worktrees/<name>`), so sweep() can deregister it.
        Returns None if the pointer is missing/unexpected (then a hard rmtree is used)."""
        try:
            text = (wt / ".git").read_text().strip()
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        parts = Path(text.split(":", 1)[1].strip()).parts
        # match the EXACT '.git/worktrees' pair so a repo path containing a '.git'
        # component can't mis-parse; <repo> is everything before that '.git'.
        for i in range(len(parts) - 1):
            if parts[i] == ".git" and parts[i + 1] == "worktrees":
                return str(Path(*parts[:i])) if i > 0 else None
        return None
