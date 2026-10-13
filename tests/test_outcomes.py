"""outcomes-ledger pure core (v0.3) — path-scoped line-hash identity, the `finding:` line parser
with fail-closed sanitization, and the hunk-validated git-object resolve. DB-free + adversarial,
like test_capture.py.

Run:  PYTHONPATH=src python3 tests/test_outcomes.py
"""
import runtime.outcomes as O

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


# ---- line_hash: normalization + collision (SonarQube borrow) ----------------------------
def test_line_hash_normalizes_whitespace():
    ok(O.line_hash("  a   b  ") == O.line_hash("a b"), "leading/trailing/internal ws collapse -> same hash")
    ok(O.line_hash("a\tb") == O.line_hash("a b"), "a tab is a whitespace run -> same hash as a space")
    ok(O.line_hash("if (x) {") == O.line_hash("    if (x) {   "), "re-indentation does not change identity")
    ok(O.line_hash("a b") != O.line_hash("ab"), "token boundary preserved (a b != ab)")
    ok(len(O.line_hash("x")) == 64, "sha256 hex digest")


def test_line_hash_same_normalized_line_collides():
    # two identical normalized lines legitimately share a hash — ACCEPTED (like SonarQube).
    ok(O.line_hash("return None") == O.line_hash("return None"), "identical lines -> identical hash (accepted alias)")


# ---- finding_key: PATH IS IN THE KEY (the v0.1 CRIT regression) --------------------------
def test_finding_key_includes_path():
    lh = O.line_hash("return None")
    k_a = O.finding_key("repoA", "fail-open", "src/a.py", lh)
    k_b = O.finding_key("repoA", "fail-open", "src/b.py", lh)
    ok(k_a != k_b, "identical line in DIFFERENT files -> DIFFERENT keys (cross-file must not collide)")
    ok(k_a == O.finding_key("repoA", "fail-open", "src/a.py", lh), "same (repo,tag,path,hash) -> same key")
    ok(k_a != O.finding_key("repoB", "fail-open", "src/a.py", lh), "different repo -> different key")
    ok(k_a != O.finding_key("repoA", "toctou-race", "src/a.py", lh), "different tag -> different key")
    # NUL-separation: no field-boundary collision
    ok(O.finding_key("a", "b", "c", "d") != O.finding_key("a\x00b", "c", "d", ""),
       "NUL separator prevents field-boundary collision")
    ok(len(k_a) == 64, "sha256 hex digest")


# ---- parse_finding_lines: valid + fail-closed sanitization ------------------------------
def test_parse_valid_finding():
    txt = "some prose\nfinding:fail-open @ src/runtime/x.py:42\nmore prose\n"
    found, dropped = O.parse_finding_lines(txt)
    ok(len(found) == 1 and dropped == 0, "one clean finding parsed, nothing dropped")
    ok(found[0] == {"tag": "fail-open", "path": "src/runtime/x.py", "line": 42},
       "tag/path/line extracted correctly")


def test_parse_ctrl_char_reject_on_raw():
    # a control char (tab) injected into the path portion drops the WHOLE finding, on the raw line.
    txt = "finding:fail-open @ src/a\tb.py:10"
    found, dropped = O.parse_finding_lines(txt)
    ok(found == [] and dropped == 1, "tab in path -> dropped on raw line (fail-closed)")
    # a DEL/other C0 in the tag portion likewise drops
    found2, dropped2 = O.parse_finding_lines("finding:fail\x07open @ src/a.py:1")
    ok(found2 == [], "control char in tag region -> dropped")


def test_parse_offlist_tag_reject():
    found, dropped = O.parse_finding_lines("finding:totally-made-up @ src/a.py:5")
    ok(found == [] and dropped == 1, "off-list tag -> dropped (S3, allowlist)")
    # a restricted allowed_tags set also rejects an otherwise-allowlisted tag
    found2, _ = O.parse_finding_lines("finding:fail-open @ src/a.py:5", allowed_tags={"toctou-race"})
    ok(found2 == [], "tag not in the passed allowed_tags -> dropped")


def test_parse_skills_path_reject():
    found, dropped = O.parse_finding_lines("finding:fail-open @ skills/x/SKILL.md:3")
    ok(found == [] and dropped == 1, "a skills/ path -> dropped (self-exclusion)")
    found2, _ = O.parse_finding_lines("finding:fail-open @ skill/auto/foo:3")
    ok(found2 == [], "a skill/auto/* ref-like path -> dropped (self-exclusion)")


def test_parse_non_numeric_line_reject():
    ok(O.parse_finding_lines("finding:fail-open @ src/a.py:notanumber")[0] == [], "non-numeric line -> dropped")
    ok(O.parse_finding_lines("finding:fail-open @ src/a.py:")[0] == [], "empty line number -> dropped")
    ok(O.parse_finding_lines("finding:fail-open @ src/a.py:0")[0] == [], "line 0 -> dropped (1-indexed)")
    ok(O.parse_finding_lines("finding:fail-open @ src/a.py")[0] == [], "no ':' at all -> dropped")


def test_parse_unicode_digit_line_dropped_not_raised():
    # some Unicode digits pass str.isdigit() but RAISE in int() — e.g. superscript '²' (U+00B2) and
    # '³' (U+00B3). ASCII-digit validation (re.fullmatch [0-9]+) must DROP them, never raise (the
    # "never raises" contract). If the old isdigit()+int() path were live, this would throw.
    for bad in ("²", "²³", "1²"):   # '²', '²³', '1²' (mixed)
        found, dropped = O.parse_finding_lines(f"finding:fail-open @ src/a.py:{bad}")
        ok(found == [] and dropped == 1, f"Unicode-digit line {bad!r} dropped (not raised)")
    # a superscript that str.isdigit() accepts but int() rejects — prove the guard is what saves us
    ok("²".isdigit() is True, "sanity: '²'.isdigit() is True (why isdigit() was unsafe)")


def test_parse_last_at_split_with_hard_path():
    # design §4: the LAST ' @ ' splits fields (LEFT=tag, RIGHT=path:line); the path may contain
    # spaces, ':' and a bare '@'. Here the path has a bare '@', a ':' and a space; the split lands on
    # the LAST ' @ ' (the real separator), so tag='toctou-race' and the rest is the path:line chunk.
    txt = "finding:toctou-race @ src/we ird/a@b:c.py:99"
    found, dropped = O.parse_finding_lines(txt)
    ok(len(found) == 1 and dropped == 0, "a path with spaces/:/@ still parses (last ' @ ' anchor)")
    ok(found[0]["path"] == "src/we ird/a@b:c.py" and found[0]["line"] == 99,
       "path keeps its inner space/'@'/':'; line is the digits after the FINAL ':'")
    # if the PATH itself contains a ' @ ', the last ' @ ' is inside the path — the tag side then
    # carries the earlier ' @ ' and fails the allowlist, so the finding drops (documented boundary:
    # a space-flanked at-sign inside a path is the one shape the anchor cannot disambiguate).
    d = O.parse_finding_lines("finding:fail-open @ src/we @ ird.py:5")
    ok(d[0] == [] and d[1] == 1, "a ' @ ' INSIDE the path is the documented un-disambiguable case -> drop")


def test_parse_respects_max_rows_cap():
    lines = "\n".join(f"finding:fail-open @ src/f{i}.py:{i + 1}" for i in range(O.OUTCOME_MAX_ROWS + 10))
    found, dropped = O.parse_finding_lines(lines)
    ok(len(found) == O.OUTCOME_MAX_ROWS, "kept findings capped at OUTCOME_MAX_ROWS")
    ok(dropped == 10, "overflow findings counted as dropped")


def test_parse_empty_text():
    ok(O.parse_finding_lines("") == ([], 0), "empty text -> no findings, nothing dropped")
    ok(O.parse_finding_lines(None) == ([], 0), "None text -> no findings (no crash)")


# ---- resolve_and_hash: hunk validation + git-object read (injected run_git) --------------
def _fake_git(files):
    """A run_git stub: files = {path: full_file_text}. `cat-file blob` returns None for a missing/non-blob
    object (like a non-zero exit), else the file text. `ls-tree` lists the path iff it exists (mirrors
    real git), so file_line_hashes can positively confirm a delete vs a transient blob-read failure."""
    def run(argv):
        cmd = argv[2]                                   # ["-C", repo, <cmd>, ...]
        if cmd == "cat-file":                           # BOTH primitives read a blob by <tip>:<path>
            return files.get(argv[-1].split(":", 1)[1]) # via `cat-file blob` (never `show`)
        if cmd == "ls-tree":                            # "<mode> <type> <sha>\t<path>" iff it's in the tree
            path = argv[-1]
            assert path.startswith(":(literal)"), "probe must use literal pathspec magic"
            path = path[len(":(literal)"):]
            return (f"100644 blob deadbeef\t{path}\n") if path in files else ""
        return None
    return run


def test_resolve_line_inside_hunk():
    files = {"src/a.py": "line1\nreturn None\nline3\n"}
    h = O.resolve_and_hash("/repo", "tip", "src/a.py", 2, [(1, 3)], run_git=_fake_git(files))
    ok(h == O.line_hash("return None"), "a line inside a changed hunk resolves to its line_hash")


def test_resolve_line_outside_hunk_is_none():
    files = {"src/a.py": "line1\nreturn None\nline3\n"}
    h = O.resolve_and_hash("/repo", "tip", "src/a.py", 3, [(1, 1)], run_git=_fake_git(files))
    ok(h is None, "a wrong-but-resolvable line OUTSIDE every changed hunk -> None (can't mis-key)")
    ok(O.resolve_and_hash("/repo", "tip", "src/a.py", 2, [], run_git=_fake_git(files)) is None,
       "no changed hunks at all -> None (fail-closed)")


def test_resolve_missing_path_is_none():
    files = {"src/a.py": "x\ny\n"}
    ok(O.resolve_and_hash("/repo", "tip", "src/missing.py", 1, [(1, 2)], run_git=_fake_git(files)) is None,
       "missing git object -> None (drop)")
    ok(O.resolve_and_hash("/repo", "tip", "src/a.py", 99, [(1, 200)], run_git=_fake_git(files)) is None,
       "line past end-of-file -> None (drop)")


def test_resolve_empty_line_is_none():
    files = {"src/a.py": "code\n   \nmore\n"}   # line 2 is whitespace-only
    ok(O.resolve_and_hash("/repo", "tip", "src/a.py", 2, [(1, 3)], run_git=_fake_git(files)) is None,
       "a whitespace-only line normalizes to empty -> None (drop)")


def test_resolve_no_run_git_is_none():
    ok(O.resolve_and_hash("/repo", "tip", "src/a.py", 1, [(1, 1)], run_git=None) is None,
       "no run_git injected -> None (pure; the wiring supplies the subprocess callable)")


def test_resolve_reads_objects_not_worktree():
    # the run_git stub is the ONLY source of content — the resolver never touches a filesystem path.
    # Prove it: a path that would exist on disk (this test file) resolves via the stub's git text, not
    # its real disk content.
    files = {"tests/test_outcomes.py": "FAKE OBJECT LINE\n"}
    h = O.resolve_and_hash("/repo", "tip", "tests/test_outcomes.py", 1, [(1, 1)], run_git=_fake_git(files))
    ok(h == O.line_hash("FAKE OBJECT LINE"), "content comes from the git-object stub, not the working tree")


# ---- file_line_hashes: the CLOSE-phase present-set primitive (design §2) -----------------
def test_file_line_hashes_returns_all_line_hashes():
    files = {"src/a.py": "def f():\n    return None\n    x = 1\n"}
    got = O.file_line_hashes("/repo", "tip", "src/a.py", run_git=_fake_git(files))
    ok(O.line_hash("def f():") in got, "a present line's hash is in the set")
    ok(O.line_hash("return None") in got, "another present line's hash is in the set")
    ok(O.line_hash("gone forever") not in got, "an absent line's hash is NOT in the set")
    ok(len(got) == 3, "one hash per non-empty line (3 lines here)")


def test_file_line_hashes_skips_empty_lines():
    # trailing newline + a whitespace-only line -> empty normalizations must not enter the set.
    files = {"src/a.py": "code\n\n   \nmore\n"}
    got = O.file_line_hashes("/repo", "tip", "src/a.py", run_git=_fake_git(files))
    ok(got == {O.line_hash("code"), O.line_hash("more")}, "only the two non-empty lines contribute")


def test_file_line_hashes_missing_path_is_empty_set():
    # a DELETED/renamed file (missing git object) -> empty set -> every finding in it closes
    # (design-accepted whole-file-delete case §6).
    files = {"src/a.py": "x\n"}
    got = O.file_line_hashes("/repo", "tip", "src/missing.py", run_git=_fake_git(files))
    ok(got == set(), "missing object (deleted/renamed file) -> empty set (findings close)")


def test_file_line_hashes_no_run_git_is_none():
    ok(O.file_line_hashes("/repo", "tip", "src/a.py", run_git=None) is None,
       "no run_git injected -> None (can't determine -> fail-closed, don't close)")


def test_file_line_hashes_transient_read_failure_does_not_close():
    # core-audit HIGH: the blob read fails (None) but the path EXISTS at tip (ls-tree lists it) -> the
    # object was UNREADABLE (transient/mid-gc/lock), NOT deleted -> None, so the caller leaves the
    # finding OPEN rather than fabricating an applied_fixed.
    def stub(argv):
        if argv[2] == "cat-file":
            return None                                 # transient blob-read failure
        if argv[2] == "ls-tree":
            return "100644 blob abc123\tsrc/a.py\n"     # but the path IS present as a BLOB
        return None
    ok(O.file_line_hashes("/repo", "tip", "src/a.py", run_git=stub) is None,
       "blob read fails on an EXISTING blob -> None (transient, do NOT close)")


def test_file_line_hashes_git_unavailable_does_not_close():
    # both show and ls-tree fail (git wedged / timeout / OSError -> None) -> can't determine -> None.
    ok(O.file_line_hashes("/repo", "tip", "src/a.py", run_git=lambda _a: None) is None,
       "show+ls-tree both fail -> None (fail-closed on a transient error)")


def test_file_line_hashes_file_replaced_by_submodule_closes():
    # kilabz: a file replaced by a submodule/gitlink -> `git show` fails (not a blob) but ls-tree lists
    # it as type 'commit'. The file's LINES are genuinely gone -> close (empty set), not hang open forever.
    def stub(argv):
        if argv[2] == "cat-file":
            return None                                 # cat-file blob fails on a gitlink (not a blob)
        if argv[2] == "ls-tree":
            return "160000 commit abc123\tsrc/a.py\n"   # now a submodule (non-blob) at the path
        return None
    ok(O.file_line_hashes("/repo", "tip", "src/a.py", run_git=stub) == set(),
       "a file replaced by a submodule (non-blob) -> empty set (close, lines gone)")


def test_file_line_hashes_file_replaced_by_directory_closes():
    # kilabz r2: `git show <tip>:<dir>` SUCCEEDS on a tree (prints child names) — the old code would
    # hash those as lines. cat-file blob FAILS on a tree, and ls-tree reports type 'tree' -> close.
    def stub(argv):
        if argv[2] == "cat-file":
            return None                                 # cat-file blob fails on a tree (not a blob)
        if argv[2] == "ls-tree":
            return "040000 tree abc123\tsrc/a.py\n"     # a directory now lives at the old file path
        return None
    ok(O.file_line_hashes("/repo", "tip", "src/a.py", run_git=stub) == set(),
       "a file replaced by a directory (tree) -> empty set (close, lines gone — not the dir listing)")


def test_file_line_hashes_reads_objects_not_worktree():
    # same object-not-worktree guarantee as resolve_and_hash: content comes from the git stub.
    files = {"tests/test_outcomes.py": "FAKE OBJECT LINE\n"}
    got = O.file_line_hashes("/repo", "tip", "tests/test_outcomes.py", run_git=_fake_git(files))
    ok(got == {O.line_hash("FAKE OBJECT LINE")}, "content comes from the git-object stub, not disk")


def test_both_primitives_read_blobs_via_cat_file_never_show():
    # `git show <tip>:<path>` SUCCEEDS on a non-blob (prints a tree listing) — hashing that output
    # is the core-audit-B bug class. Pin the exact read argv for BOTH primitives.
    seen = []
    def spy(argv):
        seen.append(list(argv))
        return None
    O.resolve_and_hash("/repo", "tip", "src/a.py", 1, [(1, 1)], run_git=spy)
    O.file_line_hashes("/repo", "tip", "src/a.py", run_git=spy)
    reads = [a for a in seen if a[2] not in ("ls-tree",)]
    ok(len(reads) == 2 and all(a[2:4] == ["cat-file", "blob"] for a in reads),
       "both primitives read git objects via `cat-file blob`, never `show`")


def test_file_line_hashes_pathspec_magic_filename_probes_literal():
    # A filename starting with ':' is parsed as PATHSPEC MAGIC by a bare `ls-tree -- <path>` probe and
    # matches NOTHING (verified against real git 2026-07-06) — the probe would then read an EXISTING
    # file as "absent" and FABRICATE an applied_fixed close whenever the blob read blips. The probe
    # must wrap the path in `:(literal)`; with it, git returns the real entry.
    def stub(argv):
        if argv[2] == "cat-file":
            return None                                 # transient blob-read failure
        if argv[2] == "ls-tree":
            p = argv[-1]
            ok(p == ":(literal):weird.py", "probe wraps the exact filename in :(literal)")
            return "100644 blob abc123\t:weird.py\n"    # real git WITH literal magic: entry found
        return None
    ok(O.file_line_hashes("/repo", "tip", ":weird.py", run_git=stub) is None,
       "a ':'-named file that exists but is transiently unreadable stays OPEN (no fabricated close)")


def test_file_line_hashes_whitespace_filename_stays_open():
    # Plain regression pin for a whitespace-NAMED file. (An oracle claim that `.strip()` could
    # collapse this entry to "" and fabricate a close was REFUTED — the line always carries
    # "<mode> <type> <sha>" so it parses to type 'blob' under strip OR rstrip; kilabz's re-review
    # caught the original test asserting a fix for a non-bug. Kept as a behavior pin: an existing
    # whitespace-named blob with a transient read failure must stay OPEN.)
    def stub(argv):
        if argv[2] == "cat-file":
            return None                                 # transient blob-read failure
        if argv[2] == "ls-tree":
            return "100644 blob abc123\t  \n"           # the file's NAME is two spaces
        return None
    ok(O.file_line_hashes("/repo", "tip", "  ", run_git=stub) is None,
       "a whitespace-named existing blob with a transient read failure stays OPEN")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
