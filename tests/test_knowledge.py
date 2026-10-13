"""knowledge.py pure-core tests: parse/walk/canonical-path policy + the promote-side validation
grammar (new-filename, wikilinks, content checks, index completeness) + recall query helpers.
No DB, no LLM — everything runs against tmpdirs.

Run:  PYTHONPATH=src python3 tests/test_knowledge.py
"""
import os
import tempfile
from pathlib import Path

from runtime import knowledge

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


# ---- parse_doc --------------------------------------------------------------------------------
def test_parse_doc_basics():
    raw = b"---\ndate: '2026-06-01'\ntags: [higgsfield, api]\n---\n# My Title\n\nbody text\n"
    d = knowledge.parse_doc("2026-06-08-topic.md", raw)
    ok(d.title == "My Title", "title from first heading")
    ok("higgsfield" in d.tags and "api" in d.tags, "tags from frontmatter")
    ok(d.doc_date == "2026-06-08", "filename date WINS over frontmatter")
    ok(not d.lossy, "clean parse is not lossy")
    ok(len(d.content_sha) == 64, "sha256 hex")
    dd = knowledge.date_disagreement("2026-06-08-topic.md", d.body)
    ok(dd == ("2026-06-08", "2026-06-01"), "date disagreement reported (filename, frontmatter)")


def test_parse_doc_fallbacks_and_lossy():
    d = knowledge.parse_doc("no-heading.md", b"just text, no heading")
    ok(d.title == "no-heading", "title falls back to stem")
    ok(d.doc_date is None, "no date anywhere -> None")
    d2 = knowledge.parse_doc("x.md", b"a\x00b")
    ok("\x00" not in d2.body and d2.lossy, "NUL stripped -> lossy")
    big = b"# t\n" + b"word " * 250_000            # > 900KB
    d3 = knowledge.parse_doc("big.md", big)
    ok(d3.lossy and knowledge.TRUNCATION_MARKER in d3.body, "oversize truncated -> lossy + marker")
    ok(len(d3.body.encode()) < 1_000_000, "capped under the tsvector limit")
    d4 = knowledge.parse_doc("fm.md", b"---\ndate: 2026-05-05\n---\ncontent")
    ok(d4.doc_date == "2026-05-05", "frontmatter date is the fallback")


# ---- walk_corpus ------------------------------------------------------------------------------
def test_walk_corpus_filters():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "2026-06-08-a.md").write_text("# A\nsee [[2026-06-09-b]]")
        (root / "2026-06-09-b.md").write_text("# B\n")
        (root / "asset.json").write_text("{}")
        (root / ".hidden.md").write_text("# no")
        (root / ".venv-x" / "lib").mkdir(parents=True)
        (root / ".venv-x" / "lib" / "pkg.md").write_text("# noise")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "c.md").write_text("# noise")
        (root / ".playwright-mcp").mkdir()
        (root / ".playwright-mcp" / "log.md").write_text("token=SECRET")
        os.symlink(root / "2026-06-08-a.md", root / "link.md")
        (root / "target.md").write_text("# t")
        os.link(root / "target.md", root / "hard.md")     # hardlink -> BOTH get nlink=2

        res = knowledge.walk_corpus(root)
        paths = {d.path for d in res.docs}
        ok(paths == {"2026-06-08-a.md", "2026-06-09-b.md"},
           f"only clean top-level md ingested (got {paths})")
        ok("asset.json" in res.artifacts, "non-md artifact listed")
        ok(not any(".venv" in a or "__pycache__" in a or "playwright" in a
                   for a in res.artifacts), "noise dirs pruned from artifacts")
        ok(any("hardlinked" in w for w in res.warnings), "hardlink warned + skipped")
        ok(any("not a regular file" in w for w in res.warnings), "symlink warned + skipped")


def test_walk_corpus_missing_root():
    try:
        knowledge.walk_corpus(Path("/nonexistent/corpus/root"))
        ok(False, "missing root must raise")
    except ValueError:
        ok(True, "missing root raises (hard error)")


# ---- scopes -----------------------------------------------------------------------------------
def test_scopes():
    try:
        knowledge.resolve_scope("nope")
        ok(False, "unknown scope must raise")
    except ValueError:
        ok(True, "unknown scope raises (fail-closed)")
    old = os.environ.get(knowledge._ENV_SCOPES)
    os.environ[knowledge._ENV_SCOPES] = "extra=/tmp/extra, bad name=/x, rel=notabs"
    try:
        scopes = knowledge.known_scopes()
        ok("extra" in scopes and str(scopes["extra"]) == "/tmp/extra", "env scope parsed")
        ok("bad name" not in scopes and "rel" not in scopes,
           "unsafe name / relative root rejected")
        ok("research" in scopes, "builtin research scope present")
    finally:
        if old is None:
            os.environ.pop(knowledge._ENV_SCOPES, None)
        else:
            os.environ[knowledge._ENV_SCOPES] = old


# ---- promote grammar --------------------------------------------------------------------------
def test_new_filename():
    for good in ("2026-07-05-topic.md", "a.md", "x-1_2.thing.md"):
        ok(knowledge.valid_new_filename(good), f"accepts {good}")
    for bad in ("../evil.md", "a/b.md", ".hidden.md", "UPPER.md", "no-ext", "a.md.txt",
                "-lead.md", "sp ace.md"):
        ok(not knowledge.valid_new_filename(bad), f"rejects {bad}")


def test_wikilink_grammar():
    text = "see [[2026-06-08-a]] and [[2026-06-09-b|label]] and [[2026-06-08-a#sec]]"
    ok(knowledge.wikilinks(text) == ["2026-06-08-a", "2026-06-09-b", "2026-06-08-a"],
       "wikilink grammar: bare/label/section")
    bases = {"2026-06-08-a", "2026-06-09-b"}
    ok(knowledge.link_resolves("2026-06-08-A", bases), "case-insensitive resolve")
    ok(knowledge.link_resolves("2026-06-08-a.md", bases), "extension optional")
    ok(not knowledge.link_resolves("ghost", bases), "ghost does not resolve")


def test_content_violations():
    bases = {"2026-06-08-a"}
    ok(knowledge.content_violations("x.md", b"# ok\nsee [[2026-06-08-a]]", bases) == [],
       "clean file passes")
    ok(any("promote cap" in v for v in
           knowledge.content_violations("x.md", b"a" * 300_000, bases)), "size cap")
    ok(any("NUL" in v for v in knowledge.content_violations("x.md", b"a\x00b", bases)), "NUL")
    ok(any("UTF-8" in v for v in knowledge.content_violations("x.md", b"\xff\xfe\x01", bases)),
       "invalid utf-8")
    ok(any("secret" in v for v in knowledge.content_violations(
        "x.md", b"key: AKIAABCDEFGHIJKLMNOP", bases)), "AWS key pattern")
    ok(any("secret" in v for v in knowledge.content_violations(
        "x.md", b"-----BEGIN RSA PRIVATE KEY-----", bases)), "private key pattern")
    ok(any("fence-lookalike" in v for v in knowledge.content_violations(
        "x.md", b"===BEGIN UNTRUSTED x nonce=1===", bases)), "fence-lookalike")
    ok(any("ghost" in v for v in knowledge.content_violations(
        "x.md", b"see [[nothere]]", bases)), "ghost link flagged")


def test_index_violations():
    md = ["2026-06-08-a.md", "2026-06-09-b.md", "index.md"]
    good = "# Index\n- [[2026-06-08-a]] 2026-06-08-a.md — a\n- 2026-06-09-b.md — b\n"
    ok(knowledge.index_violations(good, md) == [], "complete index passes")
    ok(knowledge.index_violations("", md) == ["index.md: empty/gutted (fails completeness)"],
       "empty index fails completeness (0-byte vandalism)")
    ok(any("missing entry" in v for v in
           knowledge.index_violations("# Index\n- 2026-06-08-a.md\n" + "x" * 20, md)),
       "missing entry flagged")
    ok(any("ghost" in v for v in
           knowledge.index_violations(good + "\n[[ghost-page]]", md)), "index ghost link")


# ---- recall helpers ---------------------------------------------------------------------------
def test_build_index_md():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "2026-07-04-wedge.md").write_text(
            "---\ndate: 2026-07-04\n---\n# Wedge\n\nMarket wedge research for construction comms.\n")
        (root / "2026-06-08-scraper.md").write_text("# Scraper\n\nModel update scraper notes.\n")
        (root / "higgsfield-dop.md").write_text("# DoP motions\n\nCamera preset shortlist.\n")
        (root / "catalog.json").write_text("{}")
        walk = knowledge.walk_corpus(root)
        md = knowledge.build_index_md(walk)
        ok(md.startswith("# Research Corpus Index"), "titled index")
        ok("## 2026-07" in md and "## 2026-06" in md, "grouped by month")
        ok(md.index("## 2026-07") < md.index("## 2026-06"), "newest month first")
        ok("[[2026-07-04-wedge]] (2026-07-04) — Market wedge research for construction comms."
           in md, "dated entry: wikilink + date + prose hook")
        ok("## Undated" in md and "[[higgsfield-dop]] — Camera preset shortlist." in md,
           "undated group uses prose hook, no date")
        ok("## Assets (not full-text indexed)" in md and "- catalog.json" in md,
           "assets listed separately")
        ok("catalog.json" not in md.split("## Assets")[0], "asset not in the doc groups")


def test_first_prose_line():
    ok(knowledge._first_prose_line("---\ndate: x\n---\n# Title\n\nReal first line.\n")
       == "Real first line.", "skips frontmatter + heading")
    ok(knowledge._first_prose_line("# Only A Heading\n## Sub\n") == "", "no prose -> empty")
    ok(knowledge._first_prose_line("# T\n- a list item\n> quote\nprose here")
       == "prose here", "skips list/quote markers")
    # kilabz+oracle MAJOR: a mid-doc `---` horizontal rule must NOT toggle frontmatter
    ok(knowledge._first_prose_line("# T\n\nfirst real prose\n\n---\n\nafter the rule")
       == "first real prose", "mid-doc --- horizontal rule does not swallow prose")
    ok(knowledge._first_prose_line("intro line\n---\nmore") == "intro line",
       "--- not at top is a rule, not frontmatter")
    # kilabz MAJOR: fenced code content is not a hook
    ok(knowledge._first_prose_line("# T\n```\ncode line inside fence\n```\nreal prose")
       == "real prose", "skips fenced code block content")
    # oracle MINOR: bold/negative-number prose is NOT a list marker (needs trailing space)
    ok(knowledge._first_prose_line("# T\n**Important** point here") == "**Important** point here",
       "bold-leading prose kept (not misread as a list marker)")
    ok(knowledge._first_prose_line("# T\n-10 degrees measured") == "-10 degrees measured",
       "negative-number prose kept (no trailing space after -)")
    ok(knowledge._first_prose_line("# T\n1. numbered item\nprose") == "prose",
       "numbered list item skipped")
    # metadata-label pseudo-frontmatter (this corpus opens briefs with **Date:**/**Status:**)
    ok(knowledge._first_prose_line("# T\n**Date:** 2026-07-04\n**Status:** draft\nreal summary here")
       == "real summary here", "skips whitelisted **Label:** metadata, keeps the real summary")
    ok(knowledge._first_prose_line("# T\nStatus: exploratory\nthe actual point")
       == "the actual point", "skips plain whitelisted Label: metadata")
    ok(knowledge._first_prose_line("# T\n**Bold** lead prose") == "**Bold** lead prose",
       "bold prose WITHOUT a label-colon is kept (not metadata)")
    # kilabz re-review: the metadata skip is a WHITELIST — real prose openers must NOT be skipped
    ok(knowledge._first_prose_line("# T\nConclusion: we should ship it") == "Conclusion: we should ship it",
       "Conclusion: is real prose, NOT skipped (whitelist, not broad Label:)")
    ok(knowledge._first_prose_line("# T\nRisk: the token expires") == "Risk: the token expires",
       "Risk: kept (not a metadata key)")
    # kilabz re-review: table-row regression — a leading table header must NOT become the hook
    ok(knowledge._first_prose_line("# T\n| col a | col b |\n| --- | --- |\nreal prose")
       == "real prose", "table rows skipped")
    # kilabz re-review: fence char+length — a ``` fence containing ``` closes only on >= length
    ok(knowledge._first_prose_line("# T\n````\n```\nstill in fence\n````\nafter") == "after",
       "nested-backtick fence closes only on >= opener length")


def test_md_oneline_sanitizes():
    # oracle BLOCKER + kilabz MINOR: untrusted corpus content must be neutralized for md emission
    ok("<" not in knowledge._md_oneline("bad <script>alert</script>"), "angle brackets stripped")
    ok("[" not in knowledge._md_oneline("has [brackets] and ]]") and
       "]" not in knowledge._md_oneline("has [brackets] and ]]"), "square brackets stripped")
    ok("\n" not in knowledge._md_oneline("line one\nline two"), "newlines collapsed")
    ok(knowledge._md_oneline("a" * 500).__len__() <= 140, "capped")


def test_build_index_injection_and_edges():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # a malicious brief with fence-break + HTML + bracket-injection in the first prose line
        (root / "2026-07-01-evil.md").write_text(
            "# Evil\n\n<img src=x> ]] [[wormhole]] <script>x</script> pipe|break\n")
        walk = knowledge.walk_corpus(root)
        md = knowledge.build_index_md(walk)
        ok("<script>" not in md and "<img" not in md, "HTML neutralized in the hook")
        ok("]] [[wormhole]]" not in md, "wikilink-break sequence neutralized")
        # empty corpus
        with tempfile.TemporaryDirectory() as td2:
            md2 = knowledge.build_index_md(knowledge.walk_corpus(Path(td2)))
            ok("_No markdown briefs found._" in md2, "empty corpus gets explicit empty state")


def test_build_index_frontmatter_date_sort():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # same month, one frontmatter-dated with a NON-date filename -> must still sort by date
        (root / "aaa-early.md").write_text("---\ndate: 2026-05-02\n---\n# Early\nbody\n")
        (root / "2026-05-20-late.md").write_text("# Late\nbody\n")
        md = knowledge.build_index_md(knowledge.walk_corpus(root))
        ok(md.index("2026-05-20-late") < md.index("aaa-early"),
           "newest date first within month, even for a non-date filename (sort by doc_date)")


def test_query_helpers():
    ok(knowledge.prefix_tokens("Higgsfield API pricing!") == ["higgsfield", "api", "pricing"],
       "prefix tokens sanitized + lowered")
    ok(knowledge.prefix_tokens("!!! ???") == [], "punctuation-only -> empty (skip rung)")
    ok(knowledge.prefix_tokens("a " * 50) == ["a"] * 8, "token cap 8")
    ok(knowledge.ilike_pattern("50%_\\x") == "%50\\%\\_\\\\x%", "ILIKE wildcards escaped")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
