"""C5 workspace-isolation tests - prove the headline safety property: a
workspace-actor agent's mutations never touch the live repo, its diff is
captured as an artifact, and concurrent agents don't collide. Local git only.

Run: PYTHONPATH=src python3 tests/test_workspace.py
"""
import subprocess
import tempfile
from pathlib import Path

from runtime.workspace import WorkspaceManager


def _init_repo() -> str:
    d = tempfile.mkdtemp(prefix="mdx-testrepo-")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    Path(d, "app.py").write_text("print('v1')\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    return d


def test_worktree_isolates_the_live_repo():
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    wt = wm.create(repo, "HEAD")

    # the "agent" mutates files in its worktree only
    Path(wt, "app.py").write_text("print('v2 - changed by the agent')\n")
    Path(wt, "new_file.txt").write_text("an artifact the agent produced\n")

    # the LIVE repo is untouched
    assert Path(repo, "app.py").read_text() == "print('v1')\n"
    assert not Path(repo, "new_file.txt").exists()

    # the change is captured as a reviewable artifact (not auto-merged)
    patch = wm.capture_diff(wt)
    assert patch and Path(patch).exists()
    diff = Path(patch).read_text()
    assert "v2 - changed by the agent" in diff
    assert "new_file.txt" in diff

    wm.cleanup(repo, wt)


def test_concurrent_worktrees_do_not_collide():
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    a = wm.create(repo, "HEAD")
    b = wm.create(repo, "HEAD")

    # two agents write conflicting content - in isolation
    Path(a, "app.py").write_text("AGENT-A\n")
    Path(b, "app.py").write_text("AGENT-B\n")

    assert Path(a, "app.py").read_text() == "AGENT-A\n"
    assert Path(b, "app.py").read_text() == "AGENT-B\n"      # no collision
    assert Path(repo, "app.py").read_text() == "print('v1')\n"  # live untouched

    wm.cleanup(repo, a)
    wm.cleanup(repo, b)


def test_no_changes_yields_no_artifact():
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    wt = wm.create(repo, "HEAD")
    assert wm.capture_diff(wt) is None     # nothing changed -> nothing to surface
    wm.cleanup(repo, wt)


def test_sweep_removes_only_reapable():
    """PR-1c gate (fail-safe): sweep removes ONLY worktrees whose attempt is in the
    reapable allowlist (the ledger's 'closed past the grace window' set); a worktree not
    in the set — an open or just-closed lease — is kept. Live repo untouched."""
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    orphan = wm.create(repo, "HEAD", attempt_id="orphan-1")
    live = wm.create(repo, "HEAD", attempt_id="live-1")
    removed = wm.sweep(reapable_attempt_ids={"orphan-1"})   # only orphan-1 is provably dead
    assert removed == 1
    assert not Path(orphan).exists(), "reapable orphan should be swept"
    assert Path(live).exists(), "a worktree not in the reapable set must be kept"
    assert Path(repo, "app.py").read_text() == "print('v1')\n"      # live repo untouched
    listing = subprocess.run(["git", "worktree", "list"], cwd=repo,
                             capture_output=True, text=True, check=True).stdout
    assert "orphan-1" not in listing and "live-1" in listing


def test_sweep_keeps_everything_when_nothing_reapable():
    """An empty reapable set (the common case — no crash orphans) reaps nothing, even
    for a worktree with no live attempt: liveness is the caller's call, not mtime's."""
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    wt = wm.create(repo, "HEAD", attempt_id="some-1")
    assert wm.sweep(reapable_attempt_ids=set()) == 0 and Path(wt).exists()


def test_sweep_removes_patch_artifact():
    """Reaping a worktree also drops its captured-diff .patch so the shared root doesn't
    leak artifacts across restarts."""
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    wt = wm.create(repo, "HEAD", attempt_id="diffy-1")
    Path(wt, "app.py").write_text("print('changed')\n")
    patch = wm.capture_diff(wt)
    assert patch and Path(patch).exists()
    wm.sweep(reapable_attempt_ids={"diffy-1"})
    assert not Path(wt).exists() and not Path(patch).exists()


def test_sweep_fallback_hard_removes_when_repo_unrecoverable():
    """If a worktree's .git back-pointer is gone, sweep still hard-removes the orphan dir."""
    repo = _init_repo()
    wm = WorkspaceManager(tempfile.mkdtemp(prefix="mdx-wt-root-"))
    wt = wm.create(repo, "HEAD", attempt_id="broken-1")
    (Path(wt) / ".git").unlink()                                     # break the back-pointer
    removed = wm.sweep(reapable_attempt_ids={"broken-1"})
    assert removed == 1 and not Path(wt).exists()


def test_git_timeout_parsing():
    # core-audit HIGH: _git MUST carry a wall-clock timeout (a wedged git can't hang the pool). The
    # cap is env-tunable but "0"/garbage fall back to the 120s default — the guard is never disabled.
    import os
    import runtime.workspace as W
    saved = os.environ.get("MYNDAIX_WORKTREE_GIT_TIMEOUT")
    try:
        os.environ.pop("MYNDAIX_WORKTREE_GIT_TIMEOUT", None)
        assert W._git_timeout() == 120, "default is 120s"
        os.environ["MYNDAIX_WORKTREE_GIT_TIMEOUT"] = "45"
        assert W._git_timeout() == 45, "positive env override honored"
        os.environ["MYNDAIX_WORKTREE_GIT_TIMEOUT"] = "0"
        assert W._git_timeout() == 120, "'0' never disables the guard -> default"
        os.environ["MYNDAIX_WORKTREE_GIT_TIMEOUT"] = "garbage"
        assert W._git_timeout() == 120, "garbage -> default"
        os.environ["MYNDAIX_WORKTREE_GIT_TIMEOUT"] = "²"   # '²': isdigit() True but int() raises
        assert W._git_timeout() == 120, "Unicode digit that int() rejects -> default (not a crash)"
        os.environ["MYNDAIX_WORKTREE_GIT_TIMEOUT"] = "-5"
        assert W._git_timeout() == 120, "negative -> default"
    finally:
        if saved is None:
            os.environ.pop("MYNDAIX_WORKTREE_GIT_TIMEOUT", None)
        else:
            os.environ["MYNDAIX_WORKTREE_GIT_TIMEOUT"] = saved


def test_git_passes_timeout_to_subprocess():
    # _git MUST hand subprocess.run a timeout so a wedged git is KILLED, not hung forever (core-audit).
    import runtime.workspace as W
    repo = _init_repo()
    captured = {}
    real_run = W.subprocess.run

    def spy(*a, **kw):
        captured.update(kw)
        return real_run(*a, **kw)

    W.subprocess.run = spy
    try:
        W._git(["rev-parse", "HEAD"], cwd=repo)              # a normal fast op
        assert captured.get("timeout") == 120, \
            f"_git must pass timeout=120 to subprocess.run (got {captured.get('timeout')})"
    finally:
        W.subprocess.run = real_run


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
