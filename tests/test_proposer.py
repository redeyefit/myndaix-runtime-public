"""S7 proposer driver — the safety properties earned by the cross-family review, tested WITHOUT a
real Postgres/gh/git (a FakeLedger records mutations; the gh/git helpers are monkeypatched). The
pure render/slug/stub-marker core is in test_capture.py; the ledger verbs in test_capture_verbs.py.

Run:  PYTHONPATH=src python3 tests/test_proposer.py
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import runtime.proposer as P

# Capture the REAL functions BEFORE any test monkeypatches the module globals (bare harness has no
# fixture teardown; tests stub P.resolve_repo/P._git and don't restore them).
_REAL_RESOLVE_REPO = P.resolve_repo
_REAL_GIT = P._git
_REAL_FIND_PR = P._find_open_bot_pr
_REAL_PUSH_PR = P._push_and_open_pr

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


def _res(rc=0, out="", err=""):
    return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()


class FakeLedger:
    """Records every mutating call so a test can assert DRY_RUN mutates NOTHING (A9) and the happy
    path mutates exactly right. Read methods return seeded data."""
    def __init__(self, ready=None, proposed=None, open_count=0, provenance=None, inflight=None):
        self._ready = ready or []
        self._proposed = proposed or []
        self._open = open_count
        self._prov = provenance or []
        self._inflight = inflight    # None -> derive from proposed (like the real ledger's superset)
        self.mutations = []          # (verb, *args) for every state-changing call
        self.claim_ok = True
        self.mark_ok = True

    async def list_inflight_fingerprints(self):
        if self._inflight is not None:
            return set(self._inflight)
        return {p["fingerprint"] for p in self._proposed}

    # -- reads --
    async def list_ready_candidates(self, limit, after=""):
        rows = sorted(self._ready, key=lambda r: r["fingerprint"])
        return [r for r in rows if r["fingerprint"] > after][:limit]
    async def list_proposed(self): return list(self._proposed)
    async def count_open_proposals(self): return self._open
    async def capture_provenance(self, fp, limit=8): return list(self._prov)

    # -- mutations (recorded) --
    async def claim_for_proposing(self, fp, branch, draft_sha):
        self.mutations.append(("claim", fp, branch)); return self.claim_ok
    async def mark_capture_proposed(self, fp, branch, draft_sha, pr):
        self.mutations.append(("mark", fp, pr)); return self.mark_ok
    async def release_proposing(self, fp, branch, draft_sha):
        self.mutations.append(("release", fp)); return True
    async def resolve_capture(self, fp, outcome):
        self.mutations.append(("resolve", fp, outcome)); return True
    async def reap_stuck_proposing(self, mins):
        self.mutations.append(("reap", mins)); return 0
    async def close(self): pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset(monkey_dry=False):
    P.DRY_RUN = monkey_dry
    # per-test isolation: cursor + suspects are process state; point them at fresh temp files
    d = Path(tempfile.mkdtemp(prefix="mdx-test-state."))
    P.CURSOR_FILE = d / "cursor"
    P.SUSPECTS_FILE = d / "suspects.json"


# ---- K3: symlink-safe, creation-only SKILL.md write --------------------------------------
def test_safe_write_clean():
    with tempfile.TemporaryDirectory() as wt:
        ok(P._safe_write_skill(Path(wt), "fail-open", "hello"), "clean write into a fresh worktree")
        f = Path(wt) / "skills" / "fail-open" / "SKILL.md"
        ok(f.read_text() == "hello", "content written")


def test_safe_write_refuses_existing_target():
    with tempfile.TemporaryDirectory() as wt:
        d = Path(wt) / "skills" / "fail-open"; d.mkdir(parents=True)
        (d / "SKILL.md").write_text("prior")
        ok(not P._safe_write_skill(Path(wt), "fail-open", "new"),
           "existing target refused (creation-only; never edits an existing skill)")
        ok((d / "SKILL.md").read_text() == "prior", "existing content untouched")


def test_safe_write_refuses_symlinked_skills_dir():
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as evil:
        os.symlink(evil, str(Path(wt) / "skills"))          # skills/ is a symlink -> escape vector
        ok(not P._safe_write_skill(Path(wt), "fail-open", "x"),
           "symlinked skills/ ancestor refused (K3: no write escape)")
        ok(not (Path(evil) / "fail-open" / "SKILL.md").exists(), "nothing written through the symlink")


def test_safe_write_refuses_symlinked_slugdir():
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as evil:
        (Path(wt) / "skills").mkdir()
        os.symlink(evil, str(Path(wt) / "skills" / "fail-open"))
        ok(not P._safe_write_skill(Path(wt), "fail-open", "x"), "symlinked slug dir refused (K3)")


# ---- K6: gh pr create prints a URL, not JSON — parse the number ------------------------------
def test_pr_url_parse():
    m = P._PR_URL_RE.search("https://github.com/redeyefit/myndaix-runtime/pull/142\n")
    ok(m is not None and m.group(1) == "142", "PR number parsed from the create URL")
    ok(P._PR_URL_RE.search("no url here") is None, "no false match on non-URL output")


# ---- A2: resolve_repo is fail-closed against the exact-key allowlist MAP ---------------------
def test_resolve_repo_fail_closed(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        rj = Path(d) / "repos.json"
        rj.write_text('{"_comment":"x","myndaix-runtime":{"path":"' + d + '"}}')
        P.REPOS_JSON = rj
        # even a listed key needs a resolvable git repo + nwo; stub gh to a known nwo, and make the
        # path look like a git repo
        (Path(d) / ".git").mkdir()
        P._gh_json_path = lambda path, *a: {"nameWithOwner": "redeyefit/myndaix-runtime",
                                            "defaultBranchRef": {"name": "main"}}
        ok(_REAL_RESOLVE_REPO("myndaix-runtime") is not None, "listed repo with a real path resolves")
        ok(_REAL_RESOLVE_REPO("feat-sync-phase1") is None, "UNLISTED scope (branch name) -> None (A2 fail-closed)")
        ok(_REAL_RESOLVE_REPO("wf_80d11feb-cb4-3") is None, "workflow-id scope -> None")
        ok(_REAL_RESOLVE_REPO("_comment") is None, "an underscore/meta key -> None")


# ---- K2/K1: tri-state recovery lookup (found / none / unknown / ambiguous) -------------------
def test_find_open_bot_pr_identity(monkeypatch=None):
    repo = {"nwo": "redeyefit/myndaix-runtime"}
    P._gh_json = lambda nwo, *a: [{"number": 5, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"}]
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open") == ("found", 5), "single same-repo bot PR -> found")
    P._gh_json = lambda nwo, *a: [{"number": 6, "isCrossRepository": True, "headRefName": "skill/auto/fail-open"}]
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open") == ("none", None),
       "a FORK PR (cross-repo) is not ours -> definitive absence (K2)")
    P._gh_json = lambda nwo, *a: [{"number": 7, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"},
                                  {"number": 8, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"}]
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open")[0] == "ambiguous",
       "multi-match -> ambiguous (never first-match)")
    P._gh_json = lambda nwo, *a: None
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open")[0] == "unknown",
       "gh failure -> unknown (NOT permission to create — kilabz #3)")


def test_unknown_lookup_never_creates():
    # kilabz MAJOR: after a crash-before-mark, a transient lookup failure must NOT lead to a push
    # over a (possibly human-edited) PR branch. Under the lookup-before-claim order there is now
    # NOTHING to release — an unknown lookup defers with zero DB mutations and zero creation.
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("unknown", None)
    created = {"n": 0}
    def _create(*a, **k):
        created["n"] += 1; return Path("/tmp/wt")
    P._make_proposal_commit = _create
    _run(P.propose(led))
    ok(created["n"] == 0, "unknown lookup -> NO worktree/commit/push (defer)")
    ok(led.mutations == [], "unknown lookup takes no claim at all (lookup precedes claim)")


def test_adoption_bypasses_max_open():
    # inbox-synthesis P1: an untracked open PR (unknown create outcome) must be adoptable even
    # when tracked PRs are at MAX_OPEN — adoption RE-TRACKS an existing PR, it adds no load. The
    # old loop broke at capacity before the adopt path could run; the orphan was stranded forever.
    _reset(False)
    led = FakeLedger(open_count=99,                        # way over MAX_OPEN: creation is gated
                     ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("found", 88)
    created = {"n": 0}
    def _create(*a, **k):
        created["n"] += 1; return Path("/tmp/wt")
    P._make_proposal_commit = _create
    _run(P.propose(led))
    ok(("mark", "fp1", 88) in led.mutations, "the orphaned PR is adopted DESPITE MAX_OPEN")
    ok(created["n"] == 0, "no creation at capacity (only adoption bypasses the gate)")


def test_probe_cap_bounds_lookups_at_capacity():
    # K6: the adopt-probe at capacity is bounded — with many ready candidates and creation gated,
    # at most PROBE_CAP gh lookups run per tick (the fairness cursor rotates who gets probed).
    _reset(False)
    ready = [{"fingerprint": f"fp{i:02d}", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
              "path_glob": "src/*.py", "decline_count": 0} for i in range(20)]
    led = FakeLedger(open_count=99, ready=ready)
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    looked = {"n": 0}
    def _lookup(repo, branch):
        looked["n"] += 1; return ("none", None)
    P._find_open_bot_pr = _lookup
    _run(P.propose(led))
    ok(looked["n"] == P.PROBE_CAP, f"exactly PROBE_CAP lookups at capacity (got {looked['n']})")
    ok(led.mutations == [], "no claims/creates at capacity when nothing is adoptable")


def test_existing_remote_branch_refuses_create():
    # inbox-synthesis P1 (force-push erasure): a remote branch with NO open bot PR (human content
    # on a closed-PR branch, or a crashed prior push) must REFUSE creation — never push over it.
    # An unknown existence lookup refuses too (fail-closed).
    _reset(False)
    saved = P._remote_branch_exists
    try:
        for exists in (True, None):
            led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                                     "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
            P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
            P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            P._find_open_bot_pr = lambda repo, branch: ("none", None)
            P._remote_branch_exists = lambda repo, branch, _e=exists: _e
            created = {"n": 0}
            def _create(*a, **k):
                created["n"] += 1; return Path("/tmp/wt")
            P._make_proposal_commit = _create
            _run(P.propose(led))
            ok(created["n"] == 0 and led.mutations == [],
               f"remote-branch exists={exists} + no open PR -> refuse create, no claim")
    finally:
        P._remote_branch_exists = saved


def test_probe_exhaustion_does_not_starve_below_capacity():
    # kilabz ff MAJOR: with creation budget left but probes spent, the old `continue` put the
    # unprocessed candidate in `seen` and the cursor moved PAST it — an adoptable orphan beyond
    # the probe budget was never adopted, permanently. Now the scan BREAKS before consuming it;
    # the next tick resumes exactly there. CAPTURE_MAX_OPEN=10 >= PROBE_CAP=6 also triggers the
    # attack-pass FLOOR: the effective probe budget becomes MAX_OPEN+1 = 11 (a suspect head must
    # never be able to consume every probe and starve adoption — the accounting's self-heal
    # path). 12 adoptable candidates: tick 1 probes exactly 11 and does NOT consume the 12th.
    _reset(False)
    prior_max_open = os.environ.get("CAPTURE_MAX_OPEN")          # restore, don't just pop (ff-r3 MINOR)
    os.environ["CAPTURE_MAX_OPEN"] = "10"
    try:
        ready = [{"fingerprint": f"fp{i:02d}", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
                  "path_glob": "src/*.py", "decline_count": 0} for i in range(12)]
        P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
        P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        P._find_open_bot_pr = lambda repo, branch: ("found", 70)
        led = FakeLedger(ready=ready)
        _run(P.propose(led))
        t1 = [m for m in led.mutations if m[0] == "mark"]
        ok(len(t1) == 11, f"tick 1 adopts exactly the FLOORED budget (MAX_OPEN+1 = 11) (got {len(t1)})")
        claimed1 = {m[1] for m in led.mutations if m[0] == "claim"}
        ok("fp11" not in claimed1, "the 12th candidate is NOT consumed in tick 1")
        led2 = FakeLedger(ready=ready)
        _run(P.propose(led2))
        ok(("mark", "fp11", 70) in led2.mutations,
           "tick 2 resumes AT the unprocessed candidate and ADOPTS it (no starvation)")
    finally:
        if prior_max_open is None:
            os.environ.pop("CAPTURE_MAX_OPEN", None)
        else:
            os.environ["CAPTURE_MAX_OPEN"] = prior_max_open


def test_unknown_create_persists_suspect_and_recovers_next_tick():
    # ff-r3 M2b: the suspect is PERSISTED independently of the fairness cursor; the next tick
    # resolves it (adopt/clear/reserve) BEFORE any creation.
    _reset(False)
    ready = [{"fingerprint": f"fp{i:02d}", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
              "path_glob": "src/*.py", "decline_count": 0} for i in range(7)]
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    creates = {"n": 0}
    def _unknown(repo, wt, branch, slug):
        creates["n"] += 1; return ("unknown", None)
    P._push_and_open_pr = _unknown
    try:
        led = FakeLedger(open_count=2, ready=ready)              # 2 tracked, cap 3
        _run(P.propose(led))
        ok(creates["n"] == 1, "tick 1: ONE unknown create (the in-tick reservation gates the rest)")
        suspects = json.loads(P.SUSPECTS_FILE.read_text())
        ok(len(suspects) == 1 and suspects[0]["fingerprint"] == "fp00",
           "the suspect is PERSISTED (fingerprint+scope+branch)")
        # tick 2: the orphan turned out REAL — suspect resolution adopts it BEFORE any creation
        P._find_open_bot_pr = lambda repo, branch: ("found", 91)
        led2 = FakeLedger(open_count=2, ready=ready)
        _run(P.propose(led2))
        ok(("mark", "fp00", 91) in led2.mutations, "tick 2 ADOPTS the orphan via the suspect file")
        ok(json.loads(P.SUSPECTS_FILE.read_text()) == [], "the resolved suspect is cleared")
        ok(creates["n"] == 1, "tick 2 creates nothing more (3 tracked after the adopt)")
    finally:
        P._remote_branch_exists = saved_rbe


def test_suspect_beats_newly_ready_candidate():
    # kilabz ff-r3 M2b gap-2 repro: cursor fp00, suspect fp20, a NEWLY-READY fp10 sorts between
    # them. The suspect must be recovered BEFORE fp10 can create — else 4 real PRs under cap 3.
    _reset(False)
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 55)
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text(json.dumps([{"fingerprint": "fp20", "repo_scope": "myndaix-runtime",
                                            "branch": "skill/auto/fail-open"}]))
    P.CURSOR_FILE.write_text("fp00")
    # found ONLY for the suspect's branch; fp10's branch (toctou-race) gets a definitive none so
    # fp10 WOULD create if capacity allowed (kilabz ff-r4: an all-found stub made the assertion
    # vacuous — fp10 could never attempt creation regardless of capacity)
    P._find_open_bot_pr = lambda repo, branch: (("found", 77) if branch.endswith("fail-open")
                                                else ("none", None))
    creates = {"n": 0}
    def _create_counting(repo, wt, branch, slug):
        creates["n"] += 1; return ("opened", 55)
    P._push_and_open_pr = _create_counting
    led = FakeLedger(open_count=2,                               # 2 tracked + 1 orphan = cap FULL
                     ready=[{"fingerprint": "fp10", "repo_scope": "myndaix-runtime",
                             "rule_tag": "toctou-race", "path_glob": "src/*.py", "decline_count": 0}])
    try:
        _run(P.propose(led))
        ok(("mark", "fp20", 77) in led.mutations, "the suspect is adopted FIRST (pre-scan)")
        ok(creates["n"] == 0,
           "fp10 attempts NO create — capacity is full once the orphan is re-tracked (no overflow)")
    finally:
        P._remote_branch_exists = saved_rbe


def test_corrupt_suspects_file_disables_creation_and_is_preserved():
    # ff-r4 P1: damaged JSON may hold live reservations — never overwrite it, never create while
    # it is unreadable. Adoption stays allowed.
    _reset(False)
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text("{not-json[")
    ready = [{"fingerprint": "fp00", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
              "path_glob": "src/*.py", "decline_count": 0},
             {"fingerprint": "fp01", "repo_scope": "myndaix-runtime", "rule_tag": "toctou-race",
              "path_glob": "src/*.py", "decline_count": 0}]
    led = FakeLedger(ready=ready)
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    def _lookup(repo, branch):
        return ("found", 62) if branch.endswith("fail-open") else ("none", None)
    P._find_open_bot_pr = _lookup
    creates = {"n": 0}
    def _create(repo, wt, branch, slug):
        creates["n"] += 1; return ("opened", 63)
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = _create
    try:
        _run(P.propose(led))
        ok(creates["n"] == 0, "creation DISABLED while the suspects file is damaged")
        ok(("mark", "fp00", 62) in led.mutations, "adoption still allowed under a damaged file")
        ok(P.SUSPECTS_FILE.read_text() == "{not-json[", "the damaged file is preserved, not overwritten")
    finally:
        P._remote_branch_exists = saved_rbe


def test_already_tracked_suspect_cleared_without_double_reserve():
    # ff-r4 P2 + ff-r5 discrimination fix: a suspect whose class is ALREADY tracked
    # (state='proposed' — e.g. a mark committed but its response raised) is cleared even when its
    # PR lookup returns FOUND and the claim would be refused (row not 'ready'). Capacity is
    # arranged so DOUBLE-counting would block the next candidate's create: open_count=2 (fpT +
    # one other) under cap 3 — the buggy keep+reserve makes n_open 3 and blocks fpZ; correct
    # clearing leaves room and fpZ creates.
    _reset(False)
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text(json.dumps([{"fingerprint": "fpT", "repo_scope": "myndaix-runtime",
                                            "branch": "skill/auto/fail-open"}]))
    led = FakeLedger(open_count=2,
                     proposed=[{"fingerprint": "fpT", "repo_scope": "myndaix-runtime",
                                "rule_tag": "fail-open", "pr_number": 44,
                                "branch": "skill/auto/fail-open", "proposed_at": None}],
                     ready=[{"fingerprint": "fpZ", "repo_scope": "myndaix-runtime",
                             "rule_tag": "toctou-race", "path_glob": "src/*.py", "decline_count": 0}])
    async def _claim(fp, branch, draft_sha):
        led.mutations.append(("claim", fp, branch))
        return fp != "fpT"                                       # the tracked row refuses a claim
    led.claim_for_proposing = _claim
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: (("found", 44) if branch.endswith("fail-open")
                                                else ("none", None))
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 45)
    try:
        _run(P.propose(led))
        ok(json.loads(P.SUSPECTS_FILE.read_text()) == [], "the already-tracked suspect is cleared")
        ok(("mark", "fpZ", 45) in led.mutations,
           "no double reservation: fpZ still creates (2 tracked incl. fpT + 1 new = cap 3)")
    finally:
        P._remote_branch_exists = saved_rbe


def test_mark_raise_retains_reservation():
    # ff-r5 P1: a RAISING mark (or failed fence-close) is an unresolved exit — the reservation
    # taken at intent-persist must survive so further creates stay gated. 2 tracked / cap 3: the
    # first create's mark raises; the second candidate must NOT create (reservation held).
    _reset(False)
    ready = [{"fingerprint": f"fp{i}", "repo_scope": "myndaix-runtime", "rule_tag": t,
              "path_glob": "src/*.py", "decline_count": 0}
             for i, t in enumerate(["fail-open", "toctou-race"])]
    led = FakeLedger(open_count=2, ready=ready)
    async def _raising_mark(fp, branch, draft_sha, pr):
        raise RuntimeError("mark response lost")
    led.mark_capture_proposed = _raising_mark
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    creates = {"n": 0}
    def _create(repo, wt, branch, slug):
        creates["n"] += 1; return ("opened", 46)
    P._push_and_open_pr = _create
    try:
        _run(P.propose(led))
        ok(creates["n"] == 1, "a raising mark retains the reservation — no second create")
        ok(json.loads(P.SUSPECTS_FILE.read_text())[0]["fingerprint"] == "fp0",
           "the intent survives the raising mark (next tick re-tracks the real PR)")
    finally:
        P._remote_branch_exists = saved_rbe


def test_proposing_claim_suspect_kept_but_not_double_reserved():
    # ff-r5 P2: a surviving 'proposing' claim (failed release) is ALREADY in count_open_proposals;
    # its retained suspect must not reserve a second slot. open_count=2 under cap 3 (kilabz r6 P3:
    # at open_count=1 the assertion passed even with double-counting restored — 2 is the boundary
    # where an erroneous extra reservation hits the cap and blocks fpZ; verified discriminating).
    _reset(False)
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text(json.dumps([{"fingerprint": "fpP", "repo_scope": "myndaix-runtime",
                                            "branch": "skill/auto/fail-open"}]))
    led = FakeLedger(open_count=2, inflight={"fpP"},
                     ready=[{"fingerprint": "fpZ", "repo_scope": "myndaix-runtime",
                             "rule_tag": "toctou-race", "path_glob": "src/*.py", "decline_count": 0}])
    async def _claim(fp, branch, draft_sha):
        led.mutations.append(("claim", fp, branch))
        return fp != "fpP"                                       # the proposing row refuses a claim
    led.claim_for_proposing = _claim
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: (("found", 47) if branch.endswith("fail-open")
                                                else ("none", None))
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 48)
    try:
        _run(P.propose(led))
        suspects = json.loads(P.SUSPECTS_FILE.read_text())
        ok(len(suspects) == 1 and suspects[0]["fingerprint"] == "fpP",
           "the suspect for a live 'proposing' claim is KEPT (may still resolve either way)")
        ok(("mark", "fpZ", 48) in led.mutations,
           "but NOT double-reserved: fpZ still creates (the count already holds fpP's slot)")
    finally:
        P._remote_branch_exists = saved_rbe


def test_release_exception_cannot_bypass_reservation():
    # ff-r3 M2b gap-1: a raising release after an unknown create must not bypass the reservation —
    # the suspect is persisted BEFORE the release attempt.
    _reset(False)
    ready = [{"fingerprint": f"fp{i}", "repo_scope": "myndaix-runtime", "rule_tag": t,
              "path_glob": "src/*.py", "decline_count": 0}
             for i, t in enumerate(["fail-open", "toctou-race"])]
    led = FakeLedger(open_count=2, ready=ready)                  # 2 tracked, cap 3
    async def _raising_release(fp, branch, draft_sha):
        raise RuntimeError("db hiccup mid-release")
    led.release_proposing = _raising_release
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    creates = {"n": 0}
    def _unknown(repo, wt, branch, slug):
        creates["n"] += 1; return ("unknown", None)
    P._push_and_open_pr = _unknown
    try:
        _run(P.propose(led))
        ok(creates["n"] == 1, "the raising release does not enable a second create")
        ok(json.loads(P.SUSPECTS_FILE.read_text())[0]["fingerprint"] == "fp0",
           "the suspect was persisted BEFORE the release raised (reservation survives)")
    finally:
        P._remote_branch_exists = saved_rbe


def test_unknown_lookup_defers_without_wedging_queue():
    # ff-r3: an unknown LOOKUP must not stop the pass (one repo's gh trouble would block adoption
    # in healthy repos). It defers that candidate only; the next candidate still proceeds.
    _reset(False)
    ready = [{"fingerprint": "fp00", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
              "path_glob": "src/*.py", "decline_count": 0},
             {"fingerprint": "fp01", "repo_scope": "myndaix-runtime", "rule_tag": "toctou-race",
              "path_glob": "src/*.py", "decline_count": 0}]
    led = FakeLedger(ready=ready)
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    def _lookup(repo, branch):
        return ("unknown", None) if branch.endswith("fail-open") else ("found", 61)
    P._find_open_bot_pr = _lookup
    _run(P.propose(led))
    ok(not any(m[0] == "claim" and m[1] == "fp00" for m in led.mutations),
       "the unknown-lookup candidate takes no claim (deferred)")
    ok(("mark", "fp01", 61) in led.mutations, "the NEXT candidate still adopts (queue not wedged)")


def test_unknown_create_reserves_max_open_slot():
    # kilabz ff MAJOR: an unknown create outcome may be a REAL open PR — it must consume a
    # MAX_OPEN slot this tick so further creates can't push real open PRs past the cap.
    _reset(False)
    ready = [{"fingerprint": f"fp{i}", "repo_scope": "myndaix-runtime", "rule_tag": t,
              "path_glob": "src/*.py", "decline_count": 0}
             for i, t in enumerate(["fail-open", "toctou-race"])]
    led = FakeLedger(open_count=2, ready=ready, provenance=["deadbeef"])   # 2 tracked, cap 3
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    calls = {"n": 0}
    def _unknown(repo, wt, branch, slug):
        calls["n"] += 1; return ("unknown", None)
    P._push_and_open_pr = _unknown
    try:
        _run(P.propose(led))
        ok(calls["n"] == 1, f"the unknown outcome RESERVES the last slot — no 2nd create (got {calls['n']})")
    finally:
        P._remote_branch_exists = saved_rbe


def test_dry_run_bounded_by_creation_budget():
    # kilabz ff MINOR: dry-run must model the creation limits, not report the whole 100-row batch.
    _reset(monkey_dry=True)
    ready = [{"fingerprint": f"fp{i:02d}", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
              "path_glob": "src/*.py", "decline_count": 0} for i in range(10)]
    led = FakeLedger(ready=ready, provenance=["deadbeef"])
    visited = {"n": 0}
    async def _prov(fp, limit=8):
        visited["n"] += 1; return ["deadbeef"]
    led.capture_provenance = _prov
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    _run(P.propose(led))
    ok(visited["n"] == P.MAX_PER_TICK,
       f"dry-run stops at the creation budget ({visited['n']} == MAX_PER_TICK)")
    ok(led.mutations == [], "still zero mutations (A9)")
    _reset(False)


def test_push_has_no_force():
    # inbox-synthesis P1: the push must be a PLAIN push (a racing branch creation gets a
    # non-fast-forward rejection, never an overwrite).
    _reset(False)
    git_calls = []
    def _rec_git(cwd, *a, **k):
        git_calls.append(a); return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    saved_git, saved_run = P._git, P.subprocess.run
    P._git = _rec_git
    P.subprocess.run = lambda *a, **k: type("R", (), {
        "returncode": 0, "stdout": "https://github.com/o/r/pull/9\n", "stderr": ""})()
    try:
        n = _REAL_PUSH_PR({"nwo": "o/r", "default_branch": "main", "path": Path("/tmp/x")},
                                Path("/tmp/wt"), "skill/auto/x", "x")
        ok(n == ("opened", 9), "tri-state opened + PR number parsed from create URL")
        push = next((c for c in git_calls if c and c[0] == "push"), None)
        ok(push is not None and "--force" not in push, f"push argv has NO --force (got {push})")
    finally:
        P._git, P.subprocess.run = saved_git, saved_run


def test_attempt_budget_burns_on_unknown_outcome():
    # kilabz MAJOR: an unknown create outcome may have opened a PR whose response was lost — the
    # per-tick budget must burn on the ATTEMPT, not only on a confirmed mark (else 3 ready
    # candidates x unknown outcomes = 3+ create attempts against a 2-per-tick budget).
    _reset(False)
    ready = [{"fingerprint": f"fp{i}", "repo_scope": "myndaix-runtime", "rule_tag": t,
              "path_glob": "src/*.py", "decline_count": 0}
             for i, t in enumerate(["fail-open", "toctou-race", "missing-scoping"])]
    led = FakeLedger(ready=ready, provenance=["deadbeef"])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    calls = {"n": 0}
    def _unknown_create(repo, wt, branch, slug):
        calls["n"] += 1; return ("unknown", None)                 # outcome unknown every time
    P._push_and_open_pr = _unknown_create
    _run(P.propose(led))
    ok(calls["n"] <= P.MAX_PER_TICK,
       f"create ATTEMPTS bounded by MAX_PER_TICK ({calls['n']} <= {P.MAX_PER_TICK})")


def test_resolver_json_argv_is_one_field_list():
    # kilabz BLOCKER unmask: `--json A B` makes B gh's positional REPO argument; the field list
    # must be ONE comma-joined argv element. The old resolver test stubbed the helper and masked it.
    seen = {}
    def _record(path, *args):
        seen["args"] = args
        return {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}
    saved = P._gh_json_path
    P._gh_json_path = _record
    try:
        with tempfile.TemporaryDirectory() as d:
            rj = Path(d) / "repos.json"
            rj.write_text('{"myndaix-runtime":{"path":"' + d + '"}}')
            P.REPOS_JSON = rj
            (Path(d) / ".git").mkdir()
            _REAL_RESOLVE_REPO("myndaix-runtime")
        args = seen.get("args", ())
        ji = args.index("--json") if "--json" in args else -1
        ok(ji >= 0 and args[ji + 1] == "nameWithOwner,defaultBranchRef" and len(args) == ji + 2,
           f"--json takes ONE comma-joined field list as the FINAL arg (got {args})")
    finally:
        P._gh_json_path = saved


def test_dry_run_makes_zero_git_calls():
    # kilabz r2 #1: the per-candidate prune ran BEFORE the DRY_RUN guard — a dry tick mutated git.
    # Prune now lives in gc_worktrees (guarded). Assert a dry propose makes ZERO _git calls.
    _reset(monkey_dry=True)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    calls = {"n": 0}
    def _rec_git(*a, **k):
        calls["n"] += 1; return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._git = _rec_git
    _run(P.propose(led))
    P.gc_worktrees()                                   # DRY_RUN=True → must be a no-op too
    ok(calls["n"] == 0, f"A9: dry-run makes ZERO git calls incl. prune/GC (got {calls['n']})")
    ok(not P.CURSOR_FILE.exists(), "dry-run does not advance the fairness cursor")
    _reset(False)


def test_cursor_fairness_rotates_past_skipped():
    # kilabz r2 #7 + r3 MINOR: the test must be DISCRIMINATING — >100 skipped rows so the valid
    # candidate sits BEYOND the first batch (reaching it REQUIRES the persisted cursor), and the
    # wrap assertion checks the cursor's VALUE (which a no-op wrap cannot produce). kilabz proved
    # the old version passed with the cursor disabled entirely.
    _reset(False)
    skipped = [{"fingerprint": f"fp{i:03d}", "repo_scope": "unlisted", "rule_tag": "fail-open",
                "path_glob": "src/*.py", "decline_count": 0} for i in range(101)]
    ready = skipped + [{"fingerprint": "fpzz-valid", "repo_scope": "myndaix-runtime",
                        "rule_tag": "toctou-race", "path_glob": "src/*.py", "decline_count": 0}]
    P.resolve_repo = lambda s: (None if s == "unlisted" else
                                {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"})
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 150)
    # tick 1: only the first 100 skips fit the batch — the valid row is NOT reachable this tick
    led = FakeLedger(ready=ready, provenance=["deadbeef"])
    _run(P.propose(led))
    ok(led.mutations == [], "tick 1 visits only the first 100 skips (valid row beyond the batch)")
    ok(P.CURSOR_FILE.read_text() == "fp099", "cursor persisted at the last visited fingerprint")
    # tick 2: the scan RESUMES after fp099 — reaching fpzz-valid REQUIRES the cursor
    led2 = FakeLedger(ready=ready, provenance=["deadbeef"])
    _run(P.propose(led2))
    ok(("mark", "fpzz-valid", 150) in led2.mutations,
       "tick 2 reaches the valid candidate PAST 101 skips (cursor-driven rotation)")
    ok(P.CURSOR_FILE.read_text() == "fpzz-valid", "cursor at the tail after tick 2")
    # tick 3: tail exhausted → WRAP to the start; the cursor VALUE proves rows were re-visited
    led3 = FakeLedger(ready=skipped, provenance=["deadbeef"])
    _run(P.propose(led3))
    ok(led3.mutations == [], "wrapped scan revisits skips without claiming")
    ok(P.CURSOR_FILE.read_text() == "fp099",
       "wrap PROVEN: cursor moved from the tail back to the first batch's last fingerprint")


def test_dry_run_bypasses_arm_flag_live_does_not():
    # kilabz r4 MINOR: the pre-arm dry-run diagnostic must run WITHOUT the flag (a dry tick is
    # proven side-effect-free), while a LIVE tick without the flag must still exit before the
    # ledger is even touched.
    _reset(False)
    with tempfile.TemporaryDirectory() as d:
        P.ENABLED_FLAG = Path(d) / "PROPOSER_ENABLED"      # absent
        P.LOCK = Path(d) / "proposer.lock"
        P.REPOS_JSON = Path(d) / "repos.json"              # unreadable -> gc no-op
        connects = {"n": 0}
        class _FakePG:
            @staticmethod
            async def connect(dsn):
                connects["n"] += 1; return FakeLedger()
        saved = P.PostgresLedger
        P.PostgresLedger = _FakePG
        try:
            P.DRY_RUN = False
            ok(_run(P._amain()) == 0 and connects["n"] == 0,
               "LIVE tick without the flag exits before any ledger connect")
            P.DRY_RUN = True
            ok(_run(P._amain()) == 0 and connects["n"] == 1,
               "DRY_RUN tick without the flag PROCEEDS (the pre-arm diagnostic works)")
        finally:
            P.PostgresLedger = saved
            P.DRY_RUN = False


def test_gc_survives_bad_allowlist_entry():
    # kilabz r3 MAJOR: one malformed repos.json entry (non-string path / raising prune) must not
    # abort the whole tick — the sweep logs + continues, and healthy repos still get pruned.
    _reset(False)
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "goodrepo"; (good / ".git").mkdir(parents=True)
        rj = Path(d) / "repos.json"
        rj.write_text(json.dumps({
            "bad-type": {"path": 123},                       # TypeError-shaped
            "bad-missing": {"path": d + "/nope"},
            "good": {"path": str(good)},
        }))
        P.REPOS_JSON = rj
        pruned = []
        def _rec_git(cwd, *a, **k):
            pruned.append(str(cwd)); return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        P._git = _rec_git
        try:
            P.gc_worktrees()
        except Exception as e:
            ok(False, f"gc_worktrees raised through a bad entry: {e!r}")
        else:
            ok(any(str(good) in p for p in pruned), "the healthy repo is still pruned")
        # a RAISING prune on one repo must not stop the sweep either
        def _raise_git(cwd, *a, **k):
            raise RuntimeError("wedged prune")
        P._git = _raise_git
        try:
            P.gc_worktrees()
            ok(True, "a raising prune is caught per-entry (sweep + tick continue)")
        except Exception as e:
            ok(False, f"raising prune escaped gc_worktrees: {e!r}")


def test_git_hooks_disabled_on_every_git_op():
    # kilabz BLOCKER: tracked hooks must never run during worktree/commit/push (they inherit
    # GH_TOKEN + can stage files post-assert + fire the orchestrator pre-push reviewer).
    ok(P._GIT_NOHOOKS == ("-c", "core.hooksPath=/dev/null"), "hook-disable config pair defined")
    r = _REAL_GIT(Path("/tmp"), "version")
    ok(r.returncode == 0, "_git still executes with the no-hooks config injected")


# ---- A9: DRY_RUN mutates NOTHING across propose + reconcile ----------------------------------
def test_dry_run_propose_no_mutation():
    _reset(monkey_dry=True)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda scope: {"path": Path("/tmp/x"), "nwo": "redeyefit/myndaix-runtime",
                                    "default_branch": "main"}
    _run(P.propose(led))
    ok(led.mutations == [], f"DRY_RUN propose opened/claimed NOTHING (got {led.mutations})")
    _reset(False)


def test_dry_run_reconcile_no_mutation():
    _reset(monkey_dry=True)
    led = FakeLedger(proposed=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                                "rule_tag": "fail-open", "pr_number": 9, "branch": "skill/auto/fail-open",
                                "proposed_at": None}])
    P.resolve_repo = lambda scope: {"nwo": "redeyefit/myndaix-runtime", "path": Path("/tmp/x"),
                                    "default_branch": "main"}
    P._gh_json = lambda nwo, *a: {"state": "MERGED", "mergedAt": "2026-09-13T00:00:00Z"}
    _run(P.reconcile(led))
    ok(led.mutations == [], "DRY_RUN reconcile resolved NOTHING even for a merged PR (A9)")
    _reset(False)


# ---- A5/A7: reconcile acts only on definitive states -----------------------------------------
def _proposed_one():
    return [{"fingerprint": "fp1", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
             "pr_number": 9, "branch": "skill/auto/fail-open", "proposed_at": None}]


def test_reconcile_defers_on_gh_unknown():
    _reset(False)
    led = FakeLedger(proposed=_proposed_one())
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._gh_json = lambda nwo, *a: None                      # gh down / rate-limited
    _run(P.reconcile(led))
    ok(led.mutations == [], "A5: gh-unknown (None) -> DEFER, never resolve")


def test_reconcile_promotes_and_declines():
    _reset(False)
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    led = FakeLedger(proposed=_proposed_one())
    P._gh_json = lambda nwo, *a: {"state": "MERGED", "mergedAt": "2026-09-13T00:00:00Z"}
    _run(P.reconcile(led))
    ok(("resolve", "fp1", "promoted") in led.mutations, "merged PR -> promoted")
    led = FakeLedger(proposed=_proposed_one())
    P._gh_json = lambda nwo, *a: {"state": "CLOSED", "mergedAt": None}
    _run(P.reconcile(led))
    ok(("resolve", "fp1", "declined") in led.mutations, "closed-unmerged PR -> declined")
    led = FakeLedger(proposed=_proposed_one())
    P._gh_json = lambda nwo, *a: {"state": "OPEN", "mergedAt": None}
    _run(P.reconcile(led))
    ok(led.mutations == [], "an OPEN PR within TTL -> no resolution")


# ---- happy path: propose claims, opens, marks ------------------------------------------------
def test_propose_happy_path_marks_pr():
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}],
                     provenance=["deadbeef"])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._find_open_bot_pr = lambda repo, branch: ("none", None)   # definitive absence
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 142)
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()  # worktree remove no-op
    _run(P.propose(led))
    verbs = [m[0] for m in led.mutations]
    ok("claim" in verbs, "claimed the ready class")
    ok(("mark", "fp1", 142) in led.mutations, "marked proposed with the opened PR number")


def test_propose_adopts_existing_pr():
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._find_open_bot_pr = lambda repo, branch: ("found", 77)  # K1: a bot PR already open (crash recovery)
    opened = {"made": False}
    def _boom(*a, **k):
        opened["made"] = True; return Path("/tmp/wt")
    P._make_proposal_commit = _boom
    _run(P.propose(led))
    ok(("mark", "fp1", 77) in led.mutations, "adopted the existing PR (marked with its number)")
    ok(not opened["made"], "K1: did NOT create a second PR when one already exists")


def test_propose_skips_unlisted_scope():
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "wf_80d11feb-cb4-3",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: None                        # unlisted / polluted scope
    _run(P.propose(led))
    ok(led.mutations == [], "A2: a scope not in the allowlist is skipped before any claim/PR")


# ---- oracle code-review folds ------------------------------------------------------------------
def test_ttl_close_failure_defers_not_stale():
    # oracle BLOCKER: a FAILED `gh pr close` must DEFER — never record 'stale' and orphan a live PR.
    _reset(False)
    import datetime as dt
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    led = FakeLedger(proposed=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                                "rule_tag": "fail-open", "pr_number": 9,
                                "branch": "skill/auto/fail-open", "proposed_at": old}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._gh_json = lambda nwo, *a: {"state": "OPEN", "mergedAt": None}     # over-TTL and still open
    P._gh_close = lambda nwo, pr: False                                   # the close FAILS
    _run(P.reconcile(led))
    ok(led.mutations == [], "failed TTL close -> defer (no 'stale' write, PR not orphaned)")
    P._gh_close = lambda nwo, pr: True                                    # the close succeeds
    _run(P.reconcile(led))
    ok(("resolve", "fp1", "stale") in led.mutations, "confirmed close -> stale recorded")


def test_propose_poison_pill_does_not_wedge_tick():
    # oracle MAJOR: a candidate that RAISES before the claim (provenance/render) must be skipped,
    # not crash the tick — and the NEXT candidate must still be processed.
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "bad", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0},
                            {"fingerprint": "good", "repo_scope": "myndaix-runtime",
                             "rule_tag": "toctou-race", "path_glob": "src/*.py", "decline_count": 0}])
    async def _prov(fp, limit=8):
        if fp == "bad":
            raise RuntimeError("malformed provenance row")
        return ["deadbeef"]
    led.capture_provenance = _prov
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 143)
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    _run(P.propose(led))
    ok(("mark", "good", 143) in led.mutations, "the candidate AFTER the poison pill still proposes")
    ok(not any(m[0] == "release" and m[1] == "bad" for m in led.mutations),
       "a pre-claim raise releases nothing (it never claimed)")


def test_reconcile_poison_pill_does_not_wedge_tick():
    # oracle MAJOR: a raising reconcile row must defer, and the NEXT row must still resolve.
    _reset(False)
    led = FakeLedger(proposed=[{"fingerprint": "bad", "repo_scope": "myndaix-runtime",
                                "rule_tag": "fail-open", "pr_number": 1,
                                "branch": "skill/auto/fail-open", "proposed_at": None},
                               {"fingerprint": "good", "repo_scope": "myndaix-runtime",
                                "rule_tag": "toctou-race", "pr_number": 2,
                                "branch": "skill/auto/toctou-race", "proposed_at": None}])
    async def _resolve(fp, outcome):
        if fp == "bad":
            raise RuntimeError("db hiccup")
        led.mutations.append(("resolve", fp, outcome)); return True
    led.resolve_capture = _resolve
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._gh_json = lambda nwo, *a: {"state": "MERGED", "mergedAt": "2026-09-13T00:00:00Z"}
    _run(P.reconcile(led))
    ok(("resolve", "good", "promoted") in led.mutations,
       "the row AFTER a raising reconcile row is still resolved (tick not wedged)")


# ---- P1 push-wedge: nonzero push exit must re-verify the remote ref ---------------------------
def test_push_failure_reverifies_remote_ref():
    # P1: a nonzero `git push` exit does NOT prove the ref didn't update (a network drop AFTER the
    # server applied refs/heads/<branch> exits 128) — the old definitive "no-pr" cleared the
    # suspect + refunded, and the next tick's branch-exists guard then REFUSED create forever.
    # The fix re-verifies: ref at OUR sha -> the push landed, proceed to create; "absent" must be
    # confirmed by TWO spaced reads (a lagging read right after a failed push is evidence about
    # NOW, not the push's final server-side outcome); foreign sha -> no-pr-foreign; lookup
    # failure/raise -> unknown (never-raises contract intact).
    _reset(False)
    saved_git, saved_run = P._git, P.subprocess.run
    saved_pause = getattr(P, "PUSH_REVERIFY_PAUSE_S", None)
    P.PUSH_REVERIFY_PAUSE_S = 0
    repo = {"nwo": "o/r", "default_branch": "main", "path": Path("/tmp/x")}

    def run_case(ls_script, rev_raises=False):
        state = {"ls": 0, "argvs": [], "gh": 0}
        def fake_git(cwd, *a, **k):
            state["argvs"].append(a)
            if a and a[0] == "push":
                return _res(1, "", "fatal: the remote end hung up unexpectedly")
            if a and a[0] == "rev-parse":
                if rev_raises:
                    raise P.subprocess.TimeoutExpired("git", 1)
                return _res(0, "abc123\n", "")
            if a and a[0] == "ls-remote":
                step = ls_script[min(state["ls"], len(ls_script) - 1)]
                state["ls"] += 1
                if step == "raise":
                    raise P.subprocess.TimeoutExpired("git", 1)
                return _res(step[0], step[1], "")
            return _res()
        def fake_run(*a, **k):
            state["gh"] += 1
            return _res(0, "https://github.com/o/r/pull/9\n", "")
        P._git, P.subprocess.run = fake_git, fake_run
        return _REAL_PUSH_PR(repo, Path("/tmp/wt"), "skill/auto/x", "x"), state

    try:
        # (A) DISCRIMINATOR: ref present at OUR sha -> the push landed; PR still gets created
        r, st = run_case([(0, "abc123\trefs/heads/skill/auto/x\n")])
        ok(r == ("opened", 9), f"push-exit-nonzero + ref at our sha -> PR created (got {r})")
        ok(st["gh"] == 1, f"gh pr create invoked exactly once (got {st['gh']})")
        ls = next((a for a in st["argvs"] if a and a[0] == "ls-remote"), None)
        ok(ls is not None and ls[-1] == "refs/heads/skill/auto/x",
           f"ls-remote queries the FULL ref (suffix-match trap guard) (got {ls})")
        # (B) verified absent -> definitive no-pr, but only after TWO consistent reads
        r, st = run_case([(0, "")])
        ok(r == ("no-pr", None) and st["gh"] == 0, f"double-read absent -> no-pr, no create (got {r})")
        ok(st["ls"] == 2, f"'absent' requires TWO spaced ls-remote reads (got {st['ls']})")
        # (B2) read1 absent, read2 present at our sha: the ref materialized late -> still create
        r, st = run_case([(0, ""), (0, "abc123\trefs/heads/skill/auto/x\n")])
        ok(r == ("opened", 9), f"late-applied ref caught by the second read -> created (got {r})")
        # (B3) read1 absent, read2 raising -> unknown (disagreement is never definitive)
        r, st = run_case([(0, ""), "raise"])
        ok(r == ("unknown", None) and st["gh"] == 0, f"read2 raise -> unknown (got {r})")
        # (C) ref lookup failed -> unknown (caller keeps suspect + reservation)
        r, st = run_case([(1, "")])
        ok(r == ("unknown", None) and st["gh"] == 0, f"ls-remote nonzero -> unknown (got {r})")
        # (D) foreign sha: our PLAIN no-force push cannot have landed -> no-pr-foreign
        r, st = run_case([(0, "0000beef\trefs/heads/skill/auto/x\n")])
        ok(r == ("no-pr-foreign", None) and st["gh"] == 0, f"foreign sha -> no-pr-foreign (got {r})")
        # (E) never-raises: a raising ls-remote / rev-parse stays inside the contract
        r, st = run_case(["raise"])
        ok(r == ("unknown", None), f"ls-remote raise -> unknown, no escape (got {r})")
        r, st = run_case([(0, "")], rev_raises=True)
        ok(r == ("unknown", None), f"rev-parse raise -> unknown via the outer try (got {r})")
    finally:
        P._git, P.subprocess.run = saved_git, saved_run
        if saved_pause is None:
            try:
                del P.PUSH_REVERIFY_PAUSE_S
            except AttributeError:
                pass
        else:
            P.PUSH_REVERIFY_PAUSE_S = saved_pause


def test_verified_absent_push_failure_keeps_suspect():
    # attack amendment (crash-window hole 2): a "no-pr" from the failed-push re-verification path
    # must NOT clear the write-ahead suspect — absence read seconds after a failed push can be a
    # lagging read of a ref the server applies late; only the FOREIGN-sha flavor clears (positive
    # evidence the branch content is not ours). Refund + release still happen (clean retry).
    _reset(False)
    ready = [{"fingerprint": "fp1", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
              "path_glob": "src/*.py", "decline_count": 0}]
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: _res()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("no-pr", None)
    try:
        led = FakeLedger(ready=ready)
        _run(P.propose(led))
        suspects = json.loads(P.SUSPECTS_FILE.read_text())
        ok(len(suspects) == 1 and suspects[0]["fingerprint"] == "fp1",
           "verified-absent no-pr KEEPS the suspect (next tick's resolution disposes of it)")
        ok(("release", "fp1") in led.mutations, "the claim is still released (row back to 'ready')")
        # the FOREIGN flavor is the one sub-case that must clear (a kept suspect would arm the
        # recovery-create over foreign content)
        _reset(False)
        P._push_and_open_pr = lambda repo, wt, branch, slug: ("no-pr-foreign", None)
        led2 = FakeLedger(ready=ready)
        _run(P.propose(led2))
        ok(json.loads(P.SUSPECTS_FILE.read_text()) == [], "foreign-sha no-pr clears the suspect")
        ok(("release", "fp1") in led2.mutations, "foreign flavor also refunds + releases")
    finally:
        P._remote_branch_exists = saved_rbe


def test_stranded_pushed_branch_recovered_without_push():
    # attack amendment (crash-window hole 1): a durable write-ahead suspect + PR probe "none" +
    # remote branch PRESENT means WE verified the branch absent, pushed, and crashed (or gh
    # failed) before any PR existed. The old "none" arm cleared the durable suspect and the
    # branch-exists guard then REFUSED create forever. Recovery: open the PR for the EXISTING
    # remote head — NO push, so the no-overwrite rule stays intact.
    _reset(False)
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text(json.dumps([{"fingerprint": "fpS", "repo_scope": "myndaix-runtime",
                                            "branch": "skill/auto/fail-open"}]))
    led = FakeLedger(open_count=0, ready=[])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    git_argvs = []
    def _rec_git(cwd, *a, **k):
        git_argvs.append(a); return _res()
    P._git = _rec_git
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: True
    gh_argvs = []
    saved_run = P.subprocess.run
    def _gh(argv, **k):
        gh_argvs.append(argv); return _res(0, "https://github.com/o/r/pull/91\n", "")
    P.subprocess.run = _gh
    try:
        _run(P.propose(led))
        ok(("mark", "fpS", 91) in led.mutations, "the stranded branch's PR is created + marked")
        ok(any("--head" in a and "skill/auto/fail-open" in a for a in gh_argvs),
           f"gh pr create targets the EXISTING remote head (got {gh_argvs})")
        ok(not any(a and a[0] == "push" for a in git_argvs),
           f"recovery makes NO git push (no overwrite) (got {git_argvs})")
        ok(json.loads(P.SUSPECTS_FILE.read_text()) == [], "the recovered suspect is cleared")
    finally:
        P._remote_branch_exists = saved_rbe
        P.subprocess.run = saved_run


# ---- capacity accounting: conversion, terminal drop, shared probe budget -----------------------
def test_adopting_reserved_suspect_converts_not_double_counts():
    # capacity (a) P1: a suspect reserved at resolution time (probe unknown) whose main-scan probe
    # then found the PR was counted TWICE (tick-start reserve + bare adopt increment) — n_open hit
    # MAX_OPEN with only 2 real PRs and starved the next candidate. The adopt must CONVERT the
    # reservation in place.
    _reset(False)
    ready = [{"fingerprint": "fpA", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
              "path_glob": "src/*.py", "decline_count": 0},
             {"fingerprint": "fpZ", "repo_scope": "myndaix-runtime", "rule_tag": "toctou-race",
              "path_glob": "src/*.py", "decline_count": 0}]
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text(json.dumps([{"fingerprint": "fpA", "repo_scope": "myndaix-runtime",
                                            "branch": "skill/auto/fail-open"}]))
    led = FakeLedger(open_count=1, ready=ready)
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: _res()
    calls = {"failopen": 0}
    def _probe(repo, branch):
        if branch.endswith("fail-open"):
            calls["failopen"] += 1
            return ("unknown", None) if calls["failopen"] == 1 else ("found", 91)
        return ("none", None)
    P._find_open_bot_pr = _probe
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 45)
    try:
        _run(P.propose(led))
        ok(("mark", "fpA", 91) in led.mutations, "the reserved suspect's PR is adopted mid-scan")
        ok(("mark", "fpZ", 45) in led.mutations,
           "conversion, not double-count: fpZ still creates (1 tracked + adopted fpA = 2 < cap 3)")
        ok(json.loads(P.SUSPECTS_FILE.read_text()) == [], "the adopted suspect is cleared in-tick")
    finally:
        P._remote_branch_exists = saved_rbe


def test_unclaimable_terminal_suspect_dropped_not_leaked():
    # capacity (b) P2: a found-but-unclaimable suspect whose candidate left 'ready' OUTSIDE the
    # proposer (no concurrent writer exists under the flock — the ledger disposed of it) can
    # NEVER complete; keep+reserve leaked one MAX_OPEN slot every tick forever. Drop it LOUDLY:
    # log + a jefe-inbox note (the residual is permanent-until-human and STACKS per orphan, so
    # the human channel is load-bearing — launchd stderr is not one).
    _reset(False)
    home = Path(tempfile.mkdtemp(prefix="mdx-test-home."))
    saved_home = P.HOME
    P.HOME = home
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text(json.dumps([{"fingerprint": "fpB", "repo_scope": "myndaix-runtime",
                                            "branch": "skill/auto/fail-open"}]))
    led = FakeLedger(open_count=2, ready=[{"fingerprint": "fpZ", "repo_scope": "myndaix-runtime",
                                           "rule_tag": "toctou-race", "path_glob": "src/*.py",
                                           "decline_count": 0}])
    async def _claim(fp, branch, draft_sha):
        led.mutations.append(("claim", fp, branch))
        return fp != "fpB"                                       # the terminal row refuses a claim
    led.claim_for_proposing = _claim
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: _res()
    P._find_open_bot_pr = lambda repo, branch: (("found", 44) if branch.endswith("fail-open")
                                                else ("none", None))
    saved_rbe = P._remote_branch_exists
    P._remote_branch_exists = lambda repo, branch: False
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: ("opened", 45)
    try:
        _run(P.propose(led))
        ok(("mark", "fpZ", 45) in led.mutations, "the freed slot is usable: fpZ still creates")
        ok(json.loads(P.SUSPECTS_FILE.read_text()) == [], "the terminal suspect is dropped, not leaked")
        ok(not any(m[0] == "mark" and m[1] == "fpB" for m in led.mutations), "fpB is never marked")
        inbox = home / ".myndaix" / "bridge" / "inbox" / "jefe"
        drops = list(inbox.glob("*.md")) if inbox.is_dir() else []
        ok(len(drops) == 1 and "44" in drops[0].read_text(),
           f"the drop reaches the jefe inbox naming PR#44 (got {[d.name for d in drops]})")
    finally:
        P._remote_branch_exists = saved_rbe
        P.HOME = saved_home


def test_suspect_probes_share_probe_cap():
    # capacity (c) P2: suspect-resolution probes bypassed PROBE_CAP (N suspects + PROBE_CAP gh
    # reads per tick). They now share the budget; cap-hit suspects are kept + reserved and
    # ROTATED to the file head so a persistently-unknown head cannot starve the tail forever.
    # CAPTURE_MAX_OPEN=1 keeps the MAX_OPEN+1 floor (2) from lifting the tightened cap (2).
    _reset(False)
    saved_cap = P.PROBE_CAP
    prior_max_open = os.environ.get("CAPTURE_MAX_OPEN")
    os.environ["CAPTURE_MAX_OPEN"] = "1"
    P.PROBE_CAP = 2
    P.SUSPECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    P.SUSPECTS_FILE.write_text(json.dumps([
        {"fingerprint": "fpS1", "repo_scope": "myndaix-runtime", "branch": "skill/auto/a"},
        {"fingerprint": "fpS2", "repo_scope": "myndaix-runtime", "branch": "skill/auto/b"},
        {"fingerprint": "fpS3", "repo_scope": "myndaix-runtime", "branch": "skill/auto/c"}]))
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: _res()
    probed = []
    def _probe(repo, branch):
        probed.append(branch); return ("unknown", None)
    P._find_open_bot_pr = _probe
    try:
        led = FakeLedger(open_count=0, ready=[{"fingerprint": "fpZ", "repo_scope": "myndaix-runtime",
                                               "rule_tag": "toctou-race", "path_glob": "src/*.py",
                                               "decline_count": 0}])
        _run(P.propose(led))
        ok(len(probed) == 2, f"suspect probes share the cap: exactly 2 this tick (got {len(probed)})")
        ok("skill/auto/toctou-race" not in probed,
           "cap exhausted by suspects -> the ready scan makes no probe (fail-closed break)")
        saved = json.loads(P.SUSPECTS_FILE.read_text())
        ok({s["fingerprint"] for s in saved} == {"fpS1", "fpS2", "fpS3"},
           f"all three suspects kept (2 probed-unknown + 1 cap-deferred) (got {saved})")
        ok(bool(saved) and saved[0]["fingerprint"] == "fpS3",
           f"the cap-deferred suspect rotates to the file HEAD (got {[s['fingerprint'] for s in saved]})")
        ok(led.mutations == [], "no claims/creates under an exhausted budget")
        # tick 2: rotation means the previously-deferred suspect is probed FIRST
        led2 = FakeLedger(open_count=0, ready=[])
        _run(P.propose(led2))
        ok("skill/auto/c" in probed, "tick 2 probes the previously-deferred suspect (rotation works)")
    finally:
        P.PROBE_CAP = saved_cap
        if prior_max_open is None:
            os.environ.pop("CAPTURE_MAX_OPEN", None)
        else:
            os.environ["CAPTURE_MAX_OPEN"] = prior_max_open


# ---- write-ahead intent durability (fsync ordering) --------------------------------------------
def test_save_suspects_durable_ordering():
    # fsync P1: write_text + replace left the write-ahead intent in the page cache — a crash
    # after `gh pr create` lost the suspect (arm a: untracked PR, MAX_OPEN undercount) or made
    # the rename durable pointing at unwritten data (arm b: DAMAGED file -> creation wedged every
    # tick with no self-heal). Required order: file-data durability (F_FULLFSYNC on macOS,
    # os.fsync fallback) BEFORE the rename, directory fsync AFTER it.
    _reset(False)
    import stat as _stat
    events = []
    def _has_entry():
        try:
            return "fpA" in P.SUSPECTS_FILE.read_text()
        except OSError:
            return False
    real_fsync, real_fcntl = os.fsync, P.fcntl.fcntl
    full = getattr(P.fcntl, "F_FULLFSYNC", None)
    def rec_fsync(fd):
        events.append(("dir" if _stat.S_ISDIR(os.fstat(fd).st_mode) else "file", _has_entry()))
    def rec_fcntl(fd, cmd, *a):
        if full is not None and cmd == full:
            events.append(("dir" if _stat.S_ISDIR(os.fstat(fd).st_mode) else "file", _has_entry()))
            return 0
        return real_fcntl(fd, cmd, *a)
    os.fsync = rec_fsync
    P.fcntl.fcntl = rec_fcntl
    try:
        ok(P._add_suspect("fpA", "myndaix-runtime", "skill/auto/x"), "durable add returns True")
        files = [e for e in events if e[0] == "file"]
        dirs = [e for e in events if e[0] == "dir"]
        ok(len(files) == 1 and len(dirs) == 1,
           f"exactly one FILE durability call + one DIR fsync (got {events})")
        ok(bool(files) and files[0][1] is False,
           "file-data durability precedes the rename (target does not yet hold the entry)")
        ok(bool(dirs) and dirs[0][1] is True,
           "dir fsync follows the rename (target already holds the entry)")
        data = json.loads(P.SUSPECTS_FILE.read_text())
        ok(any(s.get("fingerprint") == "fpA" for s in data), "final content parses + holds the entry")
    finally:
        os.fsync = real_fsync
        P.fcntl.fcntl = real_fcntl


def test_add_suspect_fsync_failure_refuses():
    # the fsync sits INSIDE the try/except-OSError: an undurable intent must make _add_suspect
    # return False so the caller refuses to create (ff-r4 P1 contract) — a sick disk becomes
    # per-candidate create refusals, never a silent cache-only "success".
    _reset(False)
    real_fsync, real_fcntl = os.fsync, P.fcntl.fcntl
    full = getattr(P.fcntl, "F_FULLFSYNC", None)
    def boom_fsync(fd):
        raise OSError("disk sick")
    def boom_fcntl(fd, cmd, *a):
        if full is not None and cmd == full:
            raise OSError("disk sick")
        return real_fcntl(fd, cmd, *a)
    os.fsync = boom_fsync
    P.fcntl.fcntl = boom_fcntl
    try:
        ok(P._add_suspect("fpA", "myndaix-runtime", "skill/auto/x") is False,
           "fsync failure -> _add_suspect False (caller refuses to create)")
    finally:
        os.fsync = real_fsync
        P.fcntl.fcntl = real_fcntl


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
