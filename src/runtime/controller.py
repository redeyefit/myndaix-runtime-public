"""controller.py — the controller-loop ("the brain"), north-star rung 3.

A bounded, non-Claude (launchd-triggered) controller that turns push-triggered
review into BRAIN-decided review. It is a level-triggered reconciler (decide from
observed state, not from an event): each hourly tick, for every trusted repo, it
fetches the watched ref into a controller-OWNED ref, compares HEAD to a durable
per-(repo,ref) cursor (review_cursor, DESIGN v0.2 §2), and — if HEAD advanced past
the last DELIVERED review and none is in flight — triggers the EXISTING
play-review.sh pipeline for the delta. Then it exits. NOT a daemon: one bounded
job per launchd tick.

What it deliberately does NOT do (later north-star rungs): no LLM in the decision
path, no auto-fix (it sets PLAY_DISABLE_AUTOFIX=1, a HARD override, so play-review's
autofix bridge can NEVER fire from a brain review even where autofix is armed), no
auto-merge, no learning.

Trigger model (DESIGN, locked): SYNTHETIC-STDIN, near-zero-touch — the brain pipes a
constructed git pre-push line "<ref> <head> <ref> <reviewed_sha>" plus argv
`origin <url>` into play-review.sh (the ONLY edit there is a fail-closed autofix
disable), reproducing a push of reviewed_sha..head. The fetch (B1) + controller-owned
refs guarantee both objects are local and gc-safe; the cursor bootstrap (B2)
guarantees reviewed_sha is never the zero/empty-tree sha. The cursor advances from a
LEDGER signal (a delivered review job), NOT play-review's done-marker, which is
suppressed when the branch moves mid-review.

Run one tick:
    MYNDAIX_DSN=postgresql://localhost/runtime PYTHONPATH=src python3 -m runtime.controller tick

Safe first run (decide + log, write nothing, dispatch nothing):
    MYNDAIX_CONTROLLER_DRY_RUN=1 ... python3 -m runtime.controller tick
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat as _stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from runtime import capture, skillmatch
from runtime.ledger.postgres_store import PostgresLedger

# -- config --------------------------------------------------------------------
DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
# ORCH is NOT env-overridable: play-review.sh hardcodes $HOME/.myndaix/orchestrator,
# and the controller must read/write the SAME state it does (codex M6).
ORCH = HOME / ".myndaix" / "orchestrator"
STATE = ORCH / "state"
REPOS_JSON = Path(os.environ.get("MYNDAIX_REPOS_JSON", str(ORCH / "repos.json")))
PLAY_REVIEW = Path(os.environ.get("PLAY_SELF", str(ORCH / "play-review.sh")))
LOCK = ORCH / "controller.lock"                          # an flock'd FILE, not a dir

DEFAULT_WATCH_REF = "refs/heads/main"


def _int_env(name: str, default: int) -> int:
    # STRICT digit-only, mirroring play-review.sh's `^[0-9]+$` so the Python/bash sides of a SHARED
    # knob (PLAY_REVIEW_CALL_TIMEOUT, PLAY_STALE) can't diverge (Python's int() would otherwise take
    # "-1000"/"+5"/" 5 " that bash rejects). Capped to a sane machine int: 2^31-1 is 10 digits, so
    # any 11+ SIGNIFICANT-digit value exceeds it AND a >4300-digit one would crash int() (Py3.11+
    # limit) — the length short-circuit caps both without ever calling int() on a huge string. Strip
    # leading zeros FIRST or a padded small value ("00000000003") wrongly hits the cap (oracle+kilabz
    # r5). A malformed value falls back to the default, so a bad launchd env var can never crash a
    # service at import; EVERY module-level env knob below reads through this helper.
    val = os.environ.get(name, "")
    if not re.fullmatch(r"[0-9]+", val):
        return default
    val = val.lstrip("0") or "0"
    return 2**31 - 1 if len(val) > 10 else min(int(val), 2**31 - 1)


MAX_DISPATCH_PER_TICK = _int_env("MYNDAIX_CONTROLLER_MAX_DISPATCH", 3)
MAX_DISPATCH_PER_DAY = _int_env("MYNDAIX_CONTROLLER_MAX_DAY", 20)
MAX_ATTEMPTS = _int_env("MYNDAIX_CONTROLLER_MAX_ATTEMPTS", 3)
TRANSIENT_ALERT_STREAK = _int_env("MYNDAIX_CONTROLLER_TRANSIENT_STREAK", 3)
# The transient marker's content is play-review's cause label ("kilabz OUT OF CREDITS", "USAGE
# LIMIT (Sep 24th, 2026 3:00 PM)"). It is a file in the shared state dir, so only the label's
# own alphabet reaches the alert.
_TRANSIENT_CAUSE_STRIP = re.compile(r"[^A-Za-z0-9 ():,.?-]")
# PENDING_STALE must exceed BOTH the play-review worst-case worker runtime AND the tick interval
# (3600s): the worker OUTLIVES the dispatching tick (nohup-detached + AbandonProcessGroup, so
# launchd no longer reaps it on tick exit), and a stale row reclaimed while its worker is still
# live becomes a second concurrent same-head dispatch. The worker's lock budget is NOT fixed —
# play-review.sh derives STALE_FLOOR = 3*180 + 3*RCT + 360 from PLAY_REVIEW_CALL_TIMEOUT (and takes
# max with an explicit PLAY_STALE). A hardcoded 7200 silently under-floors once RCT is raised past
# ~2100 (RCT=2500 -> floor 8400 > 7200 -> reaps a LIVE worker). So mirror the worker's derivation
# from the SAME shared env and never sit below it (kilabz self-review 2026-07-03; same class as the
# PR#60 STALE floor). At the RCT=1200 default the floor is 4500, so the 7200 default is unchanged.
def _worker_lock_floor(rct: int) -> int:
    # mirror play-review.sh STALE_FLOOR: 3 canaries x 180 + 3 review calls (each up to rct) + 360 margin
    return 3 * 180 + 3 * rct + 360
_WORKER_LOCK_FLOOR = _worker_lock_floor(_int_env("PLAY_REVIEW_CALL_TIMEOUT", 1200))
PENDING_STALE = max(
    _int_env("MYNDAIX_CONTROLLER_PENDING_STALE", 7200),   # 2 h default
    _WORKER_LOCK_FLOOR,                                    # RCT-derived worker budget
    _int_env("PLAY_STALE", 0),                             # an explicit worker STALE override
)
FETCH_TIMEOUT = _int_env("MYNDAIX_CONTROLLER_FETCH_TIMEOUT", 60)
REVIEW_TIMEOUT = _int_env("MYNDAIX_CONTROLLER_REVIEW_TIMEOUT", 60)
# Per-dispatch review-size budget in CHANGED LINES (numstat added+deleted; binary files
# count 0 — they reach the reviewers as one-line stubs). The models eat a big diff fine;
# the real wall is the reviewer's ~600s call budget — a ~3400-line backlog range timed
# kilabz out at exactly REVIEW_CALL_TIMEOUT (2026-07-02), burned 3 attempts and BLOCKED
# the cursor. A range over budget is dispatched as the largest first-parent PREFIX that
# fits (the cursor then walks the backlog across ticks); a single commit over budget on
# its own cannot be split — it is advanced past WITHOUT review + flagged to the inbox.
# A zero/negative override would flip the backstop into advance-everything-WITHOUT-review
# (workflow security lens) — clamp to the default instead, mirroring PLAY_STALE.
MAX_REVIEW_LINES = _int_env("MYNDAIX_CONTROLLER_MAX_REVIEW_LINES", 1500)   # _int_env: a malformed
if MAX_REVIEW_LINES < 1:                                                    # value falls back, not crash
    MAX_REVIEW_LINES = 1500
# Companion BYTE budget: lines under-count a long-line diff (a one-line 300KB minified
# bundle is 1 numstat line) and the worker ALSO enforces a byte cap (PLAY_MAX_DIFF) —
# a lines-only chunker would dispatch a chunk the worker bounces NON-transiently, which
# burns attempts and blocks a chunk no push can clear (workflow #1). Both budgets are
# passed through _review_env so controller and worker can never disagree on either cap.
MAX_REVIEW_BYTES = _int_env("MYNDAIX_CONTROLLER_MAX_REVIEW_BYTES", 262144)
if MAX_REVIEW_BYTES < 1:
    MAX_REVIEW_BYTES = 262144
# How many first-parent commits the chunker will size before giving up on finding a
# bigger prefix. Bounds per-tick work (2 local git calls per candidate). Past the cap a
# revert-heavy history could hide a fitting prefix (kilabz #2) — the fallback then flags
# the first commit to a human rather than searching unboundedly, and logs the truncation.
CHUNK_WALK_CAP = _int_env("MYNDAIX_CONTROLLER_CHUNK_WALK", 200)
if CHUNK_WALK_CAP < 1:
    CHUNK_WALK_CAP = 200

DRY_RUN = os.environ.get("MYNDAIX_CONTROLLER_DRY_RUN") == "1"
TEST_MODE = os.environ.get("MYNDAIX_CONTROLLER_TEST_MODE") == "1"
DISPATCH_OVERRIDE = os.environ.get("MYNDAIX_CONTROLLER_DISPATCH_OVERRIDE", "")

# -- +learning rung (skill indexer, build plan Step 5) -------------------------
GH_TIMEOUT = _int_env("MYNDAIX_CONTROLLER_GH_TIMEOUT", 30)
SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
JEFE_INBOX = HOME / ".myndaix" / "bridge" / "inbox" / "jefe"
SKILLS_ENABLED = ORCH / "SKILLS_ENABLED"             # the global arm; removed to fail-closed if a block can't persist

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9._][A-Za-z0-9._/-]*$")
# remote transports we accept. plain/unauth http:// and git:// are rejected; "ext::"/"fd::"
# (and any "::") are arbitrary-exec RCE. The SAME set is enforced at the git layer via
# GIT_ALLOW_PROTOCOL below, so an `insteadOf`/`protocol.ext.allow=always` config trick can't
# rewrite a validated URL into ext:: — including inside play-review's own ls-remote.
_URL_OK = ("https://", "ssh://", "file://", "git@")
# env-level protocol allowlist (overrides config, inherited by play-review). Authenticated/local
# only — NOT ext/fd (RCE) or git:// (plaintext). Preserves global insteadOf/credential helpers for
# the ALLOWED transports (so private-repo auth keeps working — Oracle/codex: do NOT nuke git config).
_GIT_ALLOW_PROTOCOL = "https:ssh:file"


def log(msg: str) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [controller] {msg}", flush=True)


def _utcday() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")


def _slug(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", ref)


def _ctl_head_ref(ref: str) -> str:
    return f"refs/myndaix/controller/{_slug(ref)}"        # anchors the fetched head (no FETCH_HEAD race, gc-safe)


def _ctl_reviewed_ref(ref: str) -> str:
    return f"refs/myndaix/reviewed/{_slug(ref)}"          # anchors the reviewed base against git gc


def _ctl_pending_ref(ref: str) -> str:
    return f"refs/myndaix/pending/{_slug(ref)}"           # anchors the in-flight head against gc (codex MAJOR)


# -- minimal, allowlisted subprocess envs (build up, never inherit blindly) -----
def _git_env() -> dict:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",                      # never block on a credential prompt
        "GIT_ALLOW_PROTOCOL": _GIT_ALLOW_PROTOCOL,       # block ext::/fd:: RCE (keeps auth config working)
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG"):        # ssh remotes / tmp
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _review_env() -> dict:
    # play-review.sh resets its own PATH and derives ORCH from HOME. It needs HOME +
    # MYNDAIX_DSN (so its `mxr` calls reach the ledger) + ssh auth for confirm_pushed.
    # PLAY_SELF pins its worker to the validated trusted path (codex M7, no worktree fallback).
    # PLAY_DISABLE_AUTOFIX hard-disables autofix (B1). The controller passes an EMPTY remote URL
    # to play-review (see trigger_review), so confirm_pushed treats the dispatch as pushed and
    # writes the post-delivery done-<sha> marker WITHOUT a public PLAY_FORCE_DONE bypass and
    # WITHOUT running ls-remote (codex MAJOR). GIT_ALLOW_PROTOCOL is belt-and-suspenders.
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": _GIT_ALLOW_PROTOCOL,
        "MYNDAIX_DSN": DSN,
        "PLAY_SELF": str(PLAY_REVIEW),
        "PLAY_DISABLE_AUTOFIX": "1",
        # keep BOTH worker fail-fast caps in lockstep with the chunker's budgets: the
        # controller never dispatches a range over MAX_REVIEW_LINES/MAX_REVIEW_BYTES, so
        # a tighter worker-side default could bounce a valid chunk (a non-transient diff
        # abort that climbs to the blocked ceiling — and a blocked CHUNK does not clear
        # on a new push). Passing our own budgets makes agreement structural (workflow #1:
        # covering only the line cap left the 256KB byte default able to bounce a chunk).
        "PLAY_MAX_DIFF_LINES": str(MAX_REVIEW_LINES),
        "PLAY_MAX_DIFF": str(MAX_REVIEW_BYTES),
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    # errors="replace": a non-UTF-8 filename in numstat/rev-list output (or a repo with
    # core.quotePath=false) would otherwise raise UnicodeDecodeError — a ValueError, NOT an
    # OSError, so the callers' `except (TimeoutExpired, OSError)` miss it and the tick dies
    # instead of honouring their "None on ANY failure" contract (oracle self-review 2026-07-03,
    # same class automerge.py already guards). Replacement chars never corrupt the ASCII we
    # parse (numstat counts, SHAs, remote URLs).
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace", env=_git_env(), timeout=timeout, check=False,
    )


def _gh_json(repo: Path, *args: str, timeout: Optional[int] = None):
    """Run a gh command that prints JSON; return the parsed value or None on any failure
    (non-zero exit / unparseable). argv, never shell (mirrors automerge.py:194). Adds the
    GH token to _git_env so gh auths whether via keyring (HOME/.config/gh) or env token."""
    env = _git_env()
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    try:
        r = subprocess.run(["gh", *args], cwd=str(repo), capture_output=True, text=True,
                           env=env, timeout=timeout or GH_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        # a MISSING or HUNG gh binary must fail CLOSED for skills only (-> None ->
        # _block_repo_skills), NEVER raise out of _index_skills and take down the review
        # path that runs after it (kilabz HIGH). subprocess.SubprocessError covers TimeoutExpired.
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


# -- config --------------------------------------------------------------------
class Repo:
    __slots__ = ("repo_id", "path", "watch_ref")

    def __init__(self, repo_id: str, path: Path, watch_ref: str):
        self.repo_id, self.path, self.watch_ref = repo_id, path, watch_ref


def load_config() -> list[Repo]:
    """Trusted repo list from $ORCH/repos.json (chmod-600, outside any repo, so a commit
    can't redefine it). repo_id = basename(path) to MATCH play-review.sh:76; duplicate
    basenames are rejected (they'd collide on the cursor key). A missing or malformed file
    logs and yields [] (the tick no-ops; never crash-loops launchd)."""
    try:
        raw = json.loads(REPOS_JSON.read_text())
    except FileNotFoundError:
        log(f"no repos.json at {REPOS_JSON} — nothing to watch"); return []
    except (json.JSONDecodeError, OSError) as e:
        log(f"repos.json unreadable ({e}) — skipping tick"); return []
    if not isinstance(raw, dict):                        # valid JSON but not an object (e.g. a list)
        log(f"repos.json is a {type(raw).__name__}, expected an object — skipping tick"); return []

    repos: list[Repo] = []
    seen: set[str] = set()
    for key, entry in raw.items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue                                     # _comment / _verify_note
        p = entry.get("path")
        if not p:
            log(f"config '{key}': no path — skipped"); continue
        path = Path(p).expanduser().resolve()
        if not (path.is_dir() and (path / ".git").exists()):
            log(f"config '{key}': {path} is not a git repo — skipped"); continue
        watch_ref = entry.get("watch_ref", DEFAULT_WATCH_REF)
        if not _REF_RE.match(watch_ref):
            log(f"config '{key}': bad watch_ref {watch_ref!r} — skipped"); continue
        repo_id = path.name
        if repo_id in seen:
            log(f"config '{key}': duplicate basename {repo_id!r} — rejected (cursor key clash)")
            continue
        seen.add(repo_id)
        repos.append(Repo(repo_id, path, watch_ref))
    return repos


# -- single-instance lock (kernel flock: atomic, auto-released on crash) ---------
# flock replaces the old mkdir + mtime-reap + heartbeat machinery wholesale: the kernel
# grants the lock to exactly one open fd and releases it automatically when the holder
# exits or dies, so there is NO stale-reap race (Oracle BLOCKER) and NO need for a TTL or
# heartbeat (codex/Oracle: writing LOCK/meta never bumped the dir mtime being checked). The
# fd is held open in a module global for the tick's lifetime; release closes it.
_LOCK_FD: Optional[int] = None


def acquire_lock() -> bool:
    global _LOCK_FD
    ORCH.mkdir(parents=True, exist_ok=True)
    # a pre-v0.4 build used a mkdir-DIR lock at this path; os.open would raise IsADirectoryError
    # forever. The old scheme is gone, so any dir here is a stale artifact — reap it (codex MAJOR).
    if LOCK.is_dir():
        log("reaping a legacy directory-style lock"); shutil.rmtree(LOCK, ignore_errors=True)
    fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        log("another tick holds the lock — exiting"); return False
    _LOCK_FD = fd
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {int(_dt.datetime.now().timestamp())}\n".encode())
    except OSError:
        pass
    return True


def release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
            os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


# -- the trusted play-review.sh gate + the review trigger ----------------------
def _trusted_script(p: Path) -> bool:
    """Only ever exec the FIXED installed play-review.sh, never a worktree copy: a regular
    file (not a symlink), owned by us, not group/world-writable, executable."""
    try:
        st = p.lstat()
    except OSError:
        log(f"play-review.sh missing at {p}"); return False
    if _stat.S_ISLNK(st.st_mode):
        log(f"play-review.sh is a symlink ({p}) — refusing"); return False
    if not _stat.S_ISREG(st.st_mode):
        log(f"play-review.sh not a regular file ({p}) — refusing"); return False
    if st.st_uid != os.getuid():
        log(f"play-review.sh not owned by us ({p}) — refusing"); return False
    if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
        log(f"play-review.sh is group/world-writable ({p}) — refusing"); return False
    if not os.access(p, os.X_OK):
        log(f"play-review.sh not executable ({p}) — refusing"); return False
    return True


def remote_url(repo: Repo) -> Optional[str]:
    """The repo's origin URL, restricted to authenticated/local transports (no exec
    transports). Validated BEFORE any fetch (codex M1) so a poisoned remote.origin.url
    never reaches `git fetch`."""
    r = _git(repo.path, "config", "--get", "remote.origin.url")
    url = r.stdout.strip()
    if r.returncode != 0 or not url:
        log(f"{repo.repo_id}: no origin url — skip"); return None
    if "::" in url or not url.startswith(_URL_OK):
        log(f"{repo.repo_id}: disallowed remote url transport — skip"); return None
    return url


def trigger_review(repo: Repo, head: str, base: str) -> bool:
    """Fire the review via synthetic-stdin into the trusted play-review.sh. Returns True
    iff the trigger ran cleanly (or was recorded under the test seam). We pass an EMPTY
    remote URL: the dispatched sha is already on the remote (we fetched it), so play-review's
    confirm_pushed treats it as pushed and writes its post-delivery done-marker — no public
    force-done bypass, and no ls-remote network call (codex MAJOR)."""
    line = f"{repo.watch_ref} {head} {repo.watch_ref} {base}\n"

    if TEST_MODE and DISPATCH_OVERRIDE:                  # unit-test seam: record, don't run
        rec = {"repo_id": repo.repo_id, "ref": repo.watch_ref, "head": head,
               "base": base, "cwd": str(repo.path)}
        with open(DISPATCH_OVERRIDE, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        log(f"{repo.repo_id}: TEST SEAM recorded dispatch {base[:8]}..{head[:8]}")
        return True
    if DISPATCH_OVERRIDE and not TEST_MODE:
        log("DISPATCH_OVERRIDE set without TEST_MODE=1 — refusing (not a production path)")
        return False

    if not _trusted_script(PLAY_REVIEW):
        return False
    try:
        proc = subprocess.run(
            [str(PLAY_REVIEW), "origin", ""],            # empty URL -> confirm_pushed treats as pushed, no ls-remote
            input=line.encode(), cwd=str(repo.path), env=_review_env(),
            timeout=REVIEW_TIMEOUT, check=False,
        )                                                # FRONT detaches the worker + exits 0 fast
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"{repo.repo_id}: play-review trigger failed ({e})")
        return False
    if proc.returncode != 0:                             # FRONT itself failed -> don't charge budget
        log(f"{repo.repo_id}: play-review FRONT exited {proc.returncode} — not charging budget")
        return False
    log(f"{repo.repo_id}: review triggered {base[:8]}..{head[:8]}")
    return True


# -- per-repo decision (level-triggered reconcile) -----------------------------
def _done_marker(repo: Repo, ref: str, sha: str) -> Path:
    # Scoped done-<repo>-<ref>-<sha>: a bare done-<sha> was GLOBAL — the SAME bug class already
    # fixed for _transient_marker below. This marker drives the cursor ADVANCE (marks code as
    # reviewed), so a collision is worse than the transient one: two watched repos sharing a commit
    # sha (forks/mirrors), or a force-push re-introducing a sha against a DIFFERENT base within
    # PRUNE_DAYS, could cross-claim "reviewed $sha" and advance the cursor PAST unreviewed code.
    # Scoped identically to _transient_marker so the worker's mark_done ("done-$marker_slug-$tip")
    # matches; _slug MUST stay identical to the worker's bash substitution (contract-tested).
    return STATE / f"done-{_slug(Path(repo.path).name)}-{_slug(ref)}-{sha}"


def _done(repo: Repo, ref: str, sha: str) -> bool:
    return _done_marker(repo, ref, sha).exists()


def _diff_lines(repo: Repo, base: str, head: str) -> Optional[int]:
    """Changed lines (added+deleted) of base..head per `git diff --numstat`. Binary
    files (numstat `-`) count 0 — the reviewers see them as one-line stubs, so they
    cost no review time. None on ANY failure incl. timeout (the documented contract;
    callers decide fail-open vs defer). MUST stay the same metric as play-review's
    PLAY_MAX_DIFF_LINES awk sum, or the worker could bounce a controller chunk."""
    try:
        r = _git(repo.path, "diff", "--numstat", "--no-ext-diff", base, head, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    total = 0
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        for p in parts[:2]:
            if p.isdigit():
                total += int(p)
    return total


def _diff_bytes(repo: Repo, base: str, head: str) -> Optional[int]:
    """Patch-text size in BYTES of base..head — the same metric as play-review's
    byte check, which measures $(git diff ...) i.e. AFTER bash command substitution
    strips trailing newlines (kilabz R2: counting the raw stdout disagreed by one
    byte exactly at the cap boundary). 0 ⇔ a TRULY empty diff (unlike zero numstat
    lines, which a binary/mode/rename-only change also produces). None on any
    failure. Binary-mode subprocess on purpose: a patch can carry non-UTF-8 bytes,
    and _git's text-mode decode would raise on them."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo.path), "diff", "--no-ext-diff", base, head],
            capture_output=True, env=_git_env(), timeout=60, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return len(r.stdout.rstrip(b"\n"))


def _choose_review_target(repo: Repo, base: str, head: str) -> tuple[str, str, int, int]:
    """Pick what one dispatch should actually review. Returns (mode, sha, lines, bytes):

      ("dispatch", head, n, b) — the whole range is PROVEN to fit BOTH budgets
                                 (MAX_REVIEW_LINES and MAX_REVIEW_BYTES). A dispatch
                                 always carries real, verified sizes — an UNSIZED
                                 range is never dispatched (kilabz PR#60 review: a
                                 byte-fat one-liner whose _diff_bytes times out is
                                 exactly what the byte cap exists to catch; a blind
                                 dispatch bounces non-transiently and can climb back
                                 to blocked).
      ("dispatch", mid, n, b)  — range over a budget: the LARGEST first-parent prefix
                                 base..mid that fits BOTH. The cursor walks the
                                 remainder on later ticks.
      ("advance", sha, n, b)   — no prefix fits. sha is the FIRST commit past base
                                 (or head itself when the history has no walkable
                                 prefix — a backward force-push/rewrite): either over
                                 a budget (skip WITHOUT review + flag jefe) or TRULY
                                 empty vs base (b == 0: play-review would abort on
                                 the empty diff; advance silently, mirroring the
                                 empty-range short-circuit).
      ("defer", sha, n, b)     — sizing failed anywhere a dispatch/skip decision
                                 depends on it (n/b may be -1 = unknown); do NOTHING
                                 this tick and retry. Persistent defers surface via
                                 the defer streak alert. One rule: never dispatch
                                 blind, never skip blind.

    Both budgets matter (workflow #1): numstat lines under-count a long-line diff
    (a one-line 300KB minified file is 1 line), and the worker enforces a byte cap
    the controller passes through. Zero numstat lines does NOT mean an empty diff
    (kilabz #1): a binary/mode/rename-only commit costs ~0 lines but IS reviewable —
    only a zero-BYTE (truly empty) prefix is skipped. Prefix sizes are not monotonic
    (a later commit can revert an earlier one), so every candidate within
    CHUNK_WALK_CAP is sized and the last fit wins."""
    fits = lambda n, b: n <= MAX_REVIEW_LINES and b <= MAX_REVIEW_BYTES
    # memoize per-commit sizing WITHIN this tick: the walk loop and the single-commit fallback
    # otherwise re-size the same base..commit (up to two extra 60s git calls at the worst moment —
    # an oversized range). A None (sizing FAILURE) is deliberately NOT cached, so a later call still
    # gets its one retry — preserving the "bytes unknown -> try once more" behaviour (oracle #6).
    _lc: dict = {}
    _bc: dict = {}
    def dl(c):
        if _lc.get(c) is None:
            _lc[c] = _diff_lines(repo, base, c)
        return _lc[c]
    def db(c):
        if _bc.get(c) is None:
            _bc[c] = _diff_bytes(repo, base, c)
        return _bc[c]
    total_l = dl(head)
    if total_l is None:
        return ("defer", head, -1, -1)                   # unsized — never dispatch blind
    total_b = db(head)
    if total_b is not None and fits(total_l, total_b):
        return ("dispatch", head, total_l, total_b)
    if total_b is None and total_l <= MAX_REVIEW_LINES:
        # lines fit but the byte size is UNKNOWN — a byte-fat one-line diff (whose
        # `git diff` is exactly what times _diff_bytes out) is the very case the byte
        # cap catches, so a blind dispatch here would bounce non-transitively and can
        # climb back to blocked (kilabz PR#60 review). Defer; the streak alert
        # surfaces a persistent sizing failure.
        return ("defer", head, total_l, -1)
    if total_b is None:
        total_b = -1                                     # lines already over — walk; every prefix
                                                         # candidate is sized in BOTH dimensions
    try:
        rl = _git(repo.path, "rev-list", "--first-parent", "--reverse", f"{base}..{head}",
                  timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return ("defer", head, -1, -1)
    if rl.returncode != 0:
        # a NONZERO rev-list (git lock, transient object/ref error) is NOT the force-push
        # empty-result case below — that requires a SUCCESSFUL rev-list that is genuinely
        # empty. Conflating them (the old `if rc==0 else []`) advanced the cursor PAST an
        # unreviewed over-budget range on a transient git hiccup, mislabelled "too large,
        # needs a human" — the exact never-skip-blind violation PR #61 fixed for the
        # _diff_* paths, missed here. Defer + retry, like the exception branch above.
        # (Found by the autonomous review of the PR#59+61 range, 2026-07-03.)
        return ("defer", head, -1, -1)
    commits = [c for c in rl.stdout.split() if _SHA_RE.match(c)]
    if not commits:
        # backward force-push / rewritten history: rev-list SUCCEEDED but base..head has no
        # first-parent commits (head is not a descendant of base). NO walkable prefix, and a
        # fail-open dispatch of this known-over-budget range is GUARANTEED to bounce off the
        # worker caps the controller arms (workflow #3) — advance-and-flag instead.
        return ("advance", head, total_l, total_b)
    best: tuple[str, int, int] = ("", 0, 0)
    for c in commits[:CHUNK_WALK_CAP]:
        if c == head:                                    # the full range is already known too big
            continue
        n = dl(c)
        if n is None or n > MAX_REVIEW_LINES:
            continue
        b = db(c)
        if b is None or b == 0 or b > MAX_REVIEW_BYTES:  # 0 bytes ⇔ truly empty prefix
            continue
        best = (c, n, b)                                 # keep the LAST (largest) fit
    if best[0]:
        return ("dispatch", best[0], best[1], best[2])
    if len(commits) > CHUNK_WALK_CAP:
        log(f"chunker: no fitting prefix within the first {CHUNK_WALK_CAP} of "
            f"{len(commits)} commits ({base[:8]}..{head[:8]}) — falling back on the first commit")
    first = commits[0]
    n_first = dl(first)                                  # memoized: base..first was already sized in
    b_first = db(first)                                  # the walk loop (or == base..head at the top)
    if n_first is None or b_first is None:
        # sizing failed for the very commit we'd skip — advancing on an UNKNOWN size could
        # silently skip a reviewable commit, and dispatching the known-over-budget range
        # would bounce off the worker caps. Do nothing this tick; sizing heals, we retry.
        return ("defer", first, -1, -1)
    if b_first == 0:
        return ("advance", first, 0, 0)                  # truly empty — silent advance
    # non-empty and not a candidate above => over a budget (a fitting non-empty first
    # commit would have been picked as `best`; a single-commit range IS the total).
    return ("advance", first, n_first, b_first)


def _transient_marker(repo: Repo, ref: str, sha: str) -> Path:
    # Scoped transient-<repo>-<ref>-<sha>: a bare transient-<sha> was GLOBAL, so two watched
    # repos sharing a commit sha (e.g. forks) could steal each other's refunds. Keyed on the
    # BASENAME of the repo path — NOT rid — because the worker derives its repo_id as
    # `basename "$repo"` (play-review.sh) and the two sides must match even if a repos.json
    # key ever diverges from the dir name. _slug MUST stay identical to the worker's bash
    # substitution ${var//[^A-Za-z0-9._-]/-} (contract-tested in test_controller).
    return STATE / f"transient-{_slug(Path(repo.path).name)}-{_slug(ref)}-{sha}"


def _transient_streak_file(rid: str) -> Path:
    return STATE / f"transient-streak-{_slug(rid)}"


def _bump_transient_streak(rid: str) -> int:
    f = _transient_streak_file(rid)
    try:
        n = int(f.read_text().strip() or "0")
    except (OSError, ValueError):
        n = 0
    n += 1
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(str(n))
    except OSError:
        pass
    return n


def _reset_transient_streak(rid: str) -> None:
    try:
        _transient_streak_file(rid).unlink()
    except OSError:
        pass


# defer streak: a "defer" from the chunker (sizing failed at the skip decision) is silent
# and costs no attempt — persistent sizing failure (e.g. _diff_bytes timing out on a huge
# generated file every tick) would otherwise wedge the cursor FOREVER with no ceiling and
# no alert (kilabz R2). Mirror the transient-streak pattern: alert once at the threshold.
DEFER_ALERT_STREAK = _int_env("MYNDAIX_CONTROLLER_DEFER_STREAK", 3)


def _defer_streak_file(rid: str) -> Path:
    return STATE / f"defer-streak-{_slug(rid)}"


def _bump_defer_streak(rid: str) -> int:
    f = _defer_streak_file(rid)
    try:
        n = int(f.read_text().strip() or "0")
    except (OSError, ValueError):
        n = 0
    n += 1
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(str(n))
    except OSError:
        pass
    return n


def _reset_defer_streak(rid: str) -> None:
    try:
        _defer_streak_file(rid).unlink()
    except OSError:
        pass


async def _try_forgive_transient(led: PostgresLedger, repo: Repo, rid: str, ref: str,
                                 sha: str) -> bool:
    """Consume this head's transient marker (unlink-FIRST, so a crash between the steps loses
    one refund rather than double-forgiving a FUTURE dispatch of the same sha) and refund the
    attempt in the ledger. Shared by process_repo's transient pass AND its blocked-ceiling
    re-check so the consume/forgive/streak logic cannot drift between the two sites. Returns
    True iff an attempt was forgiven (callers log their own site-specific line)."""
    tm = _transient_marker(repo, ref, sha)
    try:                                                 # EAFP: absent = nothing to forgive; the
        raw = tm.read_text(errors="replace")             # worker writes its cause label here
    except FileNotFoundError:
        return False
    except OSError as e:                                 # present but unreadable: still consume +
        log(f"{rid}: transient marker for {sha[:8]} unreadable ({type(e).__name__}) — "
            f"alerting without a cause label")           # forgive; only the label is lost
        raw = ""
    cause = _TRANSIENT_CAUSE_STRIP.sub("", raw)[:80].strip()
    try:
        tm.unlink()                                      # consume: a stale marker must not
    except OSError:                                      # forgive a FUTURE dispatch of this sha
        log(f"{rid}: could not consume transient marker for {sha[:8]} — skipping forgive")
        return False
    if not await led.forgive_transient(rid, ref, sha):
        return False
    if _bump_transient_streak(rid) == TRANSIENT_ALERT_STREAK:
        _alert_jefe(f"review backstop: transient canary failures on {rid}"
                    + (f" — {cause}" if cause else ""),
                    f"{TRANSIENT_ALERT_STREAK} consecutive review dispatches for {rid} "
                    f"aborted at the canary stage (latest cause: {cause or 'not recorded'}). "
                    f"OUT OF CREDITS = add credits (the login is fine); USAGE LIMIT = resets on "
                    f"its own; AUTH 401 = re-login that CLI on this machine; POOL = check serve; "
                    f"LOCK CONTENTION = a review lock is held (it stale-reaps on its own). "
                    f"The controller refunds attempts and retries each tick, so nothing is "
                    f"blocked, but merged code stays unreviewed until the cause is fixed.")
    return True


def _pin(repo: Repo, refname: str, sha: str) -> bool:
    """Anchor a sha behind a controller-owned ref so git gc can't prune it (codex M4 /
    Oracle MAJOR: a force-pushed-away sha would else fail cat-file forever). Returns the
    update-ref success so callers can refuse to advance onto an unpinnable object."""
    return _git(repo.path, "update-ref", refname, sha).returncode == 0


# -- +learning rung: skill indexer + branch-protection provenance (build plan Step 5) ---
# Runs EVERY tick regardless of cursor state. The unforgeable arm: `skills/` is in automerge's
# _DENY_DIRS, so any SKILL.md on main arrived via a HUMAN merge under branch protection ->
# provenance='promoted'. Re-verified every poll; fail-CLOSED — missing/weak/unreadable
# protection writes a per-repo block flag (read by skillselect) + alerts, indexes nothing.
def _skill_block_flag(rid: str) -> Path:
    return STATE / f"skills-blocked-{rid}"


def _skill_tree_file(rid: str) -> Path:
    return STATE / f"skills-tree-{rid}"          # disposable change-detect tally (the skills/ tree sha)


def _skill_taint_file(rid: str) -> Path:
    return STATE / f"skills-taint-{rid}"         # debounces the "changed-while-blocked" alert (one per tainted tree)


def _clear_taint(rid: str) -> None:
    try:
        _skill_taint_file(rid).unlink()
    except OSError:
        pass


def _alert_jefe(subject: str, body: str) -> bool:
    """Best-effort, atomic LOUD alert to the human inbox. Never raises (alerts must not sink a
    tick); returns True iff the alert was durably written — the oversized-skip path advances the
    cursor ONLY on a written flag (a silent unreviewed skip must be impossible, workflow #13).
    DRY_RUN-gated by callers. The filename carries a random token, NOT just a 1-second
    timestamp — two repos blocked in the same tick-second would otherwise os.replace to the SAME
    path and silently destroy one alert (oracle MAJOR)."""
    try:
        JEFE_INBOX.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
        tok = uuid.uuid4().hex[:8]
        text = f"---\nfrom: controller\nto: jefe\ntype: alert\nsubject: {subject}\n---\n\n{body}\n"
        tmp = JEFE_INBOX / f"{ts}-{tok}-skills-controller.md.tmp"
        tmp.write_text(text)
        os.replace(tmp, JEFE_INBOX / f"{ts}-{tok}-skills-controller.md")   # atomic; daemon skips the brief .tmp
        return True
    except OSError as e:
        log(f"jefe alert write failed ({e})")
        return False


def _block_repo_skills(rid: str, reason: str) -> None:
    """Fail-closed: write the per-repo block flag (consumed by skillselect) + alert jefe. A
    later protection downgrade can't grandfather an already-indexed skill — selection no-ops
    the instant this flag exists. DRY_RUN logs only."""
    log(f"{rid}: SKILLS BLOCKED — {reason}")
    if DRY_RUN:
        log(f"{rid}: DRY-RUN would write block flag + alert"); return
    already_blocked = _skill_block_flag(rid).exists()    # debounce: alert ONLY on the transition into blocked
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        _skill_block_flag(rid).write_text(reason + "\n")
    except OSError as e:
        # the block flag IS the fail-closed control; if it can't be persisted (disk full / perms),
        # its absence would leave selection ENABLED for a repo meant to be locked (oracle CRITICAL
        # fail-open). Fail closed by DISARMING globally — unlink frees space so it survives a full
        # disk, reliably stopping ALL injection until the human fixes the disk + re-arms.
        log(f"{rid}: CANNOT persist skills block flag ({e}) — DISARMING globally (fail-closed)")
        disarmed = True
        try:
            SKILLS_ENABLED.unlink()
        except FileNotFoundError:
            pass                                         # already absent = already disarmed (success)
        except OSError as ue:                            # could NOT remove it -> STILL armed -> STILL fail-open
            disarmed = False
            log(f"{rid}: COULD NOT remove SKILLS_ENABLED ({ue}) — selection may be FAIL-OPEN")
        # the alert must reflect reality — never claim DISARMED if the unlink failed (kilabz R5)
        if disarmed:
            _alert_jefe(f"review-skills DISARMED ({rid}: block-flag write failed)",
                        f"Could not write `{_skill_block_flag(rid).name}` ({e}). To fail CLOSED, "
                        f"`$ORCH/SKILLS_ENABLED` was removed — selection is now OFF for ALL repos. "
                        f"Fix the disk/permissions issue, then `touch $ORCH/SKILLS_ENABLED` to re-arm.")
        else:
            _alert_jefe(f"review-skills FAIL-OPEN ({rid}: could not disarm)",
                        f"Could not write `{_skill_block_flag(rid).name}` ({e}) AND could not remove "
                        f"`$ORCH/SKILLS_ENABLED` — selection may STILL be ON despite a repo that should "
                        f"be blocked. MANUALLY `rm $ORCH/SKILLS_ENABLED` NOW, then fix the disk/perms.")
        return
    if already_blocked:
        return                                           # an hourly re-block stays quiet (the flag already disables selection)
    _alert_jefe(f"review-skills BLOCKED for {rid}",
                f"Skill selection is DISABLED for `{rid}` (fail-closed): {reason}\n\n"
                f"No skills will be injected for this repo until the WATCHED branch has full "
                f"protection (required PR review + enforce admins + no force-push) and the next "
                f"controller tick clears `{_skill_block_flag(rid).name}`.")


def _branch_protection_ok(repo: Repo, nwo: str) -> bool:
    """True iff the ACTUALLY WATCHED branch (repo.watch_ref) requires PR review, enforces admins
    (so NObody pushes directly), and forbids force-push — the three conditions that make 'arrived
    via a human merge' unforgeable. Verifying a hard-coded 'main' would let a repo configured to
    watch refs/heads/dev promote skills from an UNPROTECTED branch while main stays protected
    (kilabz HIGH) — skills are indexed from this same watched ref. Any missing field, weakened
    setting, 404 (unprotected), or gh error -> False (fail-closed)."""
    branch = repo.watch_ref[len("refs/heads/"):]     # _REF_RE guarantees the refs/heads/ prefix
    # defense-in-depth before interpolating into the gh API path: _REF_RE is looser than git's
    # own ref rules (it permits `..`/leading-dot), and though `git fetch` would reject such a ref
    # earlier and repos.json is operator-trusted, refuse a traversing/empty branch here too.
    if not branch or ".." in branch or branch.startswith(".") or "/." in branch:
        return False
    # URL-ENCODE the branch: a slashed name (release/2026, feature/x — allowed by _REF_RE) left
    # raw would split the gh api path (.../branches/release/2026/protection) into extra segments
    # -> 404 -> fail-CLOSED indexing for a perfectly valid watched branch (kilabz+oracle). quote()
    # is a no-op for the common slashless `main`. (It also defangs any `..` between encoded slashes.)
    prot = _gh_json(repo.path, "api", f"repos/{nwo}/branches/{quote(branch, safe='')}/protection")
    if not isinstance(prot, dict):
        return False                                 # 404 unprotected / no access / gh error
    req_pr = isinstance(prot.get("required_pull_request_reviews"), dict)
    admins = bool((prot.get("enforce_admins") or {}).get("enabled"))
    no_force = not bool((prot.get("allow_force_pushes") or {}).get("enabled"))
    return req_pr and admins and no_force


async def _index_skills(led: PostgresLedger, repo: Repo) -> None:
    """Verify provenance + (re)index the repo's skills/ from the TRUSTED fetched owned ref.
    Wrapped by process_repo's per-repo try/except, so a failure here never sinks the tick."""
    rid, ref = repo.repo_id, repo.watch_ref

    # 1. resolve nameWithOwner (one gh call/tick) — needed for the protection endpoint
    info = _gh_json(repo.path, "repo", "view", "--json", "nameWithOwner")
    nwo = info.get("nameWithOwner") if isinstance(info, dict) else None
    if not isinstance(nwo, str) or "/" not in nwo:
        _block_repo_skills(rid, "cannot resolve nameWithOwner via gh (no remote/auth)"); return

    # 2-3. branch protection (of the WATCHED branch) is the arm — fail-closed on anything but full
    if not _branch_protection_ok(repo, nwo):
        _block_repo_skills(rid, "watched-branch protection missing/weak/unreadable "
                                "(need required PR review + enforce admins + no force-push)"); return

    # 4. compute the skills/ tree sha (read ONLY from the trusted fetched owned ref, never the
    # worktree) + the last successfully-indexed tree sha.
    head_ref = _ctl_head_ref(ref)
    tr = _git(repo.path, "rev-parse", f"{head_ref}:{SKILLS_DIR}")
    tree_sha = tr.stdout.strip() if tr.returncode == 0 else "none"   # "none" -> no skills/ dir on the ref
    try:
        prev = _skill_tree_file(rid).read_text().strip()
    except OSError:
        prev = ""

    # 4b. block-flag handling with an anti-LAUNDER taint check (kilabz MEDIUM): if we were blocked
    # (protection down/unreadable) and skills/ CHANGED while blocked, a skill could have been
    # DIRECT-PUSHED during the unprotected window — clearing + indexing would launder it as
    # 'promoted'. Stay blocked, require a HUMAN audit + manual re-arm. Auto-clear ONLY when nothing
    # changed (a transient unreadable-protection blip) or there are no skills to launder.
    bf = _skill_block_flag(rid)
    if bf.exists():
        # a MEANINGFUL change while blocked = the tree differs from the last protected index,
        # EXCEPT the trivial never-had-skills-still-none case. This INCLUDES a deletion of skills/
        # (tree -> "none" from a real prior tree): a delete archives all skills and must also be
        # audited (kilabz: the old `tree_sha != "none"` guard laundered a delete-while-blocked).
        changed_while_blocked = (tree_sha != prev) and not (tree_sha == "none" and prev in ("", "none"))
        if changed_while_blocked:
            log(f"{rid}: protection restored but skills/ CHANGED while blocked — staying blocked "
                f"(possible direct-push/delete in the unprotected window; manual audit + re-arm)")
            if not DRY_RUN:
                tf = _skill_taint_file(rid)
                try:
                    was = tf.read_text().strip()
                except OSError:
                    was = ""
                if was != tree_sha:                  # debounce: one alert per distinct tainted tree
                    _alert_jefe(f"review-skills TAINTED for {rid}",
                                f"`{rid}`: branch protection was restored, but skills/ CHANGED while "
                                f"the repo was BLOCKED — a skill may have been direct-pushed during "
                                f"the unprotected window. Selection stays DISABLED.\n\nAUDIT "
                                f"`git log -p {ref} -- {SKILLS_DIR}/` for any change NOT from a "
                                f"reviewed PR merge, revert anything suspect, then `rm {bf.name}` to "
                                f"re-arm. The next tick re-indexes from the protected ref.")
                    try:
                        tf.write_text(tree_sha + "\n")
                    except OSError:
                        pass
            return                                   # stay blocked — do NOT launder
        if not DRY_RUN:                              # safe: no skills, or unchanged since last protected index
            try:
                bf.unlink(); log(f"{rid}: protection restored, skills/ unchanged — cleared block flag")
            except OSError:
                pass
            _clear_taint(rid)
    elif not DRY_RUN:
        _clear_taint(rid)                            # not blocked -> any prior taint is resolved

    # 4c. change-detect: skills/ unchanged since the last successful index -> nothing to do
    if tree_sha == prev and prev != "":
        return

    # 5. read + lint each skills/<name>/SKILL.md from the owned ref (pure lint in skillmatch)
    skills: list[dict] = []
    rejects: list[tuple[str, str]] = []
    if tree_sha != "none":
        ls = _git(repo.path, "ls-tree", "-r", "--name-only", head_ref, "--", f"{SKILLS_DIR}/")
        if ls.returncode != 0:
            log(f"{rid}: ls-tree {SKILLS_DIR}/ failed — skip indexing this tick"); return
        for p in ls.stdout.splitlines():
            parts = p.strip().split("/")
            if len(parts) != 3 or parts[0] != SKILLS_DIR or parts[2] != SKILL_FILE:
                continue                             # only skills/<name>/SKILL.md (ignore nested/aux files)
            name = parts[1]
            blob = _git(repo.path, "cat-file", "-p", f"{head_ref}:{p.strip()}")
            if blob.returncode != 0:
                rejects.append((p.strip(), "cat-file failed")); continue
            skill, why = skillmatch.lint_skill(name, blob.stdout)
            if skill is None:
                rejects.append((p.strip(), why)); continue
            # K4 (cross-family): an auto-proposed draft whose real lesson was never authored carries
            # capture.STUB_MARKER and PASSES lint — but indexing it would inject a no-op "flag any
            # change…" skill into every future review (selection prefers unused skills + admits few,
            # so a stub DISPLACES real guidance). Refuse it here — the load-bearing gate — so even a
            # human who merges an unedited stub can NEVER pollute the injected corpus. Authoring the
            # lesson deletes the marker and the skill indexes normally on the next tick.
            if capture.is_unauthored_stub(blob.stdout):
                rejects.append((p.strip(), "un-authored auto-proposed stub (STUB_MARKER present) — "
                                           "replace the body with the real lesson to promote")); continue
            skill["content_sha"] = hashlib.sha256(blob.stdout.encode()).hexdigest()
            skill["body_sha"] = hashlib.sha256(skill["body"].encode()).hexdigest()
            skills.append(skill)

    if rejects and not DRY_RUN:
        listing = "\n".join(f"- `{p}`: {why}" for p, why in rejects)
        _alert_jefe(f"review-skill lint rejected {len(rejects)} for {rid}",
                    f"These SKILL.md files on main did NOT promote (lint is fail-closed):\n\n"
                    f"{listing}\n\nThey are not indexed. Fix + re-merge to promote.")

    if DRY_RUN:
        log(f"{rid}: DRY-RUN would index {len(skills)} skill(s), reject {len(rejects)} "
            f"(skills/ tree {tree_sha[:8] if tree_sha != 'none' else 'none'})"); return

    try:
        res = await led.index_skills(rid, skills)
    except Exception as e:                           # a DB CHECK backstop (belt) -> alert, do NOT advance the marker
        log(f"{rid}: index_skills raised ({e}) — not advancing tree marker")
        _alert_jefe(f"review-skill index FAILED for {rid}",
                    f"index_skills raised (a DB CHECK backstop tripped, or the table is absent "
                    f"pre-migration): {e}\nNo tree marker written; will retry next tick."); return
    try:
        _skill_tree_file(rid).write_text(tree_sha + "\n")
    except OSError:
        pass
    log(f"{rid}: indexed skills {res} ({len(rejects)} rejected, "
        f"tree {tree_sha[:8] if tree_sha != 'none' else 'none'})")


async def process_repo(led: PostgresLedger, repo: Repo, budget: list[int]) -> None:
    rid, ref = repo.repo_id, repo.watch_ref
    cur = await led.get_cursor(rid, ref)

    # advance pass: a prior dispatch whose review DELIVERED moves the cursor. The signal is
    # play-review's post-delivery done-<sha> marker; the controller passes an empty remote URL
    # so confirm_pushed treats the dispatch as pushed and writes the marker unconditionally
    # (branch-move-proof) without ls-remote or a PLAY_FORCE_DONE bypass.
    if cur and cur["pending_sha"]:
        ps = cur["pending_sha"]
        if _done(repo, ref, ps):
            # anchor the new base against gc BEFORE advancing onto it; if the object is
            # somehow gone, don't advance onto an unpinnable sha (would wedge cat-file).
            if not _pin(repo, _ctl_reviewed_ref(ref), ps):
                log(f"{rid}: cannot pin reviewed {ps[:8]} — not advancing")
            elif await led.advance_cursor(rid, ref, ps):
                log(f"{rid}: cursor advanced to {ps[:8]} (review delivered)")
                _reset_transient_streak(rid)
            cur = await led.get_cursor(rid, ref)

    # transient-abort pass: a canary-stage abort (or lock contention) is pool/agent flakiness,
    # never a poison head (2026-06-30: three such aborts hard-blocked the backstop until a new
    # head landed). The worker writes transient-<repo>-<ref>-<tip>; consume it exactly once ->
    # refund the attempt + force the pending row stale so the decide pass below re-dispatches
    # THIS tick instead of waiting out PENDING_STALE. The blocked ceiling then counts only
    # non-transient failures. A streak of forgives without a delivery surfaces the outage to
    # Jefe ONCE (== so re-alerts need a new streak), but NEVER blocks — retrying a canary is
    # cheap and self-heals when the pool does.
    if cur and cur["pending_sha"] and not _done(repo, ref, cur["pending_sha"]):
        ps = cur["pending_sha"]
        if await _try_forgive_transient(led, repo, rid, ref, ps):
            log(f"{rid}: transient abort on {ps[:8]} — attempt refunded, slot released")
            cur = await led.get_cursor(rid, ref)

    url = remote_url(repo)                                # validate transport BEFORE fetch (M1)
    if url is None:
        return

    # observe: fetch into a controller-OWNED ref (no FETCH_HEAD race, gc-safe — codex M3/M4)
    fr = _git(repo.path, "fetch", "--no-tags", "--no-recurse-submodules", "--quiet",
              "origin", f"+{ref}:{_ctl_head_ref(ref)}", timeout=FETCH_TIMEOUT)
    if fr.returncode != 0:
        log(f"{rid}: fetch failed ({fr.stderr.strip()[:120]}) — skip this tick"); return
    hr = _git(repo.path, "rev-parse", _ctl_head_ref(ref))
    head = hr.stdout.strip()
    if hr.returncode != 0 or not _SHA_RE.match(head):
        log(f"{rid}: could not resolve a valid HEAD sha — skip"); return

    # +learning rung (Step 5): verify provenance + (re)index skills/ from the just-fetched owned
    # ref. Runs on EVERY tick regardless of cursor state (protection must be re-checked each poll).
    # The indexer is OPTIONAL and must NEVER disrupt the review path below — any failure inside it
    # (gh down/hung, DB hiccup, lint bug) is swallowed here so reviews keep running (kilabz HIGH).
    try:
        await _index_skills(led, repo)
    except Exception as e:
        log(f"{rid}: skill indexer error ({e!r}) — continuing with the review")

    # decide
    if cur is None:                                      # first sight (B2): seed, do NOT review
        if DRY_RUN:
            log(f"{rid}: DRY-RUN would seed baseline {head[:8]}"); return
        # anchor the baseline base vs gc FIRST; only seed if the pin holds, else a later
        # cat-file on an unpinned base could wedge (workflow MAJOR — matches the advance path).
        if not _pin(repo, _ctl_reviewed_ref(ref), head):
            log(f"{rid}: could not pin baseline {head[:8]} — not seeding this tick"); return
        if await led.upsert_baseline(rid, ref, head):
            log(f"{rid}: seeded baseline {head[:8]} (not reviewed)")
        return
    if head == cur["reviewed_sha"]:
        return                                           # up to date

    base = cur["reviewed_sha"]                            # review reviewed_sha..head (never empty-tree)
    if _git(repo.path, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0 or \
       _git(repo.path, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        log(f"{rid}: head/base objects not present locally — skip"); return

    # nothing-to-review short-circuit: base..head has no net diff (empty/revert-net-zero commit).
    # play-review aborts on an empty diff and never marks done, so dispatching would re-try to the
    # BLOCKED ceiling (workflow MAJOR). Advance straight past it instead.
    if _git(repo.path, "diff", "--quiet", "--no-ext-diff", base, head).returncode == 0:
        if DRY_RUN:
            log(f"{rid}: DRY-RUN would skip empty-diff {head[:8]} (advance, no review)"); return
        if _pin(repo, _ctl_reviewed_ref(ref), head) and await led.skip_to(rid, ref, head):
            log(f"{rid}: no net diff {base[:8]}..{head[:8]} — advanced without review")
        return

    # review-size budget: a range over MAX_REVIEW_LINES/MAX_REVIEW_BYTES would time the
    # reviewer out (or bounce off the worker caps) — a NON-transient abort that burns
    # attempts and BLOCKS the cursor (the 2026-07-02 backlog wedge). Dispatch the largest
    # first-parent prefix that fits instead; everything downstream (ceiling/claim/pin/
    # trigger) is keyed on the TARGET.
    mode, target, tlines, tbytes = _choose_review_target(repo, base, head)
    if mode == "defer":
        log(f"{rid}: could not size {target[:8]} for chunking — deferring to next tick")
        if _bump_defer_streak(rid) == DEFER_ALERT_STREAK and not DRY_RUN:
            _alert_jefe(
                f"review backstop: {rid} cannot size {target[:8]} — backstop stalled",
                f"The chunker has failed to size {target} on {ref} for "
                f"{DEFER_ALERT_STREAK} consecutive ticks (git diff/numstat failing or "
                f"timing out — a huge generated file?). The review cursor is parked at "
                f"{base} and nothing is being reviewed for {rid} until this clears. "
                f"Try `git -C {repo.path} diff --numstat {base} {target}` by hand.")
        return
    _reset_defer_streak(rid)
    if mode == "advance":
        # no reviewable prefix: the target is either an unsplittable over-budget commit
        # (flag — a human must review it), a rewritten/force-pushed history with no
        # walkable prefix (also flagged), or truly empty vs base (silent).
        # NOTE: like the empty-diff skip above, skip_to clears any pending row. Reaching
        # here WITH a fresh in-flight pending requires the budget to have been re-tuned
        # mid-flight (target choice is deterministic per base) — the in-flight worker
        # still delivers its verdict to the inbox; only the cursor bookkeeping moves on.
        oversized = tlines > MAX_REVIEW_LINES or tbytes > MAX_REVIEW_BYTES or tbytes < 0
        if DRY_RUN:
            log(f"{rid}: DRY-RUN would advance past {'oversized' if oversized else 'empty'} "
                f"{target[:8]} ({tlines} lines / {tbytes}B) without review"); return
        if oversized:
            # flag FIRST, advance second (workflow #5/#13): the alert is the ONLY human
            # signal that unreviewed code passed the backstop — if it cannot be written
            # durably, do NOT advance; stay wedged LOUDLY and retry next tick (fail-safe).
            bstr = str(tbytes) if tbytes >= 0 else "unknown"
            ok = _alert_jefe(
                f"review backstop: {rid} range too large — SKIPPED, needs a human",
                f"The diff from reviewed {base[:8]} to {target} on {ref} spans "
                f"{tlines} changed lines / {bstr} bytes — over the autonomous review "
                f"budget (lines {MAX_REVIEW_LINES} / bytes {MAX_REVIEW_BYTES}; one "
                f"reviewer call times out around ~2000 lines). (On a force-pushed/"
                f"diverged history that is the REVIEW PATH size, which can far exceed "
                f"the commit's own size.) It cannot be split into smaller reviewable "
                f"steps, so the controller is now advancing the cursor past it WITHOUT "
                f"review to keep the backstop unwedged. (This alert is written BEFORE "
                f"the advance so a skip can never be silent — a repeated copy of this "
                f"alert means the advance failed and is being retried.)\n\n"
                f"This range is UNREVIEWED by the backstop. Review it manually:\n"
                f"    git -C {repo.path} show --stat {target}\n"
                f"    git -C {repo.path} diff {base} {target}\n\n"
                f"If it was already PR-reviewed, nothing else to do. To let bigger "
                f"ranges through, raise MYNDAIX_CONTROLLER_MAX_REVIEW_LINES / "
                f"MYNDAIX_CONTROLLER_MAX_REVIEW_BYTES.")
            if not ok:
                log(f"{rid}: could NOT write the skip flag for {target[:8]} — "
                    f"not advancing (retry next tick)"); return
        if not _pin(repo, _ctl_reviewed_ref(ref), target):
            log(f"{rid}: cannot pin advance target {target[:8]} — skip this tick"); return
        if await led.skip_to(rid, ref, target):
            if oversized:
                log(f"{rid}: {target[:8]} is {tlines} lines / {tbytes}B (budget "
                    f"{MAX_REVIEW_LINES}/{MAX_REVIEW_BYTES}) — advanced past WITHOUT "
                    f"review, flagged to jefe")
            else:
                log(f"{rid}: empty prefix {target[:8]} — advanced without review")
        return
    if target != head:
        log(f"{rid}: {base[:8]}..{head[:8]} over review budget — chunking to "
            f"{target[:8]} ({tlines} lines / {tbytes}B); remainder follows on later ticks")

    # ceiling: stop chasing a target that has failed MAX_ATTEMPTS dispatches. Keyed on the
    # TARGET (== head for an in-budget range). A blocked CHUNK does not self-heal on a new
    # push (the target is a function of the unchanged base), so mark_blocked must be LOUD.
    if cur["pending_sha"] == target and cur["attempts"] >= MAX_ATTEMPTS and cur["state"] != "blocked":
        # marker-after-pass race: the worker's abort can land the marker AFTER this tick's
        # transient pass but before this check. Blocking then would re-wedge like the original
        # bug — the NEXT tick's pass consumes the marker, but a plain forgive couldn't repair a
        # 'blocked' row and the marker is gone. Re-check + forgive HERE instead of blocking,
        # then fall through so the decide pass below re-claims THIS tick.
        if await _try_forgive_transient(led, repo, rid, ref, target):
            log(f"{rid}: transient abort on {target[:8]} at the ceiling — attempt refunded "
                f"instead of blocking")
        else:
            if await led.mark_blocked(rid, ref, target, MAX_ATTEMPTS):
                log(f"{rid}: BLOCKED {target[:8]} after {cur['attempts']} attempts — surfaced, backing off")
                if not DRY_RUN:                          # DRY_RUN contract: write nothing
                    _alert_jefe(
                        f"review backstop: {rid} BLOCKED after {cur['attempts']} failed reviews",
                        f"Review dispatches for {target} on {ref} ({rid}) failed "
                        f"{cur['attempts']} times (non-transient) — the controller stopped "
                        f"retrying. The review cursor is WEDGED at {base} until this clears.\n\n"
                        f"Check the newest run under ~/.myndaix/orchestrator/runs/ (play.jsonl "
                        f"+ *.err) for the abort stage. A new push clears a blocked full head "
                        f"but NOT a blocked chunk of a backlog — if this is a chunk, fix the "
                        f"cause (check BOTH caps: MYNDAIX_CONTROLLER_MAX_REVIEW_LINES and "
                        f"MYNDAIX_CONTROLLER_MAX_REVIEW_BYTES), then clear the "
                        f"row: UPDATE review_cursor SET pending_sha=NULL, state='delivered', "
                        f"attempts=0 WHERE repo_id='{rid}' AND ref='{ref}';")
            return

    if budget[0] >= MAX_DISPATCH_PER_TICK:
        log(f"{rid}: per-tick dispatch budget reached — deferring {target[:8]} to next tick"); return
    if not DRY_RUN and _day_count() >= MAX_DISPATCH_PER_DAY:  # daily gate wraps ONLY dispatch, so the
        log(f"{rid}: daily dispatch budget reached — observing only"); return  # advance pass above always runs

    if DRY_RUN:
        log(f"{rid}: DRY-RUN would dispatch review {base[:8]}..{target[:8]}"); return

    stale_before = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=PENDING_STALE)
    if not await led.claim_dispatch(rid, ref, target, stale_before):
        log(f"{rid}: dispatch not claimed (in flight or blocked) — skip"); return
    # anchor the in-flight target against gc BEFORE dispatch: a later force-push overwrites the
    # head ref while this sha is still pending, so without its own ref gc could prune it before
    # advance (codex MAJOR). If the anchor can't be written, do NOT dispatch unanchored — release
    # + retry. (A chunk target is behind head, so it stays reachable anyway; the pin is uniform.)
    if not _pin(repo, _ctl_pending_ref(ref), target):
        log(f"{rid}: could not pin pending {target[:8]} — releasing, will retry next tick")
        await led.release_dispatch(rid, ref, target); return
    if trigger_review(repo, target, base):
        budget[0] += 1
        _charge_day()
    else:                                                # FRONT failed -> un-stick now, don't wait out PENDING_STALE
        await led.release_dispatch(rid, ref, target)
        log(f"{rid}: released dispatch after trigger failure — will retry next tick")


# -- daily dispatch budget (UTC-keyed file counter, mirrors play-review's cap) ---
def _day_file() -> Path:
    return STATE / f"controller-day-{_utcday()}"


def _day_count() -> int:
    try:
        return int(_day_file().read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def _charge_day() -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        _day_file().write_text(str(_day_count() + 1))
    except OSError:
        pass


async def tick() -> int:
    repos = load_config()
    if not repos:
        return 0
    if (ORCH / "AUTOFIX_ENABLED").exists():
        log("note: AUTOFIX_ENABLED is armed — brain reviews still NEVER auto-fix (PLAY_DISABLE_AUTOFIX=1)")
    if not acquire_lock():
        return 0
    try:
        if not DRY_RUN and _day_count() >= MAX_DISPATCH_PER_DAY:
            log(f"daily dispatch budget ({MAX_DISPATCH_PER_DAY}) reached — advancing only, no new dispatches")
        led = await PostgresLedger.connect(DSN)
        budget = [0]
        try:
            # Self-migrate before any schema-dependent work (mirrors serve()'s auto-migrate-on-boot).
            # The controller is a SEPARATE launchd job, so a deploy could tick it BEFORE serve has
            # applied a new migration — e.g. 0006's skill-PK heal — which would make index_skills'
            # ON CONFLICT hit a stale PK (kilabz R3). migrate() is advisory-locked + idempotent, so
            # racing serve is safe. A migration FAILURE means a schema unsafe for EVERYTHING (not
            # just skills) -> skip the whole tick fail-closed, like serve refusing to boot. Skipped
            # under DRY_RUN (it writes).
            if not DRY_RUN:
                try:
                    await led.migrate()
                except Exception as e:
                    log(f"migrate() failed ({e!r}) — skipping this tick (schema not safe)"); return 0
            for repo in repos:                           # the daily gate lives INSIDE process_repo (wraps
                try:                                     # only dispatch), so the free advance pass always runs
                    await process_repo(led, repo, budget)
                except Exception as e:                   # one bad repo never sinks the tick
                    log(f"{repo.repo_id}: tick error {e!r}")
            # +learning rung (Step 5): prune the skill lifecycle ONCE per tick — inline, no
            # separate cron (v0.3 #7). Time-based + global, so it's decoupled from any per-repo
            # change-detect skip. Fail-soft: a missing table (pre-migration) is logged, not fatal.
            if not DRY_RUN:
                try:
                    pruned = await led.prune_skills()
                    if pruned.get("staled") or pruned.get("archived"):
                        log(f"skills pruned {pruned}")
                except Exception as e:
                    log(f"prune_skills skipped ({e!r})")
        finally:
            await led.close()
        log(f"tick complete — {budget[0]} review(s) dispatched across {len(repos)} repo(s)")
        return 0
    finally:
        release_lock()


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "tick":
        print("usage: python -m runtime.controller tick", file=sys.stderr)
        return 2
    return asyncio.run(tick())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
