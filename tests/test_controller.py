"""controller.py proofs — the brain's tick over REAL temp git repos + a real ledger.

Each test builds a bare "remote" + a working clone, drives the level-triggered
reconcile, and asserts the decision (seed / dispatch / dedup / advance / block /
skip) via the cursor table + the TEST-MODE dispatch seam (records the would-be
play-review invocation instead of running it). No live pool / no play-review needed.

Setup (once):  createdb runtime_test
Run:
    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
        PYTHONPATH=src python3 tests/test_controller.py
"""
import asyncio
import contextlib
import datetime as _dt
import inspect
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

import asyncpg

from runtime.ledger.postgres_store import PostgresLedger
import runtime.controller as C

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
C.DSN = DSN

_TMP = Path(tempfile.mkdtemp(prefix="ctrl-test-"))
_N = [0]

# +learning indexer isolation: process_repo now calls _index_skills, which shells out to `gh`
# and (fail-closed) alerts the human inbox. Stub gh + redirect the inbox to _TMP so NO test
# hits real GitHub or the real ~/.myndaix inbox. Default = fully protected (so existing tests
# index an empty corpus = a clean no-op); skill tests override C._gh_json per-test.
_GH_PROT = {"required_pull_request_reviews": {}, "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False}}


def _make_gh(protected: bool = True, nwo: str = "o/r"):
    def _gh(repo, *args, timeout=None):
        if args[:2] == ("repo", "view"):
            return {"nameWithOwner": nwo}
        if args and args[0] == "api":                # the branch-protection endpoint
            return _GH_PROT if protected else None
        return None
    return _gh


C.JEFE_INBOX = _TMP / "jefe"
C._gh_json = _make_gh(True)


def g(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


def make_repo(name: str) -> C.Repo:
    bare = _TMP / f"{name}.git"
    work = _TMP / name
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    g(work, "config", "user.email", "t@t"); g(work, "config", "user.name", "t")
    (work / "f.txt").write_text("0\n")
    g(work, "add", "-A"); g(work, "commit", "-q", "-m", "c0")
    g(work, "remote", "add", "origin", f"file://{bare}")  # file:// = an allowed transport
    g(work, "push", "-q", "-u", "origin", "main")
    return C.Repo(name, work, "refs/heads/main")


def advance(repo: C.Repo, content: str) -> str:
    (repo.path / "f.txt").write_text(content + "\n")
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", content)
    g(repo.path, "push", "-q", "origin", "main")
    return g(repo.path, "rev-parse", "HEAD").stdout.strip()


def advance_empty(repo: C.Repo) -> str:
    g(repo.path, "commit", "-q", "--allow-empty", "-m", "empty"); g(repo.path, "push", "-q", "origin", "main")
    return g(repo.path, "rev-parse", "HEAD").stdout.strip()


def advance_lines(repo: C.Repo, fname: str, n: int) -> str:
    """One commit adding exactly n changed lines (a NEW n-line file — pure adds)."""
    (repo.path / fname).write_text("".join(f"line {i}\n" for i in range(n)))
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", fname)
    g(repo.path, "push", "-q", "origin", "main")
    return g(repo.path, "rev-parse", "HEAD").stdout.strip()


def head_of(repo: C.Repo) -> str:
    return g(repo.path, "rev-parse", "HEAD").stdout.strip()


def fresh_seam(name: str) -> Path:
    """Point STATE + the dispatch seam at a clean per-test dir; enable the test seam."""
    base = _TMP / f"seam-{name}-{_N[0]}"; _N[0] += 1
    state = base / "state"; state.mkdir(parents=True)
    C.STATE = state
    C.TEST_MODE = True
    C.DISPATCH_OVERRIDE = str(base / "dispatched.jsonl")
    C.DRY_RUN = False
    C.MAX_DISPATCH_PER_TICK = 3
    C.MAX_REVIEW_LINES = 1500                            # reset the chunking budgets (chunk tests shrink them)
    C.MAX_REVIEW_BYTES = 262144
    C._gh_json = _make_gh(True)                          # reset to protected default (skill tests override)
    C.JEFE_INBOX = _TMP / "jefe"
    return Path(C.DISPATCH_OVERRIDE)


def records(seam: Path) -> list[dict]:
    if not seam.exists():
        return []
    return [json.loads(l) for l in seam.read_text().splitlines() if l.strip()]


async def _truncate(led: PostgresLedger) -> None:
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE review_cursor RESTART IDENTITY CASCADE")


# -- first sight seeds a baseline and reviews NOTHING (B2) ----------------------
async def test_bootstrap_seeds_no_dispatch(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("boot"); repo = make_repo("boot")
    await C.process_repo(led, repo, [0])
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur is not None and cur["state"] == "baseline"
    assert cur["reviewed_sha"] == head_of(repo) and cur["pending_sha"] is None
    assert records(seam) == [], "the brain must NOT review on first sight"


# -- HEAD advances -> one dispatch of exactly reviewed..head -------------------
async def test_advance_dispatches_correct_range(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("adv"); repo = make_repo("adv")
    base = head_of(repo)
    await C.process_repo(led, repo, [0])                 # seed baseline at `base`
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # HEAD moved -> dispatch
    recs = records(seam)
    assert len(recs) == 1, f"expected one dispatch, got {len(recs)}"
    assert recs[0]["base"] == base and recs[0]["head"] == head2
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == head2 and cur["state"] == "dispatching"


# -- a fresh in-flight head is NOT re-dispatched (dedup) -----------------------
async def test_inflight_is_not_redispatched(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("dedup"); repo = make_repo("dedup")
    await C.process_repo(led, repo, [0])
    advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch #1
    await C.process_repo(led, repo, [0])                 # same head, in flight -> no-op
    assert len(records(seam)) == 1, "an in-flight head must not re-dispatch"


# -- a delivered review (done-<sha> marker) advances the cursor ---------------
async def test_done_marker_advances_cursor(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("done"); repo = make_repo("done")
    await C.process_repo(led, repo, [0])
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # pending = head2
    C._done_marker(repo, repo.watch_ref, head2).write_text("")   # play-review "delivered" (scoped marker)
    await C.process_repo(led, repo, [0])                 # advance pass picks it up
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == head2 and cur["pending_sha"] is None
    assert cur["state"] == "delivered"
    assert len(records(seam)) == 1, "no extra dispatch once delivered + up to date"


# -- a synchronous trigger failure releases the claim (no 1h stall) ------------
async def test_trigger_failure_releases(led: PostgresLedger) -> None:
    await _truncate(led)
    fresh_seam("trigfail"); repo = make_repo("trigfail")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance(repo, "c1")
    C.TEST_MODE = False; C.DISPATCH_OVERRIDE = ""        # force the REAL trigger path
    C.PLAY_REVIEW = repo.path / "no-such-play-review.sh" # untrusted/missing -> trigger fails
    try:
        await C.process_repo(led, repo, [0])             # claims, trigger fails, releases
    finally:
        C.TEST_MODE = True
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == head2 and cur["state"] == "dispatching"
    # released = updated_at forced stale -> a stale-cutoff re-claim fires immediately (no PENDING_STALE wait)
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
    assert await led.claim_dispatch(repo.repo_id, repo.watch_ref, head2, cutoff) is True


# -- the flock is exclusive: a second holder cannot acquire it -----------------
async def test_lock_is_exclusive(led: PostgresLedger) -> None:
    import fcntl
    C.LOCK = _TMP / "controller.lock"                    # don't touch the live orchestrator dir
    assert C.acquire_lock() is True
    fd2 = os.open(str(C.LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        raised = False
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raised = True
        assert raised, "a second flock must NOT acquire the held lock"
    finally:
        os.close(fd2)
    C.release_lock()
    assert C.acquire_lock() is True, "lock is re-acquirable after release"
    C.release_lock()


# -- a legacy mkdir-style lock DIRECTORY is reaped, not a permanent crash ------
async def test_legacy_lock_dir_is_reaped(led: PostgresLedger) -> None:
    import fcntl
    C.LOCK = _TMP / "controller.lock.legacy"
    C.LOCK.mkdir(parents=True, exist_ok=True)            # simulate the pre-v0.4 dir lock
    (C.LOCK / "pid").write_text("123")                   # non-empty -> needs rmtree, not rmdir
    assert C.acquire_lock() is True, "must reap a legacy dir lock and acquire"
    assert not C.LOCK.is_dir(), "legacy dir replaced by the flock file"
    C.release_lock()


# -- a new head while one is in flight WAITS (no supersede, Oracle B2) ---------
async def test_new_head_waits_for_inflight(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("wait"); repo = make_repo("wait")
    await C.process_repo(led, repo, [0])
    advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch head2 (in flight)
    advance(repo, "c2")                                  # head3 arrives before head2 delivered
    await C.process_repo(led, repo, [0])                 # must NOT dispatch head3
    assert len(records(seam)) == 1, "an in-flight head must not be superseded by a new head"


# -- a fetch failure skips the repo, never crashes, never seeds ----------------
async def test_fetch_failure_skips(led: PostgresLedger) -> None:
    await _truncate(led)
    fresh_seam("fetchfail"); repo = make_repo("fetchfail")
    g(repo.path, "remote", "set-url", "origin", str(_TMP / "does-not-exist.git"))
    await C.process_repo(led, repo, [0])                 # must not raise
    assert await led.get_cursor(repo.repo_id, repo.watch_ref) is None, "no cursor on a failed observe"


# -- the per-tick dispatch budget caps work across repos -----------------------
async def test_per_tick_budget(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("budget"); C.MAX_DISPATCH_PER_TICK = 1
    r1, r2 = make_repo("budgetA"), make_repo("budgetB")
    for r in (r1, r2):
        await C.process_repo(led, r, [0])               # seed both
        advance(r, "c1")
    budget = [0]
    await C.process_repo(led, r1, budget)               # spends the only slot
    await C.process_repo(led, r2, budget)               # deferred
    assert len(records(seam)) == 1, "per-tick budget must cap dispatches"


# -- a head that failed MAX_ATTEMPTS gets blocked, not re-dispatched ------------
async def test_blocked_after_max_attempts(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("block"); repo = make_repo("block")
    await C.process_repo(led, repo, [0])
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # pending=head2, attempts=1
    async with led._pool.acquire() as con:              # simulate the ceiling reached
        await con.execute(
            "UPDATE review_cursor SET attempts=$1 WHERE repo_id=$2",
            C.MAX_ATTEMPTS, repo.repo_id)
    before = len(records(seam))
    await C.process_repo(led, repo, [0])                 # must block, not dispatch
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["state"] == "blocked"
    assert len(records(seam)) == before, "a blocked head must not re-dispatch"


# -- a transient (canary-abort) marker refunds the attempt + re-dispatches THIS tick --
async def test_transient_marker_forgives_and_redispatches(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("transient"); repo = make_repo("transient")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch #1 (pending=head2, attempts=1)
    assert len(records(seam)) == 1
    marker = C._transient_marker(repo, repo.watch_ref, head2)
    marker.write_text("")                                # the worker's canary-abort marker
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await C.process_repo(led, repo, [0])             # forgive + SAME-tick re-dispatch
    assert not marker.exists(), "the marker must be consumed exactly once"
    assert "transient abort" in buf.getvalue(), "the forgive must be logged"
    assert len(records(seam)) == 2, "the same tick must re-dispatch after a forgive"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == head2 and cur["state"] == "dispatching"
    assert cur["attempts"] == 1, "refund + re-claim nets attempts flat (can't climb the ceiling)"


# -- a streak of transient forgives alerts jefe EXACTLY once; delivery resets it --
async def test_transient_streak_alerts_once_and_delivery_resets(led: PostgresLedger) -> None:
    await _truncate(led)
    fresh_seam("streak"); repo = make_repo("streak")
    alerts: list = []
    bodies: list = []
    saved = C._alert_jefe
    C._alert_jefe = lambda subject, body: (alerts.append(subject), bodies.append(body))
    try:
        await C.process_repo(led, repo, [0])             # baseline
        head2 = advance(repo, "c1")
        await C.process_repo(led, repo, [0])             # dispatch #1
        for _ in range(4):                               # 4 transient cycles: streak 1..4
            # the worker writes its cause label into the marker; hostile bytes must not survive
            C._transient_marker(repo, repo.watch_ref, head2).write_text(
                "kilabz OUT OF CREDITS\x1b[31m;$(rm -rf x)`|<b>\n")
            await C.process_repo(led, repo, [0])         # forgive + re-dispatch each tick
        assert len(alerts) == 1, f"alert fires ONLY at streak=={C.TRANSIENT_ALERT_STREAK}, got {len(alerts)}"
        assert "kilabz OUT OF CREDITS" in alerts[0], f"alert subject must name the cause: {alerts[0]!r}"
        assert "OUT OF CREDITS = add credits" in bodies[0], "alert body must map the cause to its action"
        for bad in ("\x1b", ";", "$", "`", "|", "<", "[", "\n"):
            assert bad not in alerts[0], f"{bad!r} survived the cause sanitizer: {alerts[0]!r}"
        assert C._transient_streak_file(repo.repo_id).exists(), "streak persists while the outage lasts"
        C._done_marker(repo, repo.watch_ref, head2).write_text("")       # the review finally delivers
        await C.process_repo(led, repo, [0])             # advance pass consumes the marker
        cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
        assert cur["reviewed_sha"] == head2 and cur["state"] == "delivered"
        assert not C._transient_streak_file(repo.repo_id).exists(), "a delivery resets the streak file"
    finally:
        C._alert_jefe = saved


# -- a marker for a sha that is NOT the pending head is left untouched ----------
async def test_stale_transient_marker_ignored(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("stalemark"); repo = make_repo("stalemark")
    await C.process_repo(led, repo, [0])                 # baseline
    advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch (pending=head2, attempts=1)
    foreign = C._transient_marker(repo, repo.watch_ref, "f" * 40)   # NOT the pending head
    foreign.write_text("")
    await C.process_repo(led, repo, [0])
    assert foreign.exists(), "a non-pending sha's marker must be left untouched"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["attempts"] == 1, "no forgive happened (attempts unchanged)"
    assert len(records(seam)) == 1, "no re-dispatch (the in-flight head stays deduped)"


# -- marker lands AFTER the transient pass, AT the ceiling -> forgive, never block --
async def test_ceiling_with_marker_forgives_and_redispatches(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("ceilmark"); repo = make_repo("ceilmark")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch #1 (pending=head2)
    async with led._pool.acquire() as con:               # simulate the ceiling reached
        await con.execute(
            "UPDATE review_cursor SET attempts=$1 WHERE repo_id=$2",
            C.MAX_ATTEMPTS, repo.repo_id)
    # plant the marker BETWEEN the transient pass and the ceiling check — the real race window
    # (_index_skills runs after fetch, before decide — exactly the gap the worker's abort races)
    saved = C._index_skills

    async def _plant(_led, _repo):
        C._transient_marker(repo, repo.watch_ref, head2).write_text("")

    C._index_skills = _plant
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            await C.process_repo(led, repo, [0])
    finally:
        C._index_skills = saved
    assert "at the ceiling" in buf.getvalue(), "the ceiling forgive must be logged"
    assert not C._transient_marker(repo, repo.watch_ref, head2).exists(), "marker consumed"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["state"] == "dispatching", "a marker at the ceiling forgives — never blocks"
    assert len(records(seam)) == 2, "the same tick must re-dispatch after a ceiling forgive"


# -- a marker for the SAME sha under a different repo or ref is NOT consumed (scoping) --
async def test_transient_marker_scoped_by_repo_and_ref(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("scopemark"); repo = make_repo("scopemark")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch (pending=head2, attempts=1)
    fork = C.Repo("scopefork", _TMP / "scopefork", repo.watch_ref)   # same sha, DIFFERENT repo
    m_repo = C._transient_marker(fork, repo.watch_ref, head2)
    m_ref = C._transient_marker(repo, "refs/heads/dev", head2)       # same repo, DIFFERENT ref
    m_repo.write_text(""); m_ref.write_text("")
    await C.process_repo(led, repo, [0])
    assert m_repo.exists() and m_ref.exists(), "foreign-scope markers must be left untouched"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["attempts"] == 1, "no forgive happened (attempts unchanged)"
    assert len(records(seam)) == 1, "no re-dispatch (the in-flight head stays deduped)"


# -- the bash (play-review.sh) and python marker derivations MUST agree ----------
async def test_transient_marker_bash_python_contract(led: PostgresLedger) -> None:
    fresh_seam("contract")
    tip = "a" * 40
    # a ref with slashes + a repo dir name with a dot — the CONTRACT is the file-name string
    repo = C.Repo("my.repo", _TMP / "somewhere" / "my.repo", "refs/heads/feat/x")
    expected = f"transient-my.repo-refs-heads-feat-x-{tip}"
    assert C._transient_marker(repo, repo.watch_ref, tip).name == expected
    # the worker's derivation, verbatim from play-review.sh (bash 3.2 pattern substitution)
    r = subprocess.run(
        ["bash", "-c",
         'repo_id="$(basename "$1")"; ref="$2"; tip="$3"; '
         'marker_slug="${repo_id//[^A-Za-z0-9._-]/-}-${ref//[^A-Za-z0-9._-]/-}"; '
         'printf "transient-%s-%s" "$marker_slug" "$tip"',
         "bash", str(repo.path), repo.watch_ref, tip],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected, f"bash and python marker names must agree: {r.stdout!r}"


# -- the DONE marker (drives cursor ADVANCE) must be repo/ref-scoped like the transient one -------
async def test_done_marker_scoped_by_repo_and_ref(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("donescope"); repo = make_repo("donescope")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch (pending=head2)
    # a FOREIGN-scope done marker for the same sha (another repo / another ref) must NOT advance us
    fork = C.Repo("donefork", _TMP / "donefork", repo.watch_ref)     # same sha, DIFFERENT repo
    C._done_marker(fork, repo.watch_ref, head2).write_text("")
    C._done_marker(repo, "refs/heads/dev", head2).write_text("")     # same repo, DIFFERENT ref
    await C.process_repo(led, repo, [0])
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == head2 and cur["reviewed_sha"] != head2, \
        "a done marker from a foreign (repo, ref) scope must NOT advance this cursor past unreviewed code"
    assert len(records(seam)) == 1, "still deduped in-flight (no re-dispatch)"


# -- the bash mark_done and python _done_marker derivations MUST agree ------------
def test_done_marker_bash_python_contract() -> None:
    tip = "b" * 40
    repo = C.Repo("my.repo", _TMP / "somewhere" / "my.repo", "refs/heads/feat/x")
    expected = f"done-my.repo-refs-heads-feat-x-{tip}"
    assert C._done_marker(repo, repo.watch_ref, tip).name == expected
    r = subprocess.run(
        ["bash", "-c",
         'repo_id="$(basename "$1")"; ref="$2"; tip="$3"; '
         'marker_slug="${repo_id//[^A-Za-z0-9._-]/-}-${ref//[^A-Za-z0-9._-]/-}"; '
         'printf "done-%s-%s" "$marker_slug" "$tip"',
         "bash", str(repo.path), repo.watch_ref, tip],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected, f"bash and python done-marker names must agree: {r.stdout!r}"


# -- config: duplicate basenames rejected; junk entries skipped ----------------
async def test_load_config_dedup_and_validation(led: PostgresLedger) -> None:
    a = make_repo("dup")                                  # basename 'dup'
    nested = _TMP / "other" / "dup"; nested.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)  # same basename, diff path
    notgit = _TMP / "plain"; notgit.mkdir(exist_ok=True)
    cfg = {
        "_comment": "ignored",
        "dup": {"path": str(a.path)},
        "dup2": {"path": str(nested)},                   # duplicate basename -> rejected
        "plain": {"path": str(notgit)},                  # not a git repo -> skipped
        "badref": {"path": str(a.path), "watch_ref": "refs/heads/m;rm -rf"},  # bad ref -> skipped
    }
    cfgfile = _TMP / "repos.json"; cfgfile.write_text(json.dumps(cfg))
    C.REPOS_JSON = cfgfile
    repos = C.load_config()
    assert len(repos) == 1 and repos[0].repo_id == "dup", f"got {[r.repo_id for r in repos]}"


# -- security: ext:: (and other exec transports) remote urls are refused -------
async def test_remote_url_rejects_exec_transport(led: PostgresLedger) -> None:
    repo = make_repo("url")
    g(repo.path, "remote", "set-url", "origin", "ext::sh -c whoami")
    assert C.remote_url(repo) is None, "ext:: transport must be rejected"
    g(repo.path, "remote", "set-url", "origin", "git://anon/x.git")
    assert C.remote_url(repo) is None, "plain git:// transport must be rejected"
    g(repo.path, "remote", "set-url", "origin", "https://github.com/x/y.git")
    assert C.remote_url(repo) == "https://github.com/x/y.git"


# -- the test seam fails closed if armed without TEST_MODE ---------------------
async def test_dispatch_override_requires_test_mode(led: PostgresLedger) -> None:
    seam = fresh_seam("guard"); repo = make_repo("guard")
    C.TEST_MODE = False                                  # override set, test-mode OFF
    ok = C.trigger_review(repo, "a" * 40, "b" * 40)
    assert ok is False and records(seam) == [], "must refuse the override outside test mode"
    C.TEST_MODE = True


# -- an empty-net-diff commit advances the cursor WITHOUT a review (workflow MAJOR) --
async def test_empty_diff_advances_without_review(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("empty"); repo = make_repo("empty")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance_empty(repo)                          # empty commit -> base..head2 has no net diff
    await C.process_repo(led, repo, [0])
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == head2 and cur["pending_sha"] is None, "empty diff must advance, not dispatch"
    assert records(seam) == [], "an empty-diff head must NOT be dispatched (would wedge to BLOCKED)"


# -- an over-budget multi-commit range dispatches the largest fitting PREFIX and
# -- walks the remainder across ticks (the 2026-07-02 backlog wedge) ------------
async def test_over_budget_range_chunks_and_walks(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("chunk"); repo = make_repo("chunk")
    C.MAX_REVIEW_LINES = 10
    base = head_of(repo)
    await C.process_repo(led, repo, [0])                 # seed baseline
    c1 = advance_lines(repo, "a.txt", 6)                 # prefix base..c1 =  6 lines (fits)
    c2 = advance_lines(repo, "b.txt", 6)                 # prefix base..c2 = 12 lines (over)
    c3 = advance_lines(repo, "c.txt", 6)                 # full range      = 18 lines (over)
    await C.process_repo(led, repo, [0])
    recs = records(seam)
    assert len(recs) == 1, f"expected one chunked dispatch, got {len(recs)}"
    assert recs[0]["base"] == base and recs[0]["head"] == c1, \
        "must dispatch the LARGEST prefix under budget (base..c1), not the full range"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == c1, "the ledger claim must be keyed on the chunk target"
    C._done_marker(repo, repo.watch_ref, c1).write_text("")              # chunk 1 delivered
    await C.process_repo(led, repo, [0])                 # advance to c1, dispatch next chunk
    recs = records(seam)
    assert len(recs) == 2 and recs[1]["base"] == c1 and recs[1]["head"] == c2
    C._done_marker(repo, repo.watch_ref, c2).write_text("")              # chunk 2 delivered
    await C.process_repo(led, repo, [0])                 # tail now fits -> full head
    recs = records(seam)
    assert len(recs) == 3 and recs[2]["base"] == c2 and recs[2]["head"] == c3
    C._done_marker(repo, repo.watch_ref, c3).write_text("")
    await C.process_repo(led, repo, [0])
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == c3, "the walk must converge on the true head"


# -- a single over-budget commit cannot be split: advance past WITHOUT review + LOUD flag --
async def test_oversized_single_commit_advances_and_flags(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("bigone"); repo = make_repo("bigone")
    C.MAX_REVIEW_LINES = 10
    await C.process_repo(led, repo, [0])                 # seed baseline
    big = advance_lines(repo, "big.txt", 25)             # one unsplittable 25-line commit
    before = {f.name for f in C.JEFE_INBOX.glob("*.md")} if C.JEFE_INBOX.exists() else set()
    await C.process_repo(led, repo, [0])
    assert records(seam) == [], "an unsplittable oversized commit must NOT dispatch"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == big and cur["pending_sha"] is None, \
        "the cursor must advance past the oversized commit (no wedge)"
    alerts = [f for f in C.JEFE_INBOX.glob("*.md")
              if f.name not in before and big in f.read_text()]
    assert alerts, "skipping an UNREVIEWED commit must flag the human inbox"
    assert "WITHOUT review" in alerts[0].read_text()
    small = advance(repo, "follow-up")                   # the loop stays alive afterwards
    await C.process_repo(led, repo, [0])
    recs = records(seam)
    assert len(recs) == 1 and recs[0]["base"] == big and recs[0]["head"] == small


# -- a TRANSIENT rev-list failure (nonzero returncode) on an over-budget range must
# -- DEFER, not advance-past-unreviewed (found by the autonomous review, 2026-07-03) ---
async def test_transient_revlist_failure_defers_not_advances(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("revlist"); repo = make_repo("revlist")
    C.MAX_REVIEW_LINES = 10
    await C.process_repo(led, repo, [0])                 # seed baseline
    big = advance_lines(repo, "big.txt", 25)             # over budget -> reaches rev-list
    real_git = C._git

    def fake_git(rp, *args, **kw):                       # force ONLY rev-list to fail transiently
        if args and args[0] == "rev-list":
            return subprocess.CompletedProcess(
                args=list(args), returncode=128, stdout="", stderr="fatal: transient")
        return real_git(rp, *args, **kw)

    before = {f.name for f in C.JEFE_INBOX.glob("*.md")} if C.JEFE_INBOX.exists() else set()
    C._git = fake_git
    try:
        await C.process_repo(led, repo, [0])
    finally:
        C._git = real_git
    assert records(seam) == [], "a transient rev-list failure must not dispatch"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] != big and cur["pending_sha"] is None, \
        "a transient rev-list failure must DEFER, not advance past the unreviewed range"
    assert not [f for f in C.JEFE_INBOX.glob("*.md")
                if f.name not in before and big in f.read_text()], \
        "a git hiccup must not be mislabelled 'too large, needs a human'"
    await C.process_repo(led, repo, [0])                 # git healed -> the real advance+flag path
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == big, "once rev-list heals, the oversized range advances+flags"


# -- a net-zero FIRST commit inside an over-budget range advances silently (no flag) --
async def test_net_zero_prefix_advances_silently(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("netz"); repo = make_repo("netz")
    C.MAX_REVIEW_LINES = 10
    await C.process_repo(led, repo, [0])                 # seed baseline
    c1 = advance_empty(repo)                             # net-zero vs base
    c2 = advance_lines(repo, "z.txt", 25)                # then an oversized single
    before = {f.name for f in C.JEFE_INBOX.glob("*.md")} if C.JEFE_INBOX.exists() else set()
    await C.process_repo(led, repo, [0])                 # tick 1: silent advance past c1
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == c1 and records(seam) == []
    assert not [f for f in C.JEFE_INBOX.glob("*.md")
                if f.name not in before and c1 in f.read_text()], \
        "a net-zero skip must NOT alarm the inbox"
    await C.process_repo(led, repo, [0])                 # tick 2: oversized c2 -> flag + advance
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == c2
    assert [f for f in C.JEFE_INBOX.glob("*.md")
            if f.name not in before and c2 in f.read_text()], "the oversized commit must flag"


# -- a binary-only FIRST commit is zero numstat lines but NOT empty: it must be
# -- DISPATCHED as a chunk, never silently skipped as net-zero (kilabz #1) ------
async def test_binary_only_prefix_dispatches_not_skipped(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("binpre"); repo = make_repo("binpre")
    C.MAX_REVIEW_LINES = 10
    base = head_of(repo)
    await C.process_repo(led, repo, [0])                 # seed baseline
    (repo.path / "blob.bin").write_bytes(bytes(range(256)) * 16)
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", "bin-only")
    g(repo.path, "push", "-q", "origin", "main")
    c1 = head_of(repo)                                   # reviewable, 0 numstat lines
    advance_lines(repo, "big.txt", 25)                   # pushes the range over budget
    await C.process_repo(led, repo, [0])
    recs = records(seam)
    assert len(recs) == 1 and recs[0]["base"] == base and recs[0]["head"] == c1, \
        "a binary-only prefix is reviewable and must dispatch, not silently advance"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] == c1


# -- persistent sizing failure defers (never dispatch/advance blind) but ALERTS at
# -- the streak threshold instead of wedging silently forever (kilabz R2 #2) ----
async def test_persistent_defer_alerts_once_then_heals(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("defer"); repo = make_repo("defer")
    C.MAX_REVIEW_LINES = 10
    await C.process_repo(led, repo, [0])                 # seed baseline
    big = advance_lines(repo, "big.txt", 25)             # over budget, unsplittable
    before = {f.name for f in C.JEFE_INBOX.glob("*.md")} if C.JEFE_INBOX.exists() else set()
    saved = C._diff_bytes
    C._diff_bytes = lambda *a, **k: None                 # sizing fails every tick
    try:
        for _ in range(C.DEFER_ALERT_STREAK + 1):
            await C.process_repo(led, repo, [0])
    finally:
        C._diff_bytes = saved
    assert records(seam) == [], "unsized ranges must never dispatch (guaranteed bounce)"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] != big, "unsized ranges must never advance blind"
    stalled = [f for f in C.JEFE_INBOX.glob("*.md")
               if f.name not in before and "stalled" in f.read_text()]
    assert len(stalled) == 1, "a persistent defer must alert exactly ONCE at the threshold"
    await C.process_repo(led, repo, [0])                 # sizing healed -> flag + advance
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == big, "after sizing heals the oversized skip proceeds"


# -- an UNSIZED range never dispatches, even when lines fit (kilabz PR#60 review):
# -- a byte-fat one-liner whose _diff_bytes times out is exactly what the byte cap
# -- catches — defer instead of dispatching blind ---------------------------------
async def test_unsized_small_range_defers_not_dispatches(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("unsized"); repo = make_repo("unsized")
    await C.process_repo(led, repo, [0])                 # seed baseline
    small = advance(repo, "tiny-change")                 # 2 changed lines — well under budget
    saved = C._diff_bytes
    C._diff_bytes = lambda *a, **k: None                 # byte sizing fails
    try:
        await C.process_repo(led, repo, [0])
    finally:
        C._diff_bytes = saved
    assert records(seam) == [], "an unsized range must DEFER, never dispatch blind"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["pending_sha"] is None and cur["reviewed_sha"] != small, \
        "defer must leave the cursor untouched (no claim, no blind advance)"
    await C.process_repo(led, repo, [0])                 # sizing healed -> normal dispatch
    recs = records(seam)
    assert len(recs) == 1 and recs[0]["head"] == small


# -- _diff_lines: binary files count 0 (reviewers see a one-line stub) ----------
async def test_diff_lines_binary_counts_zero(led: PostgresLedger) -> None:
    fresh_seam("bin"); repo = make_repo("bin")
    base = head_of(repo)
    (repo.path / "blob.bin").write_bytes(bytes(range(256)) * 16)
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", "bin")
    tip = head_of(repo)
    assert C._diff_lines(repo, base, tip) == 0, "binary numstat `-` must count 0, not crash"


# -- BOTH worker caps are slaved to the controller budgets (caps can't disagree) --
async def test_review_env_carries_both_caps(led: PostgresLedger) -> None:
    fresh_seam("envcap")
    C.MAX_REVIEW_LINES = 777
    C.MAX_REVIEW_BYTES = 55555
    env = C._review_env()
    assert env["PLAY_MAX_DIFF_LINES"] == "777", \
        "_review_env must pass the line budget as PLAY_MAX_DIFF_LINES"
    assert env["PLAY_MAX_DIFF"] == "55555", \
        "_review_env must pass the byte budget as PLAY_MAX_DIFF (workflow #1)"


# -- a byte-fat few-line commit (one-line minified blob) must not slip past the
# -- lines-only budget: it is over MAX_REVIEW_BYTES -> advance + flag (workflow #1) --
async def test_byte_fat_single_commit_advances_and_flags(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("bytefat"); repo = make_repo("bytefat")
    C.MAX_REVIEW_BYTES = 500                             # lines budget stays 1500
    await C.process_repo(led, repo, [0])                 # seed baseline
    (repo.path / "bundle.min.js").write_text("x" * 2000 + "\n")   # 1 numstat line, ~2KB patch
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", "fat")
    g(repo.path, "push", "-q", "origin", "main")
    fat = head_of(repo)
    before = {f.name for f in C.JEFE_INBOX.glob("*.md")} if C.JEFE_INBOX.exists() else set()
    await C.process_repo(led, repo, [0])
    assert records(seam) == [], "a byte-fat commit must NOT dispatch (worker would bounce it)"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == fat and cur["pending_sha"] is None
    assert [f for f in C.JEFE_INBOX.glob("*.md")
            if f.name not in before and fat in f.read_text()], \
        "the byte-oversized skip must flag the human inbox"


# -- a byte-fat commit in the MIDDLE of a backlog: chunk before it, flag+advance
# -- past it, then keep walking (the loop never wedges) ---------------------------
async def test_byte_fat_middle_commit_walks_past(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("bytemid"); repo = make_repo("bytemid")
    C.MAX_REVIEW_LINES = 10
    C.MAX_REVIEW_BYTES = 800
    base = head_of(repo)
    await C.process_repo(led, repo, [0])                 # seed baseline
    c1 = advance_lines(repo, "a.txt", 6)                 # small, fits both budgets
    (repo.path / "fat.js").write_text("y" * 2000 + "\n")
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", "fat")
    g(repo.path, "push", "-q", "origin", "main")
    c2 = head_of(repo)                                   # 1 line but over the byte budget
    c3 = advance_lines(repo, "b.txt", 6)
    await C.process_repo(led, repo, [0])                 # chunk 1: base..c1
    recs = records(seam)
    assert len(recs) == 1 and recs[0]["head"] == c1
    C._done_marker(repo, repo.watch_ref, c1).write_text("")
    before = {f.name for f in C.JEFE_INBOX.glob("*.md")} if C.JEFE_INBOX.exists() else set()
    await C.process_repo(led, repo, [0])                 # advance to c1; c2 is byte-fat -> flag+advance
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == c2, "the cursor must advance past the flagged byte-fat commit"
    assert [f for f in C.JEFE_INBOX.glob("*.md")
            if f.name not in before and c2 in f.read_text()], "the byte-fat skip must flag"
    await C.process_repo(led, repo, [0])                 # tail c3 fits -> dispatch
    recs = records(seam)
    assert len(recs) == 2 and recs[1]["base"] == c2 and recs[1]["head"] == c3


# -- backward force-push: no walkable prefix, and a fail-open dispatch would bounce
# -- off the worker caps the controller arms -> advance + flag (workflow #3) ------
async def test_backward_force_push_advances_and_flags(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("fpush"); repo = make_repo("fpush")
    c0 = head_of(repo)
    await C.process_repo(led, repo, [0])                 # seed baseline at c0
    c1 = advance_lines(repo, "w.txt", 25)
    await C.process_repo(led, repo, [0])                 # dispatch c0..c1 (fits default budget)
    C._done_marker(repo, repo.watch_ref, c1).write_text("")
    await C.process_repo(led, repo, [0])                 # advance: reviewed = c1
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == c1
    C.MAX_REVIEW_LINES = 10                              # now the reverse diff (25 lines) is over budget
    g(repo.path, "reset", "-q", "--hard", c0)
    g(repo.path, "push", "-q", "-f", "origin", "main")   # head is now an ANCESTOR of reviewed
    before = {f.name for f in C.JEFE_INBOX.glob("*.md")} if C.JEFE_INBOX.exists() else set()
    n_before = len(records(seam))
    await C.process_repo(led, repo, [0])
    assert len(records(seam)) == n_before, \
        "a no-prefix over-budget range must NOT dispatch (guaranteed worker bounce)"
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == c0, "the cursor must advance to the force-pushed head"
    assert [f for f in C.JEFE_INBOX.glob("*.md")
            if f.name not in before and c0 in f.read_text()], "the rewrite skip must flag"


# -- repos.json that is valid JSON but not an object fails soft (workflow MAJOR) --
async def test_load_config_rejects_non_object(led: PostgresLedger) -> None:
    cfgfile = _TMP / "repos-array.json"; cfgfile.write_text('["not", "an", "object"]')
    C.REPOS_JSON = cfgfile
    assert C.load_config() == [], "a non-object repos.json must yield [] (fail-soft), not crash"


# -- daily budget stops NEW dispatch but the (free) advance pass still runs (workflow MINOR) --
async def test_daily_budget_blocks_dispatch_not_advance(led: PostgresLedger) -> None:
    await _truncate(led)
    seam = fresh_seam("daycap"); repo = make_repo("daycap")
    await C.process_repo(led, repo, [0])                 # baseline
    head2 = advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # dispatch head2 (pending)
    assert len(records(seam)) == 1
    C._done_marker(repo, repo.watch_ref, head2).write_text("")           # head2 delivered
    C.MAX_DISPATCH_PER_DAY = 0                            # exhaust the daily dispatch budget
    head3 = advance(repo, "c2")                           # a new head arrives
    await C.process_repo(led, repo, [0])                 # advance MUST run; dispatch MUST NOT
    cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
    assert cur["reviewed_sha"] == head2, "advance pass must run even with the daily budget exhausted"
    assert len(records(seam)) == 1, "no NEW dispatch once the daily budget is exhausted"
    C.MAX_DISPATCH_PER_DAY = 20


# -- END-TO-END: real subprocess into a thin stub play-review -> done-marker -> advance --
async def test_end_to_end_stub_play_review(led: PostgresLedger) -> None:
    await _truncate(led)
    fresh_seam("e2e"); repo = make_repo("e2e")
    state = C.STATE
    record = _TMP / "e2e-stub-stdin.txt"
    stub = _TMP / "stub-play-review.sh"                  # trusted: regular file, owned by us, not group/world-writable
    stub.write_text(
        "#!/bin/bash\nread -r lr ls rr rs\n"
        f'printf "%s|%s|%s|%s" "$lr" "$ls" "$rr" "$rs" > "{record}"\n'
        # scoped done marker: done-<repo>-<ref>-<sha>, mirroring play-review.sh's marker_slug
        'slug="$(basename "$PWD")"; slug="${slug//[^A-Za-z0-9._-]/-}-${lr//[^A-Za-z0-9._-]/-}"\n'
        f'mkdir -p "{state}"; printf x > "{state}/done-$slug-$ls"\nexit 0\n')
    stub.chmod(0o755)
    C.PLAY_REVIEW = stub
    C.TEST_MODE = False; C.DISPATCH_OVERRIDE = ""         # exercise the REAL subprocess trigger path
    try:
        await C.process_repo(led, repo, [0])             # baseline
        base = head_of(repo); head2 = advance(repo, "c1")
        await C.process_repo(led, repo, [0])             # real dispatch -> stub writes done-<head2>
        assert C._done_marker(repo, repo.watch_ref, head2).exists(), \
            "controller must invoke play-review which writes the (scoped) marker"
        got = record.read_text().split("|")
        assert got == ["refs/heads/main", head2, "refs/heads/main", base], f"synthetic-stdin contract: {got}"
        await C.process_repo(led, repo, [0])             # advance pass consumes the marker
        cur = await led.get_cursor(repo.repo_id, repo.watch_ref)
        assert cur["reviewed_sha"] == head2 and cur["state"] == "delivered", "end-to-end advance failed"
    finally:
        C.TEST_MODE = True


# =====================================================================================
# +learning rung: the controller skill indexer + branch-protection provenance (Step 5/7).
# gh is stubbed (_make_gh) so the arm is exercised without a real GitHub; the jefe inbox is a
# temp dir. Skills are read from the trusted fetched owned ref by process_repo's fetch.
# =====================================================================================
async def _truncate_skills(led: PostgresLedger) -> None:
    async with led._pool.acquire() as con:
        await con.execute("TRUNCATE skill, skill_use")


def add_skill(repo: C.Repo, name: str, *, desc: str = "prefer fresh context for the write",
              trigger: str = "src/*.swift", body: str = "When reviewing this diff, prefer X.",
              branch: str = "main") -> str:
    """Commit + push a skills/<name>/SKILL.md to `branch` (simulates a merged skill PR)."""
    d = repo.path / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\npath_trigger: {trigger}\n---\n\n{body}\n")
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", f"add skill {name}")
    g(repo.path, "push", "-q", "origin", branch)
    return head_of(repo)


def _has_skill_alert(inbox: Path) -> bool:
    return inbox.is_dir() and any("skills-controller" in p.name for p in inbox.glob("*.md"))


async def test_skills_indexed_when_protected(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skidx"); repo = make_repo("skidx")
    C.JEFE_INBOX = _TMP / "jefe-skidx"
    C._gh_json = _make_gh(protected=True)
    add_skill(repo, "fresh-ctx", trigger="src/*.swift")
    await C.process_repo(led, repo, [0])                 # fetch owned ref -> index from it
    assert not (C.STATE / "skills-blocked-skidx").exists(), "protected main -> no block flag"
    assert (C.STATE / "skills-tree-skidx").exists(), "successful index writes the tree marker"
    sel = await led.select_skills("skidx", ["src/ContentView.swift"])
    assert len(sel["skills"]) == 1 and sel["skills"][0]["name"] == "fresh-ctx", f"indexed+selectable: {sel}"
    assert not _has_skill_alert(C.JEFE_INBOX), "a clean index does not alert"


async def test_skills_unauthored_stub_refused_at_index(led: PostgresLedger) -> None:
    # K4 (cross-family): a merged-but-UNAUTHORED auto-proposed stub PASSES lint but must NOT be
    # indexed — else it injects a no-op "flag any change…" skill into every future review. The
    # controller refuses it here (the load-bearing gate) even under protection + a clean merge.
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skstub"); repo = make_repo("skstub")
    C.JEFE_INBOX = _TMP / "jefe-skstub"
    C._gh_json = _make_gh(protected=True)
    add_skill(repo, "fail-open", trigger="src/*.py",
              body=f"(no description captured)\n\n{C.capture.STUB_MARKER}: author me before merge")
    await C.process_repo(led, repo, [0])
    assert (await led.select_skills("skstub", ["src/a.py"]))["skills"] == [], \
        "an un-authored stub (STUB_MARKER present) is refused at index — never enters the corpus"
    assert _has_skill_alert(C.JEFE_INBOX), "the stub refusal alerts jefe (like a lint reject)"


async def test_skills_blocked_when_unprotected(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skblock"); repo = make_repo("skblock")
    C.JEFE_INBOX = _TMP / "jefe-skblock"
    C._gh_json = _make_gh(protected=False)               # main has no/weak protection
    add_skill(repo, "foo")
    await C.process_repo(led, repo, [0])
    assert (C.STATE / "skills-blocked-skblock").exists(), "unprotected main -> fail-closed block flag"
    assert (await led.select_skills("skblock", ["src/a.swift"]))["skills"] == [], "nothing indexed when blocked"
    assert _has_skill_alert(C.JEFE_INBOX), "fail-closed block alerts jefe"
    # debounce: a SECOND consecutive blocked tick must NOT add another alert (no hourly spam)
    n1 = len(list(C.JEFE_INBOX.glob("*.md")))
    await C.process_repo(led, repo, [0])
    assert len(list(C.JEFE_INBOX.glob("*.md"))) == n1, "an already-blocked repo does not re-alert each tick"


async def test_skills_protection_downgrade_blocks_next_tick(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skdown"); repo = make_repo("skdown")
    C.JEFE_INBOX = _TMP / "jefe-skdown"
    C._gh_json = _make_gh(protected=True)
    add_skill(repo, "bar", trigger="src/*.swift")
    await C.process_repo(led, repo, [0])                 # indexed under protection
    assert (await led.select_skills("skdown", ["src/a.swift"]))["skills"], "indexed while protected"
    C._gh_json = _make_gh(protected=False)               # protection DROPS after indexing
    await C.process_repo(led, repo, [0])                 # re-verified every tick -> must block now
    assert (C.STATE / "skills-blocked-skdown").exists(), "a downgrade blocks the next tick (no grandfathering)"


async def test_skills_lint_reject_alerts_not_indexed(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("sklint"); repo = make_repo("sklint")
    C.JEFE_INBOX = _TMP / "jefe-sklint"
    C._gh_json = _make_gh(protected=True)
    add_skill(repo, "toobroad", trigger="*")             # banned trigger -> lint fail-closed
    await C.process_repo(led, repo, [0])
    assert (await led.select_skills("sklint", ["any.py"]))["skills"] == [], "lint-rejected skill is not indexed"
    assert _has_skill_alert(C.JEFE_INBOX), "a lint rejection alerts jefe"


async def test_skills_taint_blocks_relaunder_after_window(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("sktaint"); repo = make_repo("sktaint")
    C.JEFE_INBOX = _TMP / "jefe-sktaint"
    C._gh_json = _make_gh(protected=True)
    add_skill(repo, "ok-skill", trigger="src/*.swift")
    await C.process_repo(led, repo, [0])                  # indexed under protection
    assert (await led.select_skills("sktaint", ["src/a.swift"]))["skills"], "indexed while protected"
    C._gh_json = _make_gh(protected=False)               # protection DROPS
    add_skill(repo, "smuggled", trigger="src/*.swift")   # direct-push during the unprotected window
    await C.process_repo(led, repo, [0])                  # protection down -> blocked, nothing indexed
    assert (C.STATE / "skills-blocked-sktaint").exists(), "unprotected -> blocked"
    C._gh_json = _make_gh(protected=True)                # protection RESTORED
    await C.process_repo(led, repo, [0])                  # tree changed while blocked -> must STAY blocked
    assert (C.STATE / "skills-blocked-sktaint").exists(), "tainted: stays blocked after a window change (no launder)"
    assert (C.STATE / "skills-taint-sktaint").exists(), "taint marker written (debounces the alert)"
    assert _has_skill_alert(C.JEFE_INBOX), "taint alerts jefe for a manual audit"
    names = [s["name"] for s in (await led.select_skills("sktaint", ["src/a.swift"]))["skills"]]
    assert "smuggled" not in names, "the window-pushed skill was NOT promoted while tainted"


async def test_skills_protection_checks_watched_branch_not_main(led: PostgresLedger) -> None:
    # pins the watch-ref fix: a repo watching refs/heads/dev must verify DEV's protection (and
    # index from dev), NOT a hard-coded main. The gh mock here is protected for dev ONLY — the
    # old hard-coded-main code would query main, get None, and block (this test would fail).
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skwatch"); base = make_repo("skwatch")
    g(base.path, "checkout", "-q", "-b", "dev")
    g(base.path, "push", "-q", "-u", "origin", "dev")
    repo = C.Repo("skwatch", base.path, "refs/heads/dev")   # WATCH dev, not main
    add_skill(repo, "devskill", trigger="src/*.swift", branch="dev")
    C.JEFE_INBOX = _TMP / "jefe-skwatch"
    queried: list = []

    def _gh(repo_path, *args, timeout=None):
        if args[:2] == ("repo", "view"):
            return {"nameWithOwner": "o/r"}
        if args and args[0] == "api":
            queried.append(args[-1])
            return _GH_PROT if "/branches/dev/protection" in args[-1] else None  # main -> unprotected
        return None
    C._gh_json = _gh
    await C.process_repo(led, repo, [0])
    assert any("/branches/dev/protection" in q for q in queried), f"must query the WATCHED branch (dev): {queried}"
    assert not any("/branches/main/protection" in q for q in queried), "must NOT query a hard-coded main"
    assert not (C.STATE / "skills-blocked-skwatch").exists(), "dev is protected -> not blocked"
    assert (await led.select_skills("skwatch", ["src/x.swift"]))["skills"], "indexed from the watched branch"


async def test_skills_slashed_watch_ref_is_url_encoded(led: PostgresLedger) -> None:
    # a slashed branch (release/2026, allowed by _REF_RE) must be URL-encoded in the gh path, or
    # the raw slash splits .../branches/release/2026/protection into extra segments -> 404 ->
    # fail-closed indexing. The mock is protected ONLY for the ENCODED path (kilabz+oracle).
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skslash"); base = make_repo("skslash")
    g(base.path, "checkout", "-q", "-b", "release/2026")
    g(base.path, "push", "-q", "-u", "origin", "release/2026")
    repo = C.Repo("skslash", base.path, "refs/heads/release/2026")
    add_skill(repo, "relskill", trigger="src/*.swift", branch="release/2026")
    C.JEFE_INBOX = _TMP / "jefe-skslash"
    queried: list = []

    def _gh(rp, *args, timeout=None):
        if args[:2] == ("repo", "view"):
            return {"nameWithOwner": "o/r"}
        if args and args[0] == "api":
            queried.append(args[-1])
            return _GH_PROT if "branches/release%2F2026/protection" in args[-1] else None
        return None
    C._gh_json = _gh
    await C.process_repo(led, repo, [0])
    assert any("release%2F2026" in q for q in queried), f"slashed branch must be URL-encoded: {queried}"
    assert not (C.STATE / "skills-blocked-skslash").exists(), "encoded protection lookup succeeds -> not blocked"
    assert (await led.select_skills("skslash", ["src/x.swift"]))["skills"], "indexed from the slashed watched branch"


async def test_skills_delete_while_blocked_is_tainted(led: PostgresLedger) -> None:
    # deleting skills/ while blocked (tree -> 'none' from a real prior tree) archives all skills;
    # it must ALSO taint, not auto-clear-and-launder the deletion (kilabz #3).
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skdel"); repo = make_repo("skdel")
    C.JEFE_INBOX = _TMP / "jefe-skdel"
    C._gh_json = _make_gh(protected=True)
    add_skill(repo, "doomed", trigger="src/*.swift")
    await C.process_repo(led, repo, [0])                 # indexed under protection
    assert (await led.select_skills("skdel", ["src/a.swift"]))["skills"], "indexed while protected"
    C._gh_json = _make_gh(protected=False)               # protection DROPS
    g(repo.path, "rm", "-r", "-q", "skills")             # skills/ DELETED during the window
    g(repo.path, "commit", "-q", "-m", "rm skills"); g(repo.path, "push", "-q", "origin", "main")
    await C.process_repo(led, repo, [0])                 # blocked
    assert (C.STATE / "skills-blocked-skdel").exists(), "unprotected -> blocked"
    C._gh_json = _make_gh(protected=True)                # protection RESTORED
    await C.process_repo(led, repo, [0])                 # tree -> 'none' while blocked -> MUST taint
    assert (C.STATE / "skills-blocked-skdel").exists(), "delete-while-blocked stays blocked (not laundered)"
    assert (C.STATE / "skills-taint-skdel").exists(), "delete-while-blocked taints"
    async with led._pool.acquire() as con:
        st = await con.fetchval("SELECT state FROM skill WHERE repo_scope='skdel' AND name='doomed'")
    assert st == "active", "the existing skill was not silently archived by a laundered empty index"


async def test_skills_transient_block_auto_clears(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    fresh_seam("skblip"); repo = make_repo("skblip")
    C.JEFE_INBOX = _TMP / "jefe-skblip"
    C._gh_json = _make_gh(protected=True)
    add_skill(repo, "stable", trigger="src/*.swift")
    await C.process_repo(led, repo, [0])                  # indexed
    C._gh_json = _make_gh(protected=False)               # transient unreadable protection (a gh blip)
    await C.process_repo(led, repo, [0])                  # blocked, but skills/ UNCHANGED
    assert (C.STATE / "skills-blocked-skblip").exists(), "blip -> blocked"
    C._gh_json = _make_gh(protected=True)                # readable again, nothing changed
    await C.process_repo(led, repo, [0])
    assert not (C.STATE / "skills-blocked-skblip").exists(), "transient blip + unchanged skills/ auto-clears (no manual audit)"


async def test_gh_unavailable_blocks_skills_not_reviews(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    seam = fresh_seam("ghnone"); repo = make_repo("ghnone")
    C.JEFE_INBOX = _TMP / "jefe-ghnone"
    C._gh_json = lambda repo, *a, **k: None              # what the real _gh_json returns when gh is missing/hung
    await C.process_repo(led, repo, [0])                 # seed baseline
    advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # HEAD moved -> review MUST still dispatch
    assert len(records(seam)) == 1, "gh unavailable must NOT block the review dispatch (skills fail closed alone)"
    assert (C.STATE / "skills-blocked-ghnone").exists(), "gh unavailable -> fail-closed block flag for skills only"


async def test_indexer_exception_never_sinks_review(led: PostgresLedger) -> None:
    await _truncate(led); await _truncate_skills(led)
    seam = fresh_seam("idxboom"); repo = make_repo("idxboom")
    C.JEFE_INBOX = _TMP / "jefe-idxboom"

    def _boom(repo, *a, **k):
        raise RuntimeError("indexer blew up")
    C._gh_json = _boom                                   # any indexer exception, however deep
    await C.process_repo(led, repo, [0])                 # seed
    advance(repo, "c1")
    await C.process_repo(led, repo, [0])                 # indexer raises -> wrapper swallows -> review dispatches
    assert len(records(seam)) == 1, "an indexer exception must never sink the review path"


async def test_block_flag_write_failure_disarms_globally(led: PostgresLedger) -> None:
    # if the block flag CANNOT be persisted, absence of the flag would leave selection enabled for
    # a repo meant to be locked (oracle CRITICAL fail-open). Fail closed: remove SKILLS_ENABLED.
    fresh_seam("blkfail")
    C.JEFE_INBOX = _TMP / "jefe-blkfail"
    saved = C.SKILLS_ENABLED
    try:
        C.SKILLS_ENABLED = _TMP / "SKILLS_ENABLED-blkfail"; C.SKILLS_ENABLED.write_text("")
        # force the flag WRITE to fail: pre-create a DIRECTORY where the flag file would go
        bf = C._skill_block_flag("blkfail"); bf.mkdir(parents=True, exist_ok=True)
        C._block_repo_skills("blkfail", "protection unreadable")
        assert not C.SKILLS_ENABLED.exists(), "a block-flag write failure must disarm SKILLS_ENABLED (fail-closed)"
    finally:
        C.SKILLS_ENABLED = saved


async def test_tick_self_migrates_before_indexing(led: PostgresLedger) -> None:
    # the controller is a SEPARATE launchd job from serve; it must self-migrate so a deploy can't
    # tick it against a stale skill PK before serve reboots with 0006 (kilabz R3 Medium).
    await _truncate(led)
    async with led._pool.acquire() as con:               # stale the skill schema: single-column PK
        await con.execute("DROP TABLE IF EXISTS skill CASCADE")
        await con.execute(
            "CREATE TABLE skill (name text PRIMARY KEY, description text NOT NULL, body text "
            "NOT NULL, body_sha text NOT NULL, content_sha text NOT NULL, repo_scope text NOT "
            "NULL, path_trigger text NOT NULL, provenance text NOT NULL DEFAULT 'promoted', "
            "state text NOT NULL DEFAULT 'active', last_used_at timestamptz, "
            "created_at timestamptz NOT NULL DEFAULT now())")
    saved = (C.REPOS_JSON, C.LOCK, C.STATE, C.DRY_RUN, C.JEFE_INBOX, C._gh_json)
    try:
        repo = make_repo("ticmig")
        cfg = _TMP / "repos-ticmig.json"; cfg.write_text(json.dumps({"ticmig": {"path": str(repo.path)}}))
        C.REPOS_JSON = cfg
        C.LOCK = _TMP / "controller-ticmig.lock"
        C.STATE = _TMP / "state-ticmig"; C.STATE.mkdir(parents=True, exist_ok=True)
        C.JEFE_INBOX = _TMP / "jefe-ticmig"; C._gh_json = _make_gh(True); C.DRY_RUN = False
        await C.tick()                                   # self-migrate runs before the repo loop
        async with led._pool.acquire() as con:
            cols = await con.fetchval(
                "SELECT array_agg(a.attname ORDER BY array_position(c.conkey, a.attnum)) "
                "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(c.conkey) "
                "WHERE t.relname='skill' AND c.contype='p'")
        assert cols == ["repo_scope", "name"], f"tick must self-migrate the skill PK: {cols}"
    finally:
        C.REPOS_JSON, C.LOCK, C.STATE, C.DRY_RUN, C.JEFE_INBOX, C._gh_json = saved


async def test_pending_stale_floors_on_worker_lock_budget(led: PostgresLedger) -> None:
    # the controller must NOT reap a pending row while its worker's lock could still be live.
    # play-review's STALE_FLOOR grows with PLAY_REVIEW_CALL_TIMEOUT, so the old fixed 7200
    # under-floored once RCT was raised past ~2100 (RCT=2500 -> 8400) and a LIVE worker could be
    # re-dispatched. PENDING_STALE now derives the same floor from the same env (self-review #2).
    assert C._worker_lock_floor(1200) == 4500, "default RCT -> the pre-derivation 4500 floor"
    assert C._worker_lock_floor(2500) == 8400, "raised RCT grows the floor PAST the old fixed 7200"
    assert C.PENDING_STALE >= C._WORKER_LOCK_FLOOR, "the live constant never sits below the worker floor"
    assert C.PENDING_STALE >= 7200, "the 2h default is preserved at the RCT=1200 default"


async def test_git_survives_non_utf8_output(led: PostgresLedger) -> None:
    # a non-UTF-8 filename (core.quotePath=false) or blob makes git emit bytes strict UTF-8 can't
    # decode; _git must NOT raise UnicodeDecodeError — a ValueError the callers' (TimeoutExpired,
    # OSError) guard misses, which would kill the whole tick instead of honouring their "None on
    # ANY failure" contract (self-review #1). APFS can't hold an invalid-UTF-8 FILENAME, so we
    # exercise the shared _git decode path via a blob with raw invalid bytes; the fix covers both.
    repo = make_repo("utf8")
    sha = subprocess.run(["git", "-C", str(repo.path), "hash-object", "-w", "--stdin"],
                         input=b"\xff\xfe bad \x80 bytes", capture_output=True).stdout.decode().strip()
    r = C._git(repo.path, "cat-file", "-p", sha)          # pre-fix: raises UnicodeDecodeError here
    assert r.returncode == 0, "cat-file on a non-UTF-8 blob succeeds instead of crashing the tick"
    assert "�" in r.stdout, "the undecodable bytes become the replacement char, not an exception"


async def test_int_env_strict_digit_only(led: PostgresLedger) -> None:
    # _int_env mirrors play-review.sh's ^[0-9]+$: values Python's int() accepts but bash rejects
    # (negative / +sign / whitespace / underscore / non-numeric) must FALL BACK to the default, so
    # the two sides of a shared knob never diverge and a bad launchd value can't crash a service.
    key = "MYNDAIX_TEST_INT_ENV_PROBE"
    saved = os.environ.get(key)
    try:
        for bad in ["-1000", "+5", " 5 ", "5_0", "abc", "", "0x10", "1.5", "5\n"]:  # non-digit -> default
            os.environ[key] = bad
            assert C._int_env(key, 42) == 42, f"{bad!r} must fall back (bash rejects it too)"
        for good, val in [("0", 0), ("7", 7), ("08", 8), ("1200", 1200),
                          ("00000000003", 3), ("0" * 15 + "5", 5), ("0" * 11, 0)]:  # zero-padding
            os.environ[key] = good                       # must strip BEFORE the len cap (r5 regression)
            assert C._int_env(key, 42) == val, f"{good!r} -> {val}"
        for big in ["9999999999", "9" * 5000]:           # digit-only but astronomical (2nd trips int()'s
            os.environ[key] = big                        # 4300-digit limit) -> capped via the len>10 path
            assert C._int_env(key, 42) == 2**31 - 1, f"{big[:12]!r} caps (crash-proof), not default"
        os.environ.pop(key, None)
        assert C._int_env(key, 42) == 42, "unset -> default"
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


async def test_chunker_sizes_each_commit_once(led: PostgresLedger) -> None:
    # #6: the walk loop + single-commit fallback must not RE-size the same base..commit. A byte-fat
    # single commit over budget is sized once at the top; the walk skips head; the fallback reuses
    # the memoized size — so _diff_lines/_diff_bytes(base, head) each run exactly ONCE (was 2x).
    repo = make_repo("memo")
    base = C._git(repo.path, "rev-parse", "HEAD").stdout.strip()
    # commit a byte-fat file with a SHORT message — passing the big content as `git commit -m` (what
    # advance() does) blows Linux's 128KB per-arg limit (E2BIG on CI; macOS ARG_MAX is higher).
    (repo.path / "f.txt").write_text("x" * (C.MAX_REVIEW_BYTES + 5000) + "\n")
    g(repo.path, "add", "-A"); g(repo.path, "commit", "-q", "-m", "byte-fat")
    head = g(repo.path, "rev-parse", "HEAD").stdout.strip()
    calls = {"l": 0, "b": 0}
    real_l, real_b = C._diff_lines, C._diff_bytes
    C._diff_lines = lambda r, b, h: (calls.__setitem__("l", calls["l"] + 1), real_l(r, b, h))[1]
    C._diff_bytes = lambda r, b, h: (calls.__setitem__("b", calls["b"] + 1), real_b(r, b, h))[1]
    try:
        mode, sha, n, b = C._choose_review_target(repo, base, head)
    finally:
        C._diff_lines, C._diff_bytes = real_l, real_b
    assert mode == "advance" and sha == head, f"byte-fat single commit is advanced+flagged, got {mode}/{sha[:8]}"
    assert calls["l"] == 1, f"_diff_lines(base,head) sized ONCE, not re-run in the fallback (got {calls['l']})"
    assert calls["b"] == 1, f"_diff_bytes(base,head) sized ONCE, not re-run in the fallback (got {calls['b']})"


async def main() -> None:
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and inspect.iscoroutinefunction(v)]
    passed = 0
    try:
        for t in tests:
            await t(led)
            print("PASS", t.__name__)
            passed += 1
    finally:
        await led.close()
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(main())
