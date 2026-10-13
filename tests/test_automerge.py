"""automerge gate proofs — the SECURITY-critical pure core (classify_diff + denylist)
hammered adversarially, plus the raw-diff parser. The merge/gh/git I/O is integration-
tested by the live DRY-RUN (test.sh); here we prove the un-gameable gate logic.

Run:  PYTHONPATH=src python3 tests/test_automerge.py
"""
import runtime.automerge as A

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


def entry(status, omode, nmode, *paths):
    return {"status": status, "omode": omode, "nmode": nmode, "paths": list(paths)}


# -- classify_diff: the security gate truth-table -----------------------------
def test_classify_allows_plain_docs():
    ok(A.classify_diff([entry("M", "100644", "100644", "README.md")])[0], "modify .md allowed")
    ok(A.classify_diff([entry("A", "000000", "100644", "docs/new.md")])[0], "add .md allowed")
    ok(A.classify_diff([entry("D", "100644", "000000", "docs/old.md")])[0], "delete .md allowed")
    ok(A.classify_diff([entry("R100", "100644", "100644", "a.md", "b.md")])[0], "rename .md->.md allowed")
    ok(A.classify_diff([entry("M", "100644", "100644", "a.md"),
                        entry("A", "000000", "100644", "b.md")])[0], "multi .md allowed")


def test_classify_rejects_nondoc_and_modes():
    bad = [
        ("non-.md", [entry("M", "100644", "100644", "src/x.py")]),
        ("empty diff", []),
        ("symlink new mode", [entry("A", "000000", "120000", "link.md")]),
        ("gitlink/submodule new mode", [entry("A", "000000", "160000", "sub.md")]),
        ("executable new mode", [entry("M", "100644", "100755", "run.md")]),
        ("rename code->md (old side .py)", [entry("R100", "100644", "100644", "evil.py", "evil.md")]),
        ("rename md->code (new side .py)", [entry("R100", "100644", "100644", "a.md", "evil.py")]),
        ("delete with non-doc old mode", [entry("D", "100755", "000000", "x.md")]),
        ("no extension", [entry("M", "100644", "100644", "Makefile")]),
        ("uppercase .MD (case strict)", [entry("M", "100644", "100644", "READ.MD")]),
        ("trailing dot .md.", [entry("M", "100644", "100644", "x.md.")]),
        # codex BLOCKER: validating only the NEW mode let these through —
        ("typechange T from symlink->md", [entry("T", "120000", "100644", "link.md")]),
        ("typechange T from exec->md", [entry("T", "100755", "100644", "run.md")]),
        ("copy C from a symlink old side", [entry("C100", "120000", "100644", "old.md", "new.md")]),
        ("modify with symlink OLD mode", [entry("M", "120000", "100644", "x.md")]),
        ("modify with gitlink OLD mode", [entry("M", "160000", "100644", "x.md")]),
        ("unknown status X", [entry("X", "100644", "100644", "x.md")]),
        ("unmerged status U", [entry("U", "100644", "100644", "x.md")]),
        ("add with non-zero old mode", [entry("A", "100644", "100644", "x.md")]),
        ("delete with non-zero new mode", [entry("D", "100644", "100644", "x.md")]),
        ("malformed status A999 (strict grammar)", [entry("A999", "000000", "100644", "x.md")]),
        ("malformed status Mfoo", [entry("Mfoo", "100644", "100644", "x.md")]),
        ("malformed status R (no score)", [entry("Rx", "100644", "100644", "a.md", "b.md")]),
    ]
    for label, entries in bad:
        ok(not A.classify_diff(entries)[0], f"reject: {label}")


def test_classify_rejects_denylisted_docs():
    deny = ["CLAUDE.md", "AGENTS.md", "GEMINI.md", "dir/CLAUDE.md", ".cursorrules",
            "COPILOT-INSTRUCTIONS.md", "SECURITY.md", "CODEOWNERS",
            ".github/PULL_REQUEST_TEMPLATE.md", ".claude/skills/foo.md", ".codex/x.md",
            "any/rules/policy.md", "pkg/skills/s.md", "x/prompts/p.md", ".agents/a.md",
            "DESIGN.md", "docs/controller-loop-design.md", "docs/x-spec.md", "docs/OPERATING.md"]
    for p in deny:
        ok(not A.classify_diff([entry("M", "100644", "100644", p)])[0], f"deny instruction/ground-truth: {p}")
    # denylist applies to BOTH rename sides (can't rename CLAUDE.md to a benign name)
    ok(not A.classify_diff([entry("R100", "100644", "100644", "CLAUDE.md", "harmless.md")])[0],
       "rename FROM a denylisted file rejected")
    ok(not A.classify_diff([entry("R100", "100644", "100644", "notes.md", "AGENTS.md")])[0],
       "rename TO a denylisted file rejected")
    # a normal doc is allowed (denylist is specific, not blanket)
    ok(A.classify_diff([entry("M", "100644", "100644", "README.md")])[0], "README.md still allowed")
    ok(A.classify_diff([entry("M", "100644", "100644", "docs/notes.md")])[0], "plain docs/notes.md allowed")


# -- parse_raw_z: git diff --raw -z -M parsing --------------------------------
def test_parse_raw_z():
    # modify + add (one path each), NUL-delimited, concatenated
    out = (b":100644 100644 aaa bbb M\x00README.md\x00"
           b":000000 100644 000 ccc A\x00docs/new.md\x00")
    es = A.parse_raw_z(out)
    ok(len(es) == 2, "parsed 2 entries")
    ok(es[0]["status"] == "M" and es[0]["paths"] == ["README.md"], "modify parsed")
    ok(es[1]["status"] == "A" and es[1]["nmode"] == "100644", "add parsed")
    # rename has TWO paths
    out2 = b":100644 100644 aaa aaa R100\x00old.md\x00new.md\x00"
    es2 = A.parse_raw_z(out2)
    ok(len(es2) == 1 and es2[0]["paths"] == ["old.md", "new.md"], "rename two-path parsed")
    # end-to-end: a code rename to .md is parsed AND rejected
    out3 = b":100644 100644 aaa aaa R100\x00evil.py\x00evil.md\x00"
    ok(not A.classify_diff(A.parse_raw_z(out3))[0], "parsed code->md rename rejected")
    ok(A.parse_raw_z(b"") == [], "empty diff -> no entries")


def test_parse_raw_z_is_strict():
    # a strict parser RAISES on any anomaly (never silently skips a malformed entry)
    def raises(out, label):
        try:
            A.parse_raw_z(out)
            ok(False, f"should have raised: {label}")
        except ValueError:
            ok(True, f"raised on: {label}")
    raises(b"garbage-not-an-info-line\x00", "non-info leading token")
    raises(b":100644 100644 aaa bbb\x00x.md\x00", "info line with 4 fields")
    raises(b":100644 100644 aaa bbb M\x00", "truncated: missing path")
    raises(b":100644 100644 aaa aaa R100\x00only-one.md\x00", "rename missing second path")
    raises(b":100644 100644 aaa bbb M\x00README.md\x00trailing-non-info\x00", "trailing non-info token")
    # a malformed SECOND entry must raise (not be silently skipped after a safe first entry)
    raises(b":100644 100644 a b M\x00ok.md\x00:100644 100644 c M\x00x.md\x00", "second info has 4 fields")


# -- gate ORDERING: caps run BEFORE the paid review (spend-leak regression) ----
# The audit's #1 finding: gate-review-before-caps re-ran the full 3-agent review every
# hourly tick for any PR blocked only by a cap (a None decision records nothing → never
# deduped) — a paid-agent spend leak, and why a 2nd same-author docs PR looked "stuck".
# These stub the module I/O to drive evaluate_pr to the cap boundary and prove the review
# is only reached when the caps pass.
class _R:
    def __init__(self, rc=0, out=b""):
        self.returncode = rc
        self.stdout = out


def _run_eval(budget_val=0, day_count=0, author_count=0, review_ret="needs_fix"):
    """Stub every helper evaluate_pr calls up to the cap/review boundary and run it. `_count`
    is PATH-AWARE so each of the three caps (per-tick via budget, per-day, per-author) can be
    tripped in ISOLATION — the day and author counters live at distinct _day() paths. Returns
    (result, calls) where calls records each _review_pass invocation."""
    H, M, B = "a" * 40, "b" * 40, "c" * 40
    calls = []

    def fake_git(path, *args, **kw):
        a = args[0] if args else ""
        if a == "rev-parse":
            return _R(0, (H if "pr/" in args[1] else M).encode())
        if a == "merge-base":
            return _R(0, B.encode())
        return _R(0, b"stub")                      # fetch, diff --raw, diff content

    def fake_count(p):
        return author_count if "author" in str(p) else day_count

    def fake_review(repo, b, h):
        calls.append((b, h))
        return review_ret

    monkey = {"_git": fake_git, "_merge_queue": lambda repo: False,
              "parse_raw_z": lambda out: [], "classify_diff": lambda entries: (True, ""),
              "_ci_green": lambda repo, head: True, "_count": fake_count,
              "_review_pass": fake_review}
    saved = {k: getattr(A, k) for k in monkey}
    for k, v in monkey.items():
        setattr(A, k, v)
    try:
        pr = {"number": 45, "headRefOid": H, "baseRefOid": M,
              "author": {"login": "redeyefit"}, "isDraft": False,
              "isCrossRepository": False, "mergeStateStatus": "CLEAN"}
        res = A.evaluate_pr({"path": "/tmp/x", "nwo": "redeyefit/myndaix-runtime"}, pr, [budget_val])
        return res, calls
    finally:
        for k, v in saved.items():
            setattr(A, k, v)


def test_each_cap_blocks_before_the_paid_review():
    # EACH of the three caps, tripped in ISOLATION (the others clear), must DEFER (None)
    # WITHOUT running the paid review — the whole point of the reorder (kilabz LOW: prove all
    # three, not just the daily cap that would trip first under a blanket count).
    for label, kw in (("per-tick budget", dict(budget_val=A.MAX_PER_TICK)),
                      ("per-day cap",     dict(day_count=A.MAX_PER_DAY)),
                      ("per-author cap",  dict(author_count=A.MAX_PER_AUTHOR_DAY))):
        res, calls = _run_eval(**kw)
        ok(res is None, f"{label}: defers (None), not recorded terminal")
        ok(calls == [], f"{label}: paid review NOT called (no spend leak)")


def test_uncapped_pr_reaches_the_review():
    # all caps clear → the review DOES run (proves the reorder didn't skip the gate entirely)
    res, calls = _run_eval(review_ret="needs_fix")
    ok(len(calls) == 1, "the review runs when every cap passes")
    ok(res == ("needs_fix", "review did not PASS — human"), "review verdict flows through")


def test_line_precap_is_terminal_before_the_paid_review():
    # a docs diff over the worker's CHANGED-LINES cap (PLAY_MAX_DIFF_LINES) would gate-abort
    # exit-2 "transient" EVERY tick forever (workflow #2) — it must be a TERMINAL human skip
    # here, before the paid review, exactly like the byte pre-cap above it.
    H, M, B = "a" * 40, "b" * 40, "c" * 40
    calls = []

    def fake_git(path, *args, **kw):
        a = args[0] if args else ""
        if a == "rev-parse":
            return _R(0, (H if "pr/" in args[1] else M).encode())
        if a == "merge-base":
            return _R(0, B.encode())
        if a == "diff" and "--numstat" in args:
            return _R(0, b"3000\t500\tdocs/big.md\n")   # 3500 changed lines, tiny bytes
        return _R(0, b"stub")

    monkey = {"_git": fake_git, "_merge_queue": lambda repo: False,
              "parse_raw_z": lambda out: [], "classify_diff": lambda entries: (True, ""),
              "_ci_green": lambda repo, head: True, "_count": lambda p: 0,
              "_review_pass": lambda repo, b, h: calls.append((b, h)) or "pass"}
    saved = {k: getattr(A, k) for k in monkey}
    for k, v in monkey.items():
        setattr(A, k, v)
    try:
        pr = {"number": 45, "headRefOid": H, "baseRefOid": M,
              "author": {"login": "redeyefit"}, "isDraft": False,
              "isCrossRepository": False, "mergeStateStatus": "CLEAN"}
        res = A.evaluate_pr({"path": "/tmp/x", "nwo": "redeyefit/myndaix-runtime"}, pr, [0])
    finally:
        for k, v in saved.items():
            setattr(A, k, v)
    ok(isinstance(res, tuple) and res[0] == "skipped" and "changed lines" in res[1],
       "over-line-cap docs PR records a TERMINAL human skip (no eternal transient re-gate)")
    ok(calls == [], "the paid review is NOT called for an over-line-cap PR")


def test_transient_review_is_ceiling_bounded():
    # spine-audit HIGH: a transient verdict returns None (records nothing) so the paid 3-agent panel
    # re-runs EVERY tick forever. After MAX_REVIEW_ATTEMPTS on the SAME head it must record a terminal
    # human skip (deduped, stops re-spending). Uses the REAL _count/_bump_attempt against a temp STATE.
    import tempfile, pathlib
    H, M, B = "a" * 40, "b" * 40, "c" * 40
    calls = []

    def fake_git(path, *args, **kw):
        a = args[0] if args else ""
        if a == "rev-parse":
            return _R(0, (H if "pr/" in args[1] else M).encode())
        if a == "merge-base":
            return _R(0, B.encode())
        return _R(0, b"stub")

    tmpdir = pathlib.Path(tempfile.mkdtemp())
    monkey = {"_git": fake_git, "_merge_queue": lambda repo: False,
              "parse_raw_z": lambda out: [], "classify_diff": lambda e: (True, ""),
              "_ci_green": lambda repo, head: True, "STATE": tmpdir,   # real _count + _bump_attempt
              "_review_pass": lambda repo, b, h: calls.append((b, h)) or "transient"}
    saved = {k: getattr(A, k) for k in monkey}
    for k, v in monkey.items():
        setattr(A, k, v)
    try:
        pr = {"number": 45, "headRefOid": H, "baseRefOid": M,
              "author": {"login": "redeyefit"}, "isDraft": False,
              "isCrossRepository": False, "mergeStateStatus": "CLEAN"}
        repo = {"path": "/tmp/x", "nwo": "redeyefit/myndaix-runtime"}
        results = [A.evaluate_pr(repo, pr, [0]) for _ in range(A.MAX_REVIEW_ATTEMPTS)]
    finally:
        for k, v in saved.items():
            setattr(A, k, v)
    ok(all(r is None for r in results[:-1]),
       f"transient defers (None) for the first {A.MAX_REVIEW_ATTEMPTS - 1} attempts")
    ok(isinstance(results[-1], tuple) and results[-1][0] == "skipped" and "transient" in results[-1][1],
       "at the ceiling a terminal human skip is recorded (bounds the paid re-runs)")
    ok(len(calls) == A.MAX_REVIEW_ATTEMPTS,
       "the paid review runs at most MAX_REVIEW_ATTEMPTS times for one head, then never again")


def test_byte_precap_matches_worker_metric_at_the_boundary():
    # spine-audit LOW: the byte pre-cap must count like the worker — bash `$(git diff)` strips the
    # trailing newline, so a diff whose STRIPPED size == cap (raw == cap+1) must be REVIEWED, not
    # skipped (mirrors controller._diff_bytes rstrip). A genuinely-over diff is still a terminal skip.
    H, M, B = "a" * 40, "b" * 40, "c" * 40
    cap = 10

    def run(content):
        calls = []

        def fake_git(path, *args, **kw):
            a = args[0] if args else ""
            if a == "rev-parse":
                return _R(0, (H if "pr/" in args[1] else M).encode())
            if a == "merge-base":
                return _R(0, B.encode())
            if a == "diff" and "--numstat" in args:
                return _R(0, b"1\t0\tdocs/x.md\n")       # 1 line, well under the line cap
            if a == "diff" and "--raw" in args:
                return _R(0, b"")
            if a == "diff":
                return _R(0, content)                    # the content byte-cap diff
            return _R(0, b"stub")

        monkey = {"_git": fake_git, "_merge_queue": lambda repo: False,
                  "parse_raw_z": lambda out: [], "classify_diff": lambda e: (True, ""),
                  "_ci_green": lambda repo, head: True, "_count": lambda p: 0,
                  # needs_fix (terminal, reached) proves the review RAN without entering the merge
                  # subprocess path (which would need a real cwd) — we only assert it was reached.
                  "_review_pass": lambda repo, b, h: calls.append(1) or "needs_fix"}
        saved = {k: getattr(A, k) for k in monkey}
        saved_cap = A.REVIEW_MAX_DIFF
        A.REVIEW_MAX_DIFF = cap
        for k, v in monkey.items():
            setattr(A, k, v)
        try:
            pr = {"number": 45, "headRefOid": H, "baseRefOid": M,
                  "author": {"login": "redeyefit"}, "isDraft": False,
                  "isCrossRepository": False, "mergeStateStatus": "CLEAN"}
            res = A.evaluate_pr({"path": "/tmp/x", "nwo": "redeyefit/myndaix-runtime"}, pr, [0])
        finally:
            for k, v in saved.items():
                setattr(A, k, v)
            A.REVIEW_MAX_DIFF = saved_cap
        return res, calls

    res, calls = run(b"x" * cap + b"\n")                 # stripped == cap (raw == cap+1)
    ok(calls == [1] and not (isinstance(res, tuple) and res[0] == "skipped"),
       "a diff whose STRIPPED size == cap is reviewed, not skipped (boundary matches the worker)")
    res, calls = run(b"x" * (cap + 1))                   # genuinely over
    ok(isinstance(res, tuple) and res[0] == "skipped" and "review cap" in res[1] and calls == [],
       "a diff genuinely over the byte cap is a terminal skip before the paid review")


def test_gate_env_forwards_diff_caps():
    # §3's pre-check enforces REVIEW_MAX_DIFF_LINES/REVIEW_MAX_DIFF; the gate worker env MUST carry
    # the SAME values as PLAY_MAX_DIFF_LINES/PLAY_MAX_DIFF — else a raised automerge cap passes the
    # pre-check, then the worker aborts at its own default (the retry-forever wedge the caps kill).
    saved = (A.REVIEW_MAX_DIFF_LINES, A.REVIEW_MAX_DIFF)
    A.REVIEW_MAX_DIFF_LINES, A.REVIEW_MAX_DIFF = 5000, 999999
    try:
        env = A._gate_env("/tmp/verdict.json", "am-test")
    finally:
        A.REVIEW_MAX_DIFF_LINES, A.REVIEW_MAX_DIFF = saved
    ok(env.get("PLAY_MAX_DIFF_LINES") == "5000", "gate forwards the automerge line cap to the worker")
    ok(env.get("PLAY_MAX_DIFF") == "999999", "gate forwards the automerge byte cap to the worker")
    ok(env.get("PLAY_GATE") == "1" and env.get("PLAY_DISABLE_AUTOFIX") == "1",
       "gate env still carries the gate + autofix-off flags")


def test_int_env_strict_digit_only():
    # a malformed launchd value for a diff cap (fallback intent) must default, not crash at import.
    import os
    key = "MYNDAIX_TEST_AM_INT_ENV"
    saved = os.environ.get(key)
    try:
        for bad in ["-1", "+9", " 9 ", "9_9", "nan", ""]:  # non-digit -> default
            os.environ[key] = bad
            ok(A._int_env(key, 262144) == 262144, f"{bad!r} falls back to the default")
        os.environ[key] = "500"
        ok(A._int_env(key, 262144) == 500, "a clean digit string is honoured")
        os.environ[key] = "0" * 15 + "5"                 # zero-padded: strip BEFORE the len cap (r5)
        ok(A._int_env(key, 262144) == 5, "a leading-zero-padded small value is NOT capped")
        for big in ["9999999999", "9" * 5000]:           # astronomical (2nd trips int()'s 4300 limit)
            os.environ[key] = big
            ok(A._int_env(key, 262144) == 2**31 - 1, f"{big[:12]!r} caps at 2^31-1 (crash-proof)")
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def test_parse_authors_strips_and_drops_empty():
    # the REAL production parser (module uses _parse_authors for AUTHOR_ALLOWLIST). fail-CLOSED:
    # "" must never enter the allowlist (a null-login PR would pass the gate), and padded names
    # ("  bob") must normalise or a legit author silently never matches.
    ok(A._parse_authors("") == set(), "empty env -> empty allowlist (nothing auto-merges)")
    ok(A._parse_authors("redeyefit,") == {"redeyefit"}, "trailing comma -> no '' bypass")
    ok(A._parse_authors("alice, bob") == {"alice", "bob"}, "whitespace-padded names are stripped")
    ok(A._parse_authors("  ,  ") == set(), "all-whitespace/empty entries drop -> fail-closed")
    ok("" not in A.AUTHOR_ALLOWLIST, "the live module allowlist carries no '' entry")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
