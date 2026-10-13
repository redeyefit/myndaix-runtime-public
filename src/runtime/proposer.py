"""The S7 proposer — turns `ready` capture_candidate classes into skill-draft PRs (auto-capture's
acting rung). A separate, flag-gated launchd job (`ai.myndaix.proposer`); ONE bounded tick then
exits (not a daemon). NEVER promotes — it opens a `--draft` PR that rides the unchanged human-merge
gate (skills/** is automerge-denylisted); the human authoring the body IS the promotion.

Run one tick:
    MYNDAIX_DSN=... GH_TOKEN=... PYTHONPATH=src python3 -m runtime.proposer tick
Dry-run (decide + log, mutate NOTHING — no DB, no git, no gh):
    MYNDAIX_PROPOSER_DRY_RUN=1 ... python3 -m runtime.proposer tick

DESIGN: docs/auto-capture-design.md (v0.6 — cross-family hardened). The pure render/slug/path core
is runtime.capture; the S6 state-machine verbs are ledger.postgres_store. This file is the gh/git
side-effect driver (S5/S7). Safety properties, each earned by a cross-family finding:
  - A2  repo_scope resolved by EXACT-KEY lookup in the trusted repos.json MAP; the scope string is
        NEVER passed to a git/gh argv (a polluted/hostile scope → skip, never a wrong-repo PR).
  - K1  recovery/adoption is by BRANCH + authenticated BOT-AUTHOR identity, NEVER by content hash
        (hash-verify-before-adopt infinite-loops once a human edits the stub — oracle).
  - K2  adoption validates the ACTUAL PR (our repo, not a fork; single match) and the diff is the
        one permitted path (assert_only_skill_path on the real staged diff).
  - K3  the SKILL.md write is O_EXCL + O_NOFOLLOW with symlinked-ancestor rejection (a post-write
        assert cannot stop a symlink escape DURING the write).
  - A5  RECONCILE acts ONLY on definitive merged/closed; gh-unknown (None) → DEFER (never mis-decline).
  - A7  TTL close re-reads LIVE PR state first; merged always wins; a close is never mis-recorded.
  - A6  proposer-owned worktree GC at tick start (flock proves the prior tick is dead).
  - A9  DRY_RUN suppresses EVERY mutation (DB + git + gh) across all three passes.
  - K4  every un-authored draft carries capture.STUB_MARKER; the controller index + CI refuse it.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from runtime import capture
from runtime.ledger.postgres_store import PostgresLedger

# -- config (all knobs via env; STRICT digit-only parse so a bad launchd value defaults, not crash) --
DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
ORCH = HOME / ".myndaix" / "orchestrator"
STATE = ORCH / "state"
REPOS_JSON = Path(os.environ.get("MYNDAIX_REPOS_JSON", str(ORCH / "repos.json")))
LOCK = ORCH / "proposer.lock"
ENABLED_FLAG = ORCH / "PROPOSER_ENABLED"
WORKTREE_ROOT = Path(os.environ.get("MYNDAIX_PROPOSER_WORKTREES", str(STATE / "proposer-worktrees")))
CURSOR_FILE = STATE / "proposer-cursor"       # last-visited fingerprint (fairness keyset, r2 #7)
SUSPECTS_FILE = STATE / "proposer-suspects.json"   # unresolved unknown-create outcomes (ff-r3 M2b)
BOT_NAME = os.environ.get("MYNDAIX_PROPOSER_BOT_NAME", "myndaix-proposer")
BOT_EMAIL = os.environ.get("MYNDAIX_PROPOSER_BOT_EMAIL", "proposer@myndaix.local")


def _int_env(name: str, default: int) -> int:
    """STRICT digit-only env knob (mirrors automerge/controller): a malformed launchd value defaults
    rather than crashing the service at import; caps to avoid Python's int-str limit."""
    val = os.environ.get(name, "")
    if not re.fullmatch(r"[0-9]+", val):
        return default
    val = val.lstrip("0") or "0"
    return 2**31 - 1 if len(val) > 10 else min(int(val), 2**31 - 1)


def _th(name: str) -> int:
    """A capture threshold from $CAPTURE_<NAME>, else the pure-core default (config, never a rewrite)."""
    return _int_env(f"CAPTURE_{name}", capture.DEFAULTS[name])


MAX_OPEN = lambda: _th("MAX_OPEN")            # re-read per tick so a live env change takes effect
TTL_DAYS = lambda: _th("TTL_DAYS")
MAX_PER_TICK = _int_env("MYNDAIX_PROPOSER_MAX_TICK", 2)      # K6: bound proposals opened per tick
PROBE_CAP = _int_env("MYNDAIX_PROPOSER_PROBE_CAP", 6)        # gh PR-lookup reads per tick (K6 bound),
                                                             # SHARED by suspect resolution + the ready
                                                             # scan and floored to MAX_OPEN+1 at tick
                                                             # start (a suspect head must not starve
                                                             # adoption); the fairness cursor rotates
                                                             # who gets probed
REAP_TIMEOUT_MIN = _int_env("MYNDAIX_PROPOSER_REAP_MIN", 30)  # release a 'proposing' row stuck this long
GH_TIMEOUT = _int_env("MYNDAIX_PROPOSER_GH_TIMEOUT", 60)
GIT_TIMEOUT = _int_env("MYNDAIX_PROPOSER_GIT_TIMEOUT", 120)
PUSH_REVERIFY_PAUSE_S = _int_env("MYNDAIX_PROPOSER_PUSH_REVERIFY_PAUSE", 2)  # spacing between the
                                                             # two failed-push ls-remote reads: one
                                                             # read right after a failed push races
                                                             # server-side ref application
DRY_RUN = os.environ.get("MYNDAIX_PROPOSER_DRY_RUN") == "1"

_PR_URL_RE = re.compile(r"/pull/(\d+)\s*$")   # `gh pr create` prints the PR URL, NOT json (K6)


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [proposer]{' DRY' if DRY_RUN else ''} {msg}", file=sys.stderr, flush=True)


# =====================================================================================
# I/O: git + gh — all argv (never shell), output validated. Mirrors automerge's env scrub.
# =====================================================================================
def _git_env() -> dict:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:ssh:file",
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# EVERY proposer git op runs with hooks DISABLED (kilabz BLOCKER + MAJOR, one fix): (a) a tracked
# checkout's hooks (core.hooksPath / .git/hooks) execute during worktree-add/commit/push and inherit
# GH_TOKEN — a hostile hook exfiltrates the bot credential or stages files AFTER the path assert;
# (b) the orchestrator's OWN pre-push hook would fire play-review from the ephemeral worktree path,
# which the proposer then deletes mid-review. /dev/null is not a directory, so git finds no hooks.
# The PR + human merge is the review gate for a proposal; the pre-push review is not wanted here.
_GIT_NOHOOKS = ("-c", "core.hooksPath=/dev/null")


def _git(cwd: Path, *args: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), "--no-pager", *_GIT_NOHOOKS, *args],
                          capture_output=True, text=True, env=_git_env(),
                          timeout=timeout or GIT_TIMEOUT, check=False)


def _gh_json(nwo: str, *args: str):
    """A gh command scoped to --repo <nwo> that prints JSON → parsed value, or None on ANY failure
    (nonzero exit, timeout, non-JSON). None means UNKNOWN — callers must DEFER, never decide (A5)."""
    try:
        r = subprocess.run(["gh", *args, "--repo", nwo], capture_output=True, text=True,
                           env=_git_env(), timeout=GH_TIMEOUT, check=False)
    except subprocess.SubprocessError as e:
        log(f"gh {' '.join(args)}: {e!r} — unknown"); return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _gh_close(nwo: str, pr_number: int) -> bool:
    """`gh pr close` prints NO JSON, so success = exit 0 (NOT _gh_json, whose None conflates a
    successful non-JSON mutation with failure — oracle code-review BLOCKER: a failed close that
    still records 'stale' orphans a live OPEN PR). Callers gate the DB flip on this."""
    try:
        r = subprocess.run(["gh", "pr", "close", str(int(pr_number)), "--repo", nwo],
                           capture_output=True, text=True, env=_git_env(),
                           timeout=GH_TIMEOUT, check=False)
    except subprocess.SubprocessError as e:
        log(f"gh pr close #{pr_number}: {e!r} — unknown"); return False
    if r.returncode != 0:
        log(f"gh pr close #{pr_number} failed: {r.stderr.strip()[:150]}")
    return r.returncode == 0


# -- the trusted repo allowlist MAP (A2): repo_scope → {path, nwo}, exact-key only ---------------
def resolve_repo(repo_scope: str) -> Optional[dict]:
    """EXACT-KEY lookup of `repo_scope` in repos.json → the local path + the nwo resolved FROM THAT
    PATH (never from repo_scope). A scope not on the list, or whose path is not a real git repo, or
    whose nwo can't be resolved → None (fail-closed: a polluted/hostile scope can never open a PR
    against an unintended repo). repo_scope NEVER reaches a git/gh argv."""
    try:
        raw = json.loads(REPOS_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f"repos.json unreadable ({e})"); return None
    entry = raw.get(repo_scope) if isinstance(raw, dict) else None
    if not isinstance(entry, dict) or repo_scope.startswith("_"):
        return None                                  # unknown / non-repo scope → skip
    p = entry.get("path")
    if not p:
        return None
    path = Path(p).expanduser().resolve()
    if not (path.is_dir() and (path / ".git").exists()):
        return None
    # ONE comma-joined --json field list (kilabz BLOCKER: as two argv items, the second becomes
    # gh's positional REPOSITORY argument — dref never returns and every repo resolves to None).
    info = _gh_json_path(path, "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
    nwo = info.get("nameWithOwner") if isinstance(info, dict) else None
    dref = (info.get("defaultBranchRef") or {}).get("name") if isinstance(info, dict) else None
    if not (isinstance(nwo, str) and "/" in nwo and isinstance(dref, str) and dref):
        log(f"{repo_scope}: cannot resolve nwo/defaultBranch via gh — skip"); return None
    return {"path": path, "nwo": nwo, "default_branch": dref}


def _gh_json_path(path: Path, *args: str):
    """gh JSON run with cwd=<repo path> (for `repo view` which infers the repo from cwd)."""
    try:
        r = subprocess.run(["gh", *args], cwd=str(path), capture_output=True, text=True,
                           env=_git_env(), timeout=GH_TIMEOUT, check=False)
    except subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


# -- single-instance flock (mirror automerge) -----------------------------------------------------
_LOCK_FD: Optional[int] = None


def acquire_lock() -> bool:
    global _LOCK_FD
    ORCH.mkdir(parents=True, exist_ok=True)
    if LOCK.is_dir():
        shutil.rmtree(LOCK, ignore_errors=True)
    fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd); log("another tick holds the lock — exiting"); return False
    _LOCK_FD = fd
    return True


def release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN); os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


# -- worktree GC (A6): the flock proves the prior tick is dead, so any leftover worktree is safe to
#    remove. rmtree the dirs, then `git worktree prune` EVERY allowlisted repo so the stale admin
#    entries a bare rmtree leaves are cleared in the same GC — independent of whether the propose
#    loop later selects a candidate for that repo (kilabz r2 #12), and strictly under the DRY_RUN
#    guard (kilabz r2 #1: prune is a git mutation; A9 forbids it in a dry tick).
def gc_worktrees() -> None:
    if DRY_RUN:
        return
    shutil.rmtree(WORKTREE_ROOT, ignore_errors=True)
    try:
        raw = json.loads(REPOS_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(raw, dict):
        return
    for key, entry in raw.items():
        # per-entry boundary (kilabz r3 MAJOR): a malformed path (TypeError) or a wedged prune
        # (TimeoutExpired) in ONE allowlist entry must never abort the whole tick before the ledger
        # connects — log + continue the sweep.
        try:
            if key.startswith("_") or not isinstance(entry, dict):
                continue
            p = entry.get("path")
            if not isinstance(p, str) or not p:
                continue
            path = Path(p).expanduser().resolve()
            if path.is_dir() and (path / ".git").exists():
                _git(path, "worktree", "prune")
        except Exception as e:
            log(f"gc prune for {key!r} raised ({e!r}) — continue sweep")


# -- K3: symlink-safe, creation-only write of skills/<slug>/SKILL.md inside the worktree ----------
def _safe_write_skill(wt: Path, slug: str, rendered: str) -> bool:
    """Write ONLY skills/<slug>/SKILL.md via DIRECTORY DESCRIPTORS with O_NOFOLLOW at EVERY
    component (K3 + kilabz race fix: a static pre-check can be swapped for a symlink between the
    check and the open — O_NOFOLLOW on the final component alone doesn't cover ancestors; openat
    against a pinned dir-fd does). An existing slug dir/target is refused (creation-only — enforces
    the v1 'never edit an existing skill' promise). Returns True on a clean write."""
    fds: list[int] = []
    try:
        try:
            wt_fd = os.open(str(wt), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as e:
            log(f"open worktree dir failed ({e}) — refuse"); return False
        fds.append(wt_fd)
        try:
            os.mkdir("skills", dir_fd=wt_fd)
        except FileExistsError:
            pass                                   # tracked skills/ dir already in the checkout
        except OSError as e:
            log(f"mkdir skills/ failed ({e}) — refuse"); return False
        try:                                       # ELOOP here = skills/ is a symlink → refuse
            skills_fd = os.open("skills", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=wt_fd)
        except OSError as e:
            log(f"skills/ is not a real dir in the worktree ({e}) — refuse write"); return False
        fds.append(skills_fd)
        try:
            os.mkdir(slug, dir_fd=skills_fd)       # creation-only: an existing slug dir refuses
        except OSError as e:
            log(f"skills/{slug} already exists or mkdir failed ({e}) — refuse (creation-only)"); return False
        try:
            slug_fd = os.open(slug, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skills_fd)
        except OSError as e:
            log(f"skills/{slug} vanished/replaced after mkdir ({e}) — refuse"); return False
        fds.append(slug_fd)
        try:
            fd = os.open("SKILL.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=slug_fd)
        except OSError as e:
            log(f"O_EXCL open of SKILL.md failed ({e}) — refuse"); return False
        try:
            os.write(fd, rendered.encode())
        finally:
            os.close(fd)
        return True
    finally:
        for f in fds:
            try:
                os.close(f)
            except OSError:
                pass


def _staged_paths(wt: Path) -> list[str]:
    """The NUL-delimited names git actually staged (A10: assert on git's report, not the intended
    string). rename-detection is off (diff --name-only doesn't rename-detect by default here)."""
    r = _git(wt, "diff", "--cached", "--name-only", "-z")
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.split("\0") if p]


def _make_proposal_commit(repo: dict, wt_name: str, slug: str, rendered: str) -> Optional[Path]:
    """git worktree add off the default branch, safe-write the SKILL.md, assert ONLY that path
    changed (S1/K2), commit. Returns the worktree path on success (caller pushes + opens the PR),
    else None (caller releases the claim)."""
    path = repo["path"]
    _git(path, "worktree", "prune")                              # clear stale admin entries (A6)
    fetch = _git(path, "fetch", "--no-tags", "origin", repo["default_branch"], timeout=GIT_TIMEOUT)
    if fetch.returncode != 0:                                    # a silent stale base is a wrong-diff PR
        log(f"fetch origin/{repo['default_branch']} failed: {fetch.stderr.strip()[:150]}"); return None
    wt = WORKTREE_ROOT / wt_name
    shutil.rmtree(wt, ignore_errors=True)
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    add = _git(path, "worktree", "add", "--detach", str(wt), f"origin/{repo['default_branch']}")
    if add.returncode != 0:
        log(f"worktree add failed: {add.stderr.strip()[:200]}"); return None
    ok = _safe_write_skill(wt, slug, rendered)
    if not ok:
        _git(path, "worktree", "remove", "--force", str(wt)); return None
    _git(wt, "add", "--", capture.skill_path(slug))
    staged = _staged_paths(wt)
    if not capture.assert_only_skill_path(staged, slug):          # S1/K2: exactly skills/<slug>/SKILL.md
        log(f"staged diff is not exactly {capture.skill_path(slug)} (got {staged}) — abort")
        _git(path, "worktree", "remove", "--force", str(wt)); return None
    commit = _git(wt, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
                  "-c", "commit.gpgsign=false",
                  "commit", "-m", f"skill(auto): propose {slug} (unauthored stub — review before merge)")
    if commit.returncode != 0:
        log(f"commit failed: {commit.stderr.strip()[:200]}"); _git(path, "worktree", "remove", "--force", str(wt)); return None
    # belt: verify the FINAL COMMITTED change set, not just the pre-commit staging (kilabz: with
    # hooks disabled this should never differ, but the push gate must not rest on that assumption)
    show = _git(wt, "show", "--name-only", "--format=", "-z", "HEAD")
    committed = [p for p in show.stdout.split("\0") if p]
    if show.returncode != 0 or not capture.assert_only_skill_path(committed, slug):
        log(f"COMMITTED change set is not exactly {capture.skill_path(slug)} (got {committed}) — abort")
        _git(path, "worktree", "remove", "--force", str(wt)); return None
    return wt


def _remote_branch_exists(repo: dict, branch: str) -> Optional[bool]:
    """Does refs/heads/<branch> exist on origin? True/False, or None (lookup failed → treat as
    unknown, fail-closed). The create path REFUSES to push when the branch already exists with no
    open bot PR (inbox synthesis P1: a human can close the bot's draft and keep content on the
    deterministic branch name — an overwrite push would erase it; the flock can't serialize
    GitHub-side edits). A leftover from a crashed push-before-create — or a human closure that
    kept its branch — hits the same refusal LOUDLY; deleting the branch unwedges re-proposal.

    Queries the FULL ref and exact-compares the returned ref name: a bare pattern to `ls-remote`
    is SUFFIX-matched by git (an existing `archive/skill/auto/x` would block absent `skill/auto/x`)
    and substring-checking stdout was looser still (kilabz ff-review MINOR)."""
    ref = f"refs/heads/{branch}"
    r = _git(repo["path"], "ls-remote", "origin", ref)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == ref:
            return True
    return False


def _remote_ref_sha(repo: dict, branch: str) -> tuple[str, Optional[str]]:
    """The sha origin holds for refs/heads/<branch>: ("present", sha) | ("absent", None) |
    ("unknown", None). Same FULL-ref + exact-compare discipline as _remote_branch_exists (a bare
    pattern is SUFFIX-matched by ls-remote). Unlike _remote_branch_exists this NEVER raises —
    `_git` can raise TimeoutExpired and this helper is used inside never-raises paths (do not
    refactor _remote_branch_exists onto it: silently converting the create-path's raise into a
    defer is a separate deliberate change)."""
    ref = f"refs/heads/{branch}"
    try:
        r = _git(repo["path"], "ls-remote", "origin", ref)
    except Exception:
        return ("unknown", None)
    if r.returncode != 0:
        return ("unknown", None)
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == ref and parts[0]:
            return ("present", parts[0])
    return ("absent", None)


def _create_pr_for_branch(repo: dict, branch: str, slug: str) -> tuple[str, Optional[int]]:
    """`gh pr create` for an ALREADY-PUSHED remote head — shared by the normal push path and the
    stranded-branch recovery (which must NOT push: the no-overwrite rule stays intact). NEVER
    RAISES; ("opened", n) | ("unknown", None) — after entry a PR may exist whose response was
    lost, so every failure arm is unknown, never definitive."""
    title = f"skill(auto): {slug} — recurring review finding (review before merge)"
    body = (f"Auto-proposed by the S7 proposer from a recurring `rule:{slug}` finding. This is an "
            f"UNAUTHORED STUB — fill in the real lesson (see the provenance commits in the diff) and "
            f"delete the `{capture.STUB_MARKER}` line before merging. Not indexed until authored.")
    try:
        r = subprocess.run(["gh", "pr", "create", "--repo", repo["nwo"], "--draft",
                            "--base", repo["default_branch"], "--head", branch,
                            "--title", title, "--body", body],
                           capture_output=True, text=True, env=_git_env(), timeout=GH_TIMEOUT, check=False)
    except Exception as e:                        # SubprocessError, UnicodeDecodeError, anything:
        log(f"gh pr create raised ({e!r}) — outcome UNKNOWN, reserve + recover next tick"); return ("unknown", None)
    if r.returncode != 0:
        log(f"gh pr create failed: {r.stderr.strip()[:200]} — outcome unknown"); return ("unknown", None)
    m = _PR_URL_RE.search((r.stdout or "").strip())
    if not m:
        log(f"gh pr create gave no parseable PR URL ({r.stdout.strip()[:120]}) — unknown"); return ("unknown", None)
    return ("opened", int(m.group(1)))


def _push_and_open_pr(repo: dict, wt: Path, branch: str, slug: str) -> tuple[str, Optional[int]]:
    """Push the branch and open a --draft PR. Outcome states (the caller accounts conservatively —
    kilabz ff-review MAJOR: an unknown create may have opened a REAL PR whose response was lost,
    and MAX_OPEN must reserve for it):
      ("opened", n)   — PR confirmed open (number parsed from the create URL; K6: create prints a
                        URL, not JSON).
      ("no-pr", None) — the push is VERIFIED not to have landed: the remote ref read absent on TWO
                        spaced reads. NOT "the push command exited nonzero" — a network drop AFTER
                        the server applied the ref also exits nonzero (128), so the exit code alone
                        never proves absence (P1 fix). The caller refunds + releases but KEEPS the
                        write-ahead suspect: a ref applied server-side after both reads still
                        converges via the next tick's resolution instead of wedging.
      ("no-pr-foreign", None) — remote ref present at a sha that is NOT ours: our PLAIN no-force
                        push cannot have landed, so definitively nothing of OURS exists (the caller
                        clears the intent; the branch-exists guard then protects the foreign
                        content — that refusal is the deliberate human-content protection).
      ("unknown", None) — outcome uncertain (raise/timeout/nonzero create/unparseable/failed
                        re-verify): a PR or ref may exist untracked; the caller keeps the suspect
                        + the MAX_OPEN reservation and the next tick re-resolves.
    When the failed push's re-verify finds the ref at OUR HEAD sha, the push succeeded — PROCEED
    to `gh pr create` (returning unknown instead re-wedges: the kept suspect clears on the none-PR
    lookup next tick and the branch-exists guard then refuses forever).
    Push is PLAIN (no --force): the caller verified the branch's absence just before; a branch
    racing into existence in that gap is rejected non-fast-forward (or, if it points at an
    ancestor of our commit, fast-forwards losslessly — its commit stays in our history).

    NEVER RAISES (ff-r5 P1: an escaping exception — a push TimeoutExpired, a UnicodeDecodeError
    from output decoding — bypassed the caller's outcome accounting): any unexpected error after
    entry is 'unknown' (the push may have gone through), reported as a value, not an exception."""
    try:
        push = _git(wt, "push", "origin", f"HEAD:refs/heads/{branch}")
        if push.returncode != 0:
            # P1 fix: exit 128 covers both rejection and transport loss after the server applied
            # the ref — re-verify the remote ref before declaring anything. Any raise below falls
            # to the outer except (unknown), keeping the never-raises contract.
            head = _git(wt, "rev-parse", "HEAD")
            ours = head.stdout.strip() if head.returncode == 0 else None
            state, rsha = _remote_ref_sha(repo, branch)
            log(f"push failed ({push.stderr.strip()[:150]}) — re-verify read1: {state}"
                f"{'@' + rsha[:12] if rsha else ''}, our HEAD {ours[:12] if ours else 'unknown'}")
            if state == "absent":
                # absence observed immediately after a failed push is evidence about NOW, not the
                # push's final server-side outcome (receive-pack can apply the ref after the
                # client drop; read replicas lag) — 'absent' is definitive only on TWO spaced
                # consistent reads. Residual: lag longer than the pause still converges to the
                # loud REFUSING refusal next tick, strictly narrower than the pre-fix wedge.
                time.sleep(PUSH_REVERIFY_PAUSE_S)
                state, rsha = _remote_ref_sha(repo, branch)
                log(f"re-verify read2: {state}{'@' + rsha[:12] if rsha else ''}")
                if state == "absent":
                    return ("no-pr", None)
            if state == "present" and ours and rsha == ours:
                log("push reported failure but the remote ref is at OUR commit — the push landed; "
                    "proceeding to PR create")
            elif state == "present" and ours:
                return ("no-pr-foreign", None)
            else:
                # lookup unknown, or present with our HEAD unreadable (cannot compare) — a push
                # that truly failed re-verifies branch-absent next tick and retries cleanly.
                return ("unknown", None)
    except Exception as e:
        log(f"push raised ({e!r}) — outcome UNKNOWN"); return ("unknown", None)
    return _create_pr_for_branch(repo, branch, slug)


def _find_open_bot_pr(repo: dict, branch: str) -> tuple[str, Optional[int]]:
    """K1/K2 recovery: is there already an OPEN PR that WE (the bot) opened for this exact branch in
    OUR repo (not a fork)? Scoped by `--author @me`; validates head is same-repo. TRI-STATE return
    (kilabz MAJOR: a single None conflated lookup-failure/ambiguity/absence, and the caller treated
    all three as permission to create — after a crash-before-mark, a transient lookup failure would
    force-push a fresh stub OVER a human-edited PR branch):
      ("found", n)     — exactly one same-repo bot PR: adopt it.
      ("none", None)   — DEFINITIVE absence (successful empty lookup): creating is safe.
      ("unknown", None)— lookup failed (gh down/rate-limited): DEFER, never create.
      ("ambiguous", None) — multi-match: DEFER for a human, never pick-first, never create."""
    rows = _gh_json(repo["nwo"], "pr", "list", "--head", branch, "--state", "open",
                    "--author", "@me", "--json", "number,isCrossRepository,headRefName")
    if not isinstance(rows, list):
        return ("unknown", None)
    mine = [r for r in rows if isinstance(r, dict) and r.get("isCrossRepository") is False
            and r.get("headRefName") == branch and isinstance(r.get("number"), int)]
    if len(mine) == 1:
        return ("found", mine[0]["number"])
    if len(mine) > 1:
        log(f"ambiguous open bot PRs for {branch} ({[r['number'] for r in mine]}) — defer")
        return ("ambiguous", None)
    return ("none", None)


# =====================================================================================
# the three passes
# =====================================================================================
async def reconcile(led) -> None:
    """proposed → promoted|declined|stale by RE-READING each PR's LIVE state (A5/A7). gh-unknown →
    DEFER. merged always wins. TTL close only for a still-OPEN PR past TTL, and only after the read.
    Per-candidate exception boundary (oracle code-review MAJOR): one bad row must defer, never abort
    the loop and starve the propose() phase behind it."""
    for c in await led.list_proposed():
        try:
            repo = resolve_repo(c["repo_scope"])
            if repo is None:
                log(f"proposed {c['rule_tag']} scope {c['repo_scope']!r} unresolvable — defer"); continue
            pr = _gh_json(repo["nwo"], "pr", "view", str(c["pr_number"]), "--json", "state,mergedAt")
            if not isinstance(pr, dict):
                continue                                          # A5: unknown → defer
            merged = bool(pr.get("mergedAt")) or pr.get("state") == "MERGED"
            outcome = None
            if merged:
                outcome = "promoted"
            elif pr.get("state") == "CLOSED":
                outcome = "declined"
            elif pr.get("state") == "OPEN" and _age_days(c.get("proposed_at")) > TTL_DAYS():
                if DRY_RUN:
                    log(f"would TTL-close+stale PR#{c['pr_number']} ({c['rule_tag']})"); continue
                # the close must be CONFIRMED before the DB flip (oracle BLOCKER: an unchecked
                # failed close + 'stale' write orphans a live OPEN PR the ledger no longer tracks).
                # `gh pr close` prints no JSON, so _gh_close's success = exit 0.
                if not _gh_close(repo["nwo"], c["pr_number"]):
                    log(f"TTL close of PR#{c['pr_number']} failed — defer to next tick"); continue
                outcome = "stale"
            if outcome is None:
                continue
            if DRY_RUN:
                log(f"would resolve {c['rule_tag']} → {outcome} (PR#{c['pr_number']})"); continue
            await led.resolve_capture(c["fingerprint"], outcome)
            log(f"resolved {c['rule_tag']} → {outcome} (PR#{c['pr_number']})")
        except Exception as e:                                    # defer this row, keep the tick alive
            log(f"reconcile {c.get('rule_tag')} raised ({e!r}) — defer + continue")


# -- orphan suspects (ff-r3 M2b): an unknown `gh pr create` outcome may be a REAL open PR the
#    ledger doesn't track. The suspect is PERSISTED (fingerprint+scope+branch) independently of the
#    fairness cursor — kilabz fault-injection proved every cursor-based reservation dies at a tick
#    boundary (a newly-ready candidate can sort between the cursor and the suspect, so "resume at
#    the suspect" is not guaranteed). Each tick resolves suspects FIRST, before any creation:
#    found → adopt (pre-capacity, it re-tracks an existing PR); none + remote branch ABSENT → drop
#    (nothing was created); none + remote branch PRESENT → the durable write-ahead intent proves
#    the stranded push is OURS — recover by opening the PR for the existing head, NO push (attack
#    pass: dropping here left the branch-exists guard refusing forever); unknown → keep AND
#    reserve a MAX_OPEN slot while the uncertainty persists. CLEAR-INVARIANT: a suspect may be
#    cleared only when its class is TRACKED, when BOTH no open bot PR AND no remote branch exist,
#    or on positive foreign-branch evidence (no-pr-foreign). SINGLE-MACHINE INVARIANT: the
#    suspects file + the "no concurrent writer" reasoning behind the terminal-drop arm rest on
#    the tick flock, which is MACHINE-local while the ledger is shared — arm the proposer on ONE
#    machine only (a second armed machine needs its own design pass, not a bigger lock).
def _load_suspects() -> tuple[list[dict], bool]:
    """(suspects, healthy). A MISSING file is healthy-empty; a DAMAGED/unreadable one returns
    ([], False) and the caller must NOT overwrite it and must DISABLE creation for the tick —
    the unreadable content may hold live reservations (kilabz ff-r4 P1: treating corruption as
    empty let the resolution pass overwrite reservations with [])."""
    try:
        raw = json.loads(SUSPECTS_FILE.read_text())
    except FileNotFoundError:
        return [], True
    except (ValueError, OSError):      # ValueError covers JSONDecodeError AND UnicodeDecodeError
        return [], False               # (ff-r5: invalid UTF-8 escaped and aborted before adoption)
    if not isinstance(raw, list):
        return [], False
    # STRICT whole-schema validation (ff-r5 P1): a single malformed record — [null], a dict missing
    # fingerprint/repo_scope/branch — marks the WHOLE file unhealthy. Filtering it away would
    # silently discard what may be a real (corrupted) reservation and then overwrite the evidence.
    for s in raw:
        if not (isinstance(s, dict)
                and all(isinstance(s.get(k), str) and s.get(k)
                        for k in ("fingerprint", "repo_scope", "branch"))):
            return [], False
    return list(raw), True


def _save_suspects(suspects: list[dict]) -> bool:
    # Durability order is load-bearing (fsync P1): the file DATA must reach stable media BEFORE
    # the rename is issued — a rename journaled durable ahead of cache-only data yields a durable
    # rename to unwritten bytes: an empty/truncated file post-crash, which _load_suspects treats
    # as DAMAGED and which wedges creation EVERY tick with no self-heal (unlike a lost rename,
    # arm (a), which the K1 adoption probe eventually re-tracks). macOS os.fsync stops at the
    # drive cache, so the FILE fd takes F_FULLFSYNC (stable media; arm (b) does not self-heal),
    # falling back to os.fsync where unavailable/refused (non-APFS test filesystems); the DIR
    # fsync after the rename makes the write-ahead intent itself survive the crash the suspects
    # file exists for. All inside the try: an undurable write returns False and _add_suspect's
    # caller refuses to create (ff-r4 P1 contract).
    try:
        SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SUSPECTS_FILE.with_suffix(".tmp")
        with tmp.open("w") as f:
            f.write(json.dumps(suspects))
            f.flush()
            full = getattr(fcntl, "F_FULLFSYNC", None)
            if full is not None:
                try:
                    fcntl.fcntl(f.fileno(), full)
                except OSError:
                    os.fsync(f.fileno())
            else:
                os.fsync(f.fileno())
        tmp.replace(SUSPECTS_FILE)                # atomic on APFS
        dfd = os.open(str(SUSPECTS_FILE.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return True
    except OSError as e:
        log(f"suspects write failed ({e}) — reservation may not survive a crash this tick")
        return False


def _add_suspect(fingerprint: str, repo_scope: str, branch: str) -> bool:
    """WRITE-AHEAD intent (kilabz ff-r4 P1): called BEFORE the create side effect; False (damaged
    file or failed write) means the caller must REFUSE to create — no durable intent, no PR."""
    suspects, healthy = _load_suspects()
    if not healthy:
        return False                              # never rewrite a damaged file
    if not any(s["fingerprint"] == fingerprint for s in suspects):
        suspects.append({"fingerprint": fingerprint, "repo_scope": repo_scope, "branch": branch})
    return _save_suspects(suspects)


def _clear_suspect(fingerprint: str) -> None:
    """Drop a DEFINITIVELY-resolved intent/suspect (opened+marked, adopted, foreign-branch, or
    verified nothing-exists). Best-effort: a failed clear leaves a stale suspect, which the next
    tick's resolution recognizes as already-tracked (or verified-absent) and clears — never a
    double reservation (ff-r4 P2). Skips the save when nothing was removed: _save_suspects is a
    2-fsync durable write now, and every successful adoption calls this for non-suspects too."""
    suspects, healthy = _load_suspects()
    if not healthy:
        return
    kept = [s for s in suspects if s.get("fingerprint") != fingerprint]
    if len(kept) != len(suspects):
        _save_suspects(kept)


def _notify_jefe(subject: str, body: str) -> None:
    """Best-effort drop into the jefe verdict inbox (~/.myndaix/bridge/inbox/jefe/ — the one live
    bridge path). Used ONLY when a suspect is dropped whose orphan (open PR / stranded branch)
    now needs HUMAN disposition: the residual is permanent until that disposition and stacks per
    orphan, so launchd stderr alone is not an acceptable channel. Never raises; a failed write
    degrades to the log line the caller already emitted."""
    if DRY_RUN:
        return
    try:
        inbox = HOME / ".myndaix" / "bridge" / "inbox" / "jefe"
        inbox.mkdir(parents=True, exist_ok=True)
        name = f"proposer-{time.strftime('%Y%m%d%H%M%S')}-{time.time_ns() % 1_000_000}.md"
        (inbox / name).write_text(f"# {subject}\n\n{body}\n")
    except OSError:
        pass


def _age_days(ts) -> float:
    if ts is None:
        return 0.0
    try:
        import datetime as _dt
        now = _dt.datetime.now(tz=ts.tzinfo) if getattr(ts, "tzinfo", None) else _dt.datetime.now()
        return (now - ts).total_seconds() / 86400.0
    except Exception:
        return 0.0


async def propose(led) -> None:
    """Take each ready class AT MOST ONCE (K6): resolve repo (A2), render the stub (K4 marker),
    LOOK UP an existing bot PR (K1), then adopt or create. The lookup runs BEFORE the claim and
    BEFORE the capacity gate (inbox-synthesis P1: an untracked PR from an unknown create outcome
    could never be adopted once tracked PRs hit MAX_OPEN, because the old loop broke at capacity
    before the adopt path ran — real open PRs then exceed MAX_OPEN forever). Adoption converts an
    untracked PR into a tracked one — it RESTORES the accounting, so it must never be capacity-
    gated; only CREATION is. Lookups are reads (safe pre-claim under the single-host flock) and
    are bounded by the shared per-tick probe budget (PROBE_CAP floored to MAX_OPEN+1, consumed by
    suspect resolution first); the fairness cursor rotates which candidates get probed.
    `attempts` counts CREATE ATTEMPTS (not confirmed opens): an unknown gh outcome may have opened
    a PR whose response was lost, so the budget must burn on the attempt (kilabz MAJOR)."""
    n_open = await led.count_open_proposals()
    opened = 0
    attempts = 0
    probes = 0
    seen: set = set()
    reserved_fps: set = set()      # fingerprints whose n_open slot is a SUSPECT RESERVATION — an
                                   # adopt/recovery for one of these CONVERTS the slot in place
                                   # (a bare second increment double-counted one real PR and
                                   # starved the tick); only verified nothing-exists refunds it
    # PROBE FLOOR (attack pass): PROBE_CAP and MAX_OPEN are INDEPENDENT operator knobs; with
    # PROBE_CAP <= MAX_OPEN a full suspect head could consume every probe, every tick — starving
    # the ready scan and with it ADOPTION, the path every accounting self-heal relies on. Floor
    # the budget so the scan always keeps at least one probe past a full suspect head.
    effective_probe_cap = max(PROBE_CAP, MAX_OPEN() + 1)
    if effective_probe_cap != PROBE_CAP:
        log(f"PROBE_CAP {PROBE_CAP} <= MAX_OPEN {MAX_OPEN()} — raising this tick's probe budget to "
            f"{effective_probe_cap} (a suspect head must never starve the ready scan's adoption)")

    def _create_spent() -> bool:
        # evaluated at USE time, not tick start: a reservation refunded mid-scan (verified
        # nothing-exists) must be spendable by the refunding candidate itself.
        return (not suspects_ok or n_open >= MAX_OPEN()
                or opened >= MAX_PER_TICK or attempts >= MAX_PER_TICK)

    # -- resolve persisted orphan suspects FIRST, before any creation (ff-r3 M2b) --
    suspects_ok = True
    if not DRY_RUN:
        suspects, suspects_ok = _load_suspects()
        if not suspects_ok:
            # ff-r4 P1: a damaged file may hold live reservations — preserve it for forensics and
            # DISABLE creation this tick (create_spent below). Adoption stays allowed.
            log("suspects file DAMAGED/unreadable — preserving as-is; creation DISABLED this tick")
        else:
            tracked = {p["fingerprint"] for p in await led.list_proposed()}
            # inflight = everything count_open_proposals() counts, INCLUDING 'proposing' claims a
            # failed release left behind (ff-r5 P2): their suspects are KEPT (the claim may still
            # resolve either way) but never reserved — the count already holds their slot.
            inflight = (await led.list_inflight_fingerprints()
                        if hasattr(led, "list_inflight_fingerprints") else tracked)
            remaining: list[dict] = []
            deferred: list[dict] = []
            for s in suspects:
                try:
                    if s["fingerprint"] in tracked:
                        # ff-r4 P2: the class got tracked after all (scan adopted it, or a mark
                        # committed but its response raised) — the ledger row already counts in
                        # n_open; keeping the suspect would DOUBLE-reserve. Clear it. (A set
                        # lookup that frees capacity — never cap-gated.)
                        log(f"suspect {s['branch']} is already tracked — cleared (no double reserve)")
                        continue
                    if probes >= effective_probe_cap:
                        # cap hit mid-suspects (probes share the K6 gh-read budget with the scan —
                        # they bypassed it entirely before): keep + RESERVE the unprocessed
                        # suspect fail-closed, and rotate it to the file HEAD below so a
                        # persistently-unknown head cannot starve the tail forever.
                        deferred.append(s); continue
                    repo = resolve_repo(s["repo_scope"])
                    if repo is None:
                        remaining.append(s); continue             # unresolvable scope: keep + reserve
                    probes += 1                                   # count the ATTEMPT (mirrors the scan)
                    status, existing = _find_open_bot_pr(repo, s["branch"])
                    if status == "found":
                        # the orphan is REAL — re-track it (pre-capacity: adoption adds no load).
                        slug_s = s["branch"].rsplit("/", 1)[-1]
                        prov = await led.capture_provenance(s["fingerprint"])
                        rendered = capture.render_skill_md(slug_s, slug_s, "src/**", "", "",
                                                           finding_ids=prov, origin_repo=s["repo_scope"])
                        dsha = capture.draft_hash(rendered) if rendered else "unknown"
                        if await led.claim_for_proposing(s["fingerprint"], s["branch"], dsha):
                            if await led.mark_capture_proposed(s["fingerprint"], s["branch"], dsha, existing):
                                log(f"suspect resolved: adopted orphan PR#{existing} for {s['branch']}")
                                n_open += 1
                            else:
                                await led.release_proposing(s["fingerprint"], s["branch"], dsha)
                                remaining.append(s)
                        elif s["fingerprint"] in inflight:
                            # young stuck 'proposing': its slot is already inside
                            # count_open_proposals — keep, unreserved; reap returns the row to
                            # 'ready' and the next tick's claim succeeds.
                            remaining.append(s)
                        else:
                            # No concurrent writer exists under the single-instance flock and reap
                            # already ran: a failed CAS means the LEDGER holds this candidate in a
                            # resolved/terminal state — the claim will fail EVERY tick, so a kept
                            # reservation protects nothing and leaks one MAX_OPEN slot forever.
                            # Drop, loudly. Residual: PR#existing stays OPEN but uncounted — real
                            # open bot PRs can exceed MAX_OPEN and STACK per orphan, permanently
                            # until human disposition; that is why the drop goes to the jefe
                            # inbox, not only this log.
                            msg = (f"suspect {s['branch']}: PR#{existing} is OPEN but its candidate "
                                   f"left 'ready' outside the proposer — dropping the suspect; the "
                                   f"PR needs human disposition (close or merge PR#{existing})")
                            log(msg)
                            _notify_jefe("proposer: orphan PR needs disposition", msg)
                    elif status == "none":
                        # CLEAR-INVARIANT (attack pass): "no open PR" alone never clears — a crash
                        # (or a definitive create failure) AFTER a landed push leaves a stranded
                        # branch and no PR; clearing here wedged the class behind the branch-exists
                        # refusal forever, destroying the durable intent the fsync work protects.
                        rbe = _remote_branch_exists(repo, s["branch"])
                        if rbe is False:
                            log(f"suspect resolved: no PR and no remote branch for {s['branch']} — cleared")
                        elif rbe is True:
                            # the durable write-ahead intent proves WE verified this branch absent
                            # and then pushed — the stranded ref is OURS: open the PR for the
                            # EXISTING head (no push, so the no-overwrite rule is intact).
                            # Bounded residual: a human branch pushed to the exact slug name inside
                            # the crash window gets ONE re-opened draft PR; the mark hands it to
                            # reconcile, so the next human close resolves it 'declined'.
                            slug_s = s["branch"].rsplit("/", 1)[-1]
                            prov = await led.capture_provenance(s["fingerprint"])
                            rendered = capture.render_skill_md(slug_s, slug_s, "src/**", "", "",
                                                               finding_ids=prov, origin_repo=s["repo_scope"])
                            dsha = capture.draft_hash(rendered) if rendered else "unknown"
                            if await led.claim_for_proposing(s["fingerprint"], s["branch"], dsha):
                                st2, pr2 = _create_pr_for_branch(repo, s["branch"], slug_s)
                                if st2 == "opened" and pr2 is not None and await led.mark_capture_proposed(
                                        s["fingerprint"], s["branch"], dsha, pr2):
                                    log(f"suspect recovered: opened PR#{pr2} for stranded pushed "
                                        f"branch {s['branch']} (existing head, no push)")
                                    n_open += 1
                                else:
                                    await led.release_proposing(s["fingerprint"], s["branch"], dsha)
                                    remaining.append(s)           # create/mark unresolved: keep + reserve
                            elif s["fingerprint"] in inflight:
                                remaining.append(s)
                            else:
                                msg = (f"suspect {s['branch']}: stranded remote branch exists (no "
                                       f"PR) and the candidate left 'ready' outside the proposer — "
                                       f"dropping the suspect; delete the branch to clean up")
                                log(msg)
                                _notify_jefe("proposer: stranded branch needs disposition", msg)
                        else:
                            remaining.append(s)                   # branch lookup unknown: keep + reserve
                    else:                                         # unknown/ambiguous: keep + RESERVE
                        remaining.append(s)
                except Exception as e:
                    log(f"suspect {s.get('branch')} resolution raised ({e!r}) — keep + reserve")
                    remaining.append(s)
            kept = deferred + remaining                           # rotation: cap-deferred entries go
            if suspects:                                          # to the HEAD for next tick
                _save_suspects(kept)
            # reserve ONLY for suspects the ledger count doesn't already hold (ff-r5 P2: a
            # surviving 'proposing' claim is in count_open_proposals — reserving its suspect too
            # would double-block capacity until reap).
            reserved_fps = {s["fingerprint"] for s in kept if s["fingerprint"] not in inflight}
            n_open += len(reserved_fps)
            if reserved_fps:
                log(f"{len(kept)} unresolved suspect(s) — reserving {len(reserved_fps)} MAX_OPEN "
                    f"slot(s) ({len(kept) - len(reserved_fps)} already counted in-flight)")
            if deferred and probes >= effective_probe_cap:
                log(f"suspect probes exhausted the probe budget ({probes}/{effective_probe_cap}) — "
                    f"ready scan starved this tick; {len(deferred)} suspect(s) rotated to next "
                    f"tick's head")
    # FAIRNESS (kilabz r2 #7): scan in fingerprint keyset order starting after a PERSISTED cursor,
    # wrapping to the start when the tail is exhausted — every ready row is eventually visited even
    # if 100+ persistently-skipped candidates (unlisted scope, render reject) sit ahead of it. The
    # cursor advances to the last VISITED fingerprint each tick (best-effort file; a lost cursor
    # just restarts the scan, never skips forever).
    cursor = ""
    try:
        cursor = CURSOR_FILE.read_text().strip()
    except OSError:
        pass
    batch = await led.list_ready_candidates(100, after=cursor)
    if not batch and cursor:
        cursor = ""                                               # wrapped: tail exhausted
        batch = await led.list_ready_candidates(100, after="")
    for c in batch:
        if _create_spent() and probes >= effective_probe_cap:
            break                                                 # nothing left this tick can do
        fp = c["fingerprint"]
        if fp in seen:
            continue
        seen.add(fp)                                              # K6: visit-once, no infinite retry
        # the exception boundary wraps the WHOLE candidate body (oracle code-review MAJOR: a raise
        # in provenance/render BEFORE the claim escaped the loop, crashed the tick, and — the row
        # still 'ready' — re-crashed every subsequent tick: a poison-pill halt). claimed tracks
        # whether the except-arm owes a release.
        branch = draft_sha = None
        claimed = False
        try:
            repo = resolve_repo(c["repo_scope"])
            if repo is None:
                log(f"ready {c['rule_tag']} scope {c['repo_scope']!r} not in repo allowlist — skip"); continue
            slug = capture.slug(c["rule_tag"])
            if slug is None:
                log(f"ready {c['rule_tag']} → no safe slug — skip"); continue
            prov = await led.capture_provenance(fp)
            rendered = capture.render_skill_md(slug, c["rule_tag"], c["path_glob"] or "src/**",
                                               "", "", finding_ids=prov, origin_repo=c["repo_scope"])
            if rendered is None:
                log(f"ready {c['rule_tag']} → render fail-closed — skip"); continue
            branch = capture.skill_branch(slug)
            draft_sha = capture.draft_hash(rendered)
            if DRY_RUN:
                # budget check BEFORE the log (an already-exhausted budget logs ZERO lines — kilabz
                # ff-r2 m3) AND right after the increment (a just-filled budget stops the scan
                # without visiting the next candidate). A9 intact: no DB verb, no gh read.
                if n_open >= MAX_OPEN() or opened >= MAX_PER_TICK:
                    break
                log(f"would propose {c['rule_tag']} → {branch} for {repo['nwo']} (draft_sha {draft_sha[:8]})")
                n_open += 1; opened += 1
                if n_open >= MAX_OPEN() or opened >= MAX_PER_TICK:
                    break
                continue
            if probes >= effective_probe_cap:
                # this candidate was NOT processed — it must not advance the cursor, or a persistent
                # probe-exhaustion permanently starves it (kilabz ff MAJOR: `continue` put it in
                # `seen`, the cursor moved past it, and an adoptable orphan was stranded forever).
                seen.discard(fp)
                break
            probes += 1
            status, existing = _find_open_bot_pr(repo, branch)    # K1: adopt by identity, not hash
            if status == "found":
                # ADOPT — allowed even at MAX_OPEN (it re-tracks an already-open PR). Claim just
                # before the mark: the lookup was a read, and the single-host flock means nobody
                # else could have claimed in between; a failed CAS = the row left 'ready' → skip.
                if await led.claim_for_proposing(fp, branch, draft_sha):
                    claimed = True
                    if await led.mark_capture_proposed(fp, branch, draft_sha, existing):
                        log(f"adopted existing PR#{existing} for {branch}")
                        if fp in reserved_fps:
                            reserved_fps.discard(fp)              # CONVERT the reservation in place —
                                                                  # the PR moves reserved→tracked,
                                                                  # count unchanged (a bare increment
                                                                  # here double-counted one real PR)
                        else:
                            n_open += 1
                        _clear_suspect(fp)                        # no-op for non-suspects (skip-save);
                                                                  # keeps the file consistent with the
                                                                  # conversion now, not next tick
                    else:
                        await led.release_proposing(fp, branch, draft_sha)
                continue
            if status != "none":
                # unknown/ambiguous: NEVER create for this candidate; consume + continue — the pass
                # is NOT stopped (ff-r3: break-on-unknown wedged the whole queue behind one repo's
                # gh trouble, blocking adoption in healthy repos). A non-suspect candidate has no
                # orphan risk (orphans exist only after an unknown CREATE, which persists a suspect).
                log(f"recovery lookup {status} for {branch} — defer (no claim, no create)")
                continue
            if fp in reserved_fps:
                # a RESERVED suspect probed DEFINITIVELY none mid-scan: apply the CLEAR-INVARIANT
                # (no PR AND no remote branch) before touching the reservation — the "none" PR
                # lookup alone must never destroy the durable intent for a stranded pushed branch.
                rbe = _remote_branch_exists(repo, branch)
                if rbe is False:
                    n_open -= 1
                    reserved_fps.discard(fp)
                    _clear_suspect(fp)                            # verified nothing exists: refund
                                                                  # BEFORE the create path takes its
                                                                  # own slot at the reserve-exactly-
                                                                  # once point below
                elif rbe is True:
                    # stranded pushed branch that is OURS (durable write-ahead intent): recover —
                    # PR for the EXISTING head, no push (see the resolution-pass twin arm).
                    if await led.claim_for_proposing(fp, branch, draft_sha):
                        claimed = True
                        st2, pr2 = _create_pr_for_branch(repo, branch, slug)
                        if st2 == "opened" and pr2 is not None and await led.mark_capture_proposed(
                                fp, branch, draft_sha, pr2):
                            log(f"recovered stranded branch {branch}: opened PR#{pr2} for the "
                                f"existing head (no push)")
                            reserved_fps.discard(fp)              # conversion: reservation → tracked
                            _clear_suspect(fp)
                        else:
                            await led.release_proposing(fp, branch, draft_sha)
                    continue                                      # resolved or kept — never a fresh push
                else:
                    log(f"reserved suspect {branch}: branch-exists lookup unknown — keep the "
                        f"reservation, defer")
                    continue
            if _create_spent():
                continue                                          # capacity gates CREATION only
            exists = _remote_branch_exists(repo, branch)
            if exists is not False:
                # branch present with NO open bot PR (human content on a closed-PR branch, or a
                # crashed push-before-create) OR the lookup failed — either way, NEVER push over it.
                log(f"remote {branch} exists-or-unknown ({exists}) with no open bot PR — REFUSING "
                    f"create; delete the branch (or wait) to unwedge"); continue
            if not await led.claim_for_proposing(fp, branch, draft_sha):
                continue                                          # lost the CAS / not ready
            claimed = True
            # worktree named by fingerprint, not repo_scope (kilabz MINOR: keep the scope string out
            # of every git argv, including the worktree path — A2's boundary stated fully)
            wt = _make_proposal_commit(repo, f"wt-{fp[:16]}", slug, rendered)
            if wt is None:
                await led.release_proposing(fp, branch, draft_sha); continue
            # WRITE-AHEAD intent BEFORE the create side effect (ff-r4 P1): if the intent cannot be
            # made durable, REFUSE to create — a post-hoc persist that fails would reopen the
            # untracked-orphan gap the suspects file exists to close.
            if not _add_suspect(fp, c["repo_scope"], branch):
                log(f"cannot persist create-intent for {branch} — refusing to create")
                try:
                    _git(repo["path"], "worktree", "remove", "--force", str(wt))
                except Exception:
                    pass
                await led.release_proposing(fp, branch, draft_sha); continue
            # RESERVE EXACTLY ONCE, here, with the intent (ff-r5 P1): every unresolved exit — an
            # unknown outcome, a raising mark, a failed fence-close, the candidate exception
            # handler — RETAINS this reservation; only a DEFINITIVE no-pr refunds it, and an
            # opened+marked outcome converts it into the tracked count (no second increment).
            n_open += 1
            attempts += 1                                         # burn budget on the ATTEMPT
            try:
                outcome, pr_number = _push_and_open_pr(repo, wt, branch, slug)
            finally:
                try:                                              # cleanup must NOT raise past the
                    _git(repo["path"], "worktree", "remove", "--force", str(wt))
                except Exception as ce:                           # outcome accounting (ff-r2 M2a);
                    log(f"worktree cleanup raised ({ce!r}) — tick-start GC sweeps it")
            if outcome == "unknown":
                # a REAL PR may exist untracked — the durable intent IS the suspect and the
                # reservation is already held; next tick's resolution re-tracks or clears it.
                try:
                    await led.release_proposing(fp, branch, draft_sha)
                except Exception as e:                            # ff-r3 gap-1: reservation survives
                    log(f"release after unknown create raised ({e!r}) — reap recovers the claim")
                continue
            if outcome != "opened" or pr_number is None:
                # "no-pr" flavors (attack pass): only FOREIGN-sha no-pr clears the intent —
                # positive evidence the branch content is not ours. A verified-absent no-pr KEEPS
                # the suspect: absence read seconds after a failed push can be a lagging read of a
                # ref the server applies late; next tick's resolution clears it only once no-PR
                # AND branch-absent are both confirmed (or recovers via create if the ref landed).
                if outcome == "no-pr-foreign":
                    _clear_suspect(fp)
                n_open -= 1                                       # refund the reservation
                await led.release_proposing(fp, branch, draft_sha); continue
            if await led.mark_capture_proposed(fp, branch, draft_sha, pr_number):
                _clear_suspect(fp)                                # the reservation now IS the tracked PR
                log(f"opened draft PR#{pr_number} for {branch} ({repo['nwo']})"); opened += 1
            else:
                log(f"mark_proposed fenced out (PR#{pr_number}) — closing our PR")
                if _gh_close(repo["nwo"], pr_number):
                    _clear_suspect(fp)
                    n_open -= 1                                   # definitively closed: refund
                else:
                    log(f"close of fenced-out PR#{pr_number} FAILED — suspect+reservation kept")
        except Exception as e:                                    # never let one class wedge the tick
            log(f"propose {c['rule_tag']} raised ({e!r}) — release + continue")
            if claimed and branch is not None:
                try:
                    await led.release_proposing(fp, branch, draft_sha)
                except Exception:
                    pass
    if seen and not DRY_RUN:                                      # advance the fairness cursor (r2 #7)
        try:
            CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
            CURSOR_FILE.write_text(max(seen))
        except OSError as e:
            log(f"cursor write failed ({e}) — scan restarts next tick (harmless)")


def _log_gh_identity() -> None:
    """One best-effort tick-start line naming the authenticated gh login (attack pass): every
    `--author @me` lookup silently changes meaning across a bot-PAT rotation — old-identity PRs
    then read as definitive 'none' and their suspects clear while the real PRs stay open. The
    log makes an identity change visible exactly when 'none' verdicts start lying. Unknown-
    tolerant, never raises."""
    try:
        r = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True,
                           env=_git_env(), timeout=GH_TIMEOUT, check=False)
        ident = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"
    except Exception:
        ident = "unknown"
    log(f"gh identity: {ident}")


async def _amain() -> int:
    # DRY_RUN bypasses the arm flag (kilabz r4): the flag gates LIVE operation; a dry tick is
    # proven side-effect-free (A9 — zero DB/git/gh mutations, tested) and is exactly the pre-arm
    # diagnostic the runbook needs, so requiring the flag for it would force arming to test.
    if not ENABLED_FLAG.exists() and not DRY_RUN:
        log("PROPOSER_ENABLED absent — off; exiting"); return 0
    if not acquire_lock():
        return 0
    led = None
    try:
        gc_worktrees()                                            # A6: tick-start GC (flock ⇒ prior tick dead)
        if not DRY_RUN:                                           # a dry tick makes no gh reads (A9 discipline)
            _log_gh_identity()
        led = await PostgresLedger.connect(DSN)
        # reap crashed 'proposing' claims back to 'ready' (skip under DRY_RUN — it's a mutation, A9)
        if not DRY_RUN:
            reaped = await led.reap_stuck_proposing(REAP_TIMEOUT_MIN)
            if reaped:
                log(f"reaped {reaped} stuck proposing row(s)")
        await reconcile(led)
        await propose(led)
        return 0
    finally:
        if led is not None:
            try:
                await led.close()
            except Exception:
                pass
        release_lock()


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] != "tick":
        print("usage: python -m runtime.proposer tick", file=sys.stderr)
        return 2
    # liveness-fire: one unconditional line per fire (mirrors the tick wrappers)
    log("tick fire")
    import asyncio
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
