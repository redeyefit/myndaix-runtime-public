"""Pure, DB-free core for the outcomes-ledger rung (the per-finding OUTCOME LABEL) — v0.3.

Kept separate from the ledger/play-review wiring so the finding IDENTITY (path-scoped line-hash),
the `finding:` line parser, and the hunk-validation resolve are unit-testable WITHOUT a DB or git
(mirrors runtime.capture, the sibling rung's pure layer). NO LLM decides identity or validity here:
the rule_tag is an ALLOWLISTED, version-controlled taxonomy (shared with capture, single source of
truth), the line CONTENT is read from git OBJECTS at the reviewed tip, and every malformed finding
is DROPPED fail-closed (never raised, never a silent mis-key).

DESIGN: docs/outcomes-ledger-design.md (v0.3 — cross-family reviewed). The append-only state machine
(record_findings close+open, human_dismiss, expire_open) lives in ledger.postgres_store; the git-
object read is done via an injected `run_git` callable so this file stays pure/deterministic.

Identity rules (design §3, the v0.1 CRIT fix):
  * line_hash    = sha256 of the WHITESPACE-NORMALIZED line CONTENT — never the line NUMBER, so the
                   identity survives diff shifts. Normalize = collapse internal whitespace runs to a
                   single space + strip ends. Two identical NORMALIZED lines in the SAME file
                   legitimately collide (accepted, like SonarQube).
  * finding_key  = sha256(repo_id \0 rule_tag \0 path \0 line_hash) — PATH IS IN THE KEY: identical
                   normalized lines in DIFFERENT files must NOT collide, and a crafted diff line
                   cannot mint a legit finding's key without touching that finding's own file.
"""
from __future__ import annotations

import hashlib
import re
from typing import Callable, Optional

from runtime.capture import RULE_TAG_TAXONOMY, is_allowed_tag  # single source of truth (S3)

__all__ = [
    "OUTCOME_MAX_ROWS", "RULE_TAG_TAXONOMY", "is_allowed_tag",
    "normalize_line", "line_hash", "finding_key",
    "parse_finding_lines", "resolve_and_hash", "file_line_hashes",
]

# Per-review row cap (design §4). The honest spam bound is tags × LINES in the diff (not × files),
# so an explicit cap replaces the v0.1 hand-wave. DEFAULT only — the wiring (PR-B) reads
# $OUTCOME_MAX_ROWS and passes the live value, so a recalibration is a config change, never code.
OUTCOME_MAX_ROWS = 50

# ALL C0 (incl. \t \n \r) + DEL — the strictest raw-line reject, checked on the RAW value BEFORE any
# strip (capture round-2 lesson: a leading/trailing control char must not be normalized away first).
_RAW_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_WS_RUN = re.compile(r"\s+")                 # any whitespace run -> a single space (line-hash norm)

# The `finding:` wire line, emitted one-per-finding by BOTH reviewers (design §4):
#     finding:<tag> @ <path>:<line>
# We DON'T anchor the tag in the regex — a constrained tag can be extracted by splitting, but a valid
# `finding:` tag can't carry a ` @ `, so the WHOLE line (after `finding:`) is captured raw and split
# on the LAST ` @ ` in python (design §4: "the LAST ` @ ` on the line splits fields; <path> may
# contain spaces/`:`/`@`"). LEFT of the last ` @ ` is the tag; RIGHT is `<path>:<line>` with the line
# the digits after the FINAL `:`. Capturing raw (not tokenizing in-regex) is what lets the ctrl-char
# reject run on the RAW value before any strip. The tag validity + numeric line are checked below.
_FINDING_LINE = re.compile(r"(?mi)^[ \t]*finding:(.+)$")
_SEP = " @ "                                 # the space-flanked at-sign field separator


def normalize_line(content: str) -> str:
    """The canonical whitespace normalization behind line_hash: collapse EVERY internal whitespace
    run (spaces, tabs, form-feeds, etc.) to a single ASCII space, then strip both ends. This makes
    the hash robust to re-indentation / trailing-whitespace churn while keeping token boundaries, so
    two lines differing only in whitespace share an identity (SonarQube's `line_hash` borrow)."""
    return _WS_RUN.sub(" ", content or "").strip()


def line_hash(content: str) -> str:
    """sha256 of the whitespace-normalized line CONTENT (never the line NUMBER). Two identical
    normalized lines — in the same file, or in the same review — hash the same; that intra-file
    aliasing is ACCEPTED (SonarQube accepts it too). Cross-FILE aliasing is closed by finding_key,
    which folds the path in. Determinism: same content -> same 64-hex digest, always."""
    return hashlib.sha256(normalize_line(content).encode()).hexdigest()


def finding_key(repo_id: str, rule_tag: str, path: str, line_hash_hex: str) -> str:
    """The stable per-finding identity: sha256(repo_id \0 rule_tag \0 path \0 line_hash). PATH IS IN
    THE KEY (design §3 CRIT fix) — two identical normalized lines in DIFFERENT files produce DIFFERENT
    keys, so their outcome histories can never merge, and an attacker can't mint a legit finding's key
    by planting the same line in an unrelated file. NUL-separated so no field-boundary can collide
    (e.g. ('a','b') vs ('a\\0b',''))."""
    material = "\x00".join((repo_id, rule_tag, path, line_hash_hex))
    return hashlib.sha256(material.encode()).hexdigest()


def _excluded_path(path: str) -> bool:
    """True iff `path` is one the outcomes rung must NOT key a finding on: the skills corpus itself
    (`skills/**`) or a ref-shaped auto-proposal path (`skill/auto/*`). Mirrors the sibling rung's
    self-exclusion — a finding raised on an auto-proposed skill is not review signal about the code."""
    return path.startswith("skills/") or path.startswith("skill/auto/")


def parse_finding_lines(review_text: str, allowed_tags=RULE_TAG_TAXONOMY) -> tuple[list[dict], int]:
    """Extract validated `finding:<tag> @ <path>:<line>` findings from one reviewer's text.

    Returns (findings, dropped) — a list of {"tag", "path", "line"} dicts (line is an int) plus the
    count of malformed/over-cap findings DROPPED. FAIL-CLOSED, NEVER raises: a bad finding is dropped
    and counted, never surfaced as a mis-keyed row. Validation, all on the RAW line before any strip:
      * the whole RAW line is rejected if it carries ANY C0/DEL control char (incl. tab) OUTSIDE the
        single tab/space the regex allows as a separator — checked on the raw match groups so an
        injected control char can't survive into a path;
      * <tag> must be on the ALLOWLISTED taxonomy (`allowed_tags`) — an off-list tag is dropped;
      * the LAST ` @ ` splits <path> from <line> (path may contain spaces/`:`/`@`); <line> = the
        digits after the FINAL `:` in the path-and-line remainder, and must be a positive integer;
      * a path under `skills/` or a `skill/auto/*` ref is excluded (self-exclusion).
    Bounded at OUTCOME_MAX_ROWS: once that many VALID findings are kept, every further finding
    (valid or not) counts toward `dropped` — the honest per-line spam bound (design §4)."""
    findings: list[dict] = []
    dropped = 0
    for m in _FINDING_LINE.finditer(review_text or ""):
        raw = m.group(1)                        # everything after `finding:` on the line
        # ctrl-char reject on the RAW captured value BEFORE any strip: a tab/newline/DEL injected
        # anywhere (tag, path, or around the separator) drops the WHOLE finding (capture round-2
        # lesson — check the raw value so a leading/trailing control char can't be normalized away).
        if _RAW_CTRL.search(raw):
            dropped += 1
            continue
        # the LAST ` @ ` splits fields: LEFT = <tag>, RIGHT = <path>:<line>. A missing separator ->
        # not a well-formed finding line -> drop.
        if _SEP not in raw:
            dropped += 1
            continue
        tag_part, chunk = raw.rsplit(_SEP, 1)
        tag = tag_part.strip().lower()
        if not is_allowed_tag(tag) or tag not in allowed_tags:   # off-list -> drop (S3, fail-closed)
            dropped += 1
            continue
        chunk = chunk.strip()
        colon = chunk.rfind(":")                # <line> = the digits after the FINAL ':'
        if colon <= 0:                          # no ':' or a leading ':' -> no line number -> drop
            dropped += 1
            continue
        path, line_s = chunk[:colon].strip(), chunk[colon + 1:].strip()
        # ASCII-digit validation (NOT str.isdigit()): some Unicode digits — superscripts, e.g. '²' —
        # pass isdigit() but raise on int(), which would break the "never raises" contract. Require
        # an all-[0-9] run so int() below is always safe.
        if not path or not re.fullmatch(r"[0-9]+", line_s):   # non-ASCII-numeric / empty -> drop
            dropped += 1
            continue
        line = int(line_s)
        if line < 1 or _excluded_path(path):    # 0/negative line, or self-exclusion -> drop
            dropped += 1
            continue
        if len(findings) >= OUTCOME_MAX_ROWS:   # per-run cap: overflow is counted + dropped
            dropped += 1
            continue
        findings.append({"tag": tag, "path": path, "line": line})
    return findings, dropped


def _line_in_hunks(line: int, hunks: list[tuple[int, int]]) -> bool:
    """True iff `line` falls INSIDE one of this diff's changed hunks for the finding's path. `hunks`
    is a list of (start, count) 1-indexed inclusive ranges (the `+start,count` side of a diff hunk
    header for that path). An empty hunk list -> False (fail-closed: a finding on a path with no
    changed hunk in this diff can't be validated, so it's dropped)."""
    for start, count in hunks:
        if count > 0 and start <= line <= start + count - 1:
            return True
    return False


def resolve_and_hash(repo_path: str, tip_sha: str, path: str, line: int,
                     changed_hunks: list[tuple[int, int]],
                     run_git: Optional[Callable[[list[str]], Optional[str]]] = None) -> Optional[str]:
    """Read the flagged line from git OBJECTS at `tip_sha` (`git cat-file blob <tip>:<path>`,
    1-indexed line `line`), validate it falls INSIDE one of this diff's `changed_hunks` for that path, and return its
    line_hash — or None (DROP) on any miss. Reads git OBJECTS, NEVER the working tree, so a dirty
    checkout or a post-review edit can't change the hashed content (design §3/§6).

    Fail-closed drops (all -> None): the line is outside every changed hunk (a wrong-but-resolvable
    line number must not silently key a finding to unrelated code); the path/object is missing or the
    file has fewer than `line` lines; the resolved line is empty after normalization. `run_git` is an
    injected callable(argv) -> stdout|None (None on a non-zero git exit / missing object) so this
    stays pure + unit-testable; the wiring passes a subprocess-backed one."""
    if run_git is None:
        return None
    if not _line_in_hunks(line, changed_hunks):     # line ∉ a changed hunk -> can't validate -> drop
        return None
    # `cat-file blob` (NOT `git show`) for the same reason as file_line_hashes: show SUCCEEDS on a
    # non-blob (prints a tree listing / commit) and we'd hash garbage as the "flagged line". The
    # _line_in_hunks gate makes that edge near-unreachable here (a dir path never has line hunks),
    # but the RECORD primitive must match the CLOSE primitive's read discipline exactly.
    out = run_git(["-C", repo_path, "cat-file", "blob", f"{tip_sha}:{path}"])
    if out is None:                                 # missing/non-blob object / non-zero git exit -> drop
        return None
    lines = out.split("\n")
    if line < 1 or line > len(lines):               # file has fewer lines than claimed -> drop
        return None
    content = lines[line - 1]
    if not normalize_line(content):                 # empty / whitespace-only line -> drop
        return None
    return line_hash(content)


def file_line_hashes(repo_path: str, tip_sha: str, path: str,
                     run_git: Optional[Callable[[list[str]], Optional[str]]] = None
                     ) -> Optional[set[str]]:
    """The CLOSE-phase primitive (design §2): the SET of line_hashes present in `path` at `tip_sha`,
    read from git OBJECTS (`git cat-file blob <tip>:<path>`), NEVER the working tree — so a dirty
    checkout or a post-review edit can't change the answer (same discipline as resolve_and_hash).

    A stored finding closes (applied_fixed) iff its line_hash is NOT in this set — i.e. its exact
    (whitespace-normalized) line content no longer appears ANYWHERE in the file at tip. This is the
    design's real CLOSE contract; "the reviewer didn't re-flag it" is NOT (a PASS review, or a review
    of an unrelated line in the same file, would false-close every open finding there).

    Empty/whitespace-only lines are skipped (they normalize to '' and would collide across files;
    resolve_and_hash never keys a finding on one, so none can be OPEN).

    THREE-STATE return (core-audit HIGH — a transient git error must NOT fabricate applied_fixed):
      * a populated/empty set = git POSITIVELY answered. A genuinely-absent object (deleted/renamed at
        tip, CONFIRMED via a successful ls-tree) -> the EMPTY set, so every finding in it closes (the
        design-accepted whole-file-delete/rename case §6).
      * None = presence could NOT be determined (a blob-read failure that is NOT a confirmed delete:
        timeout, OSError, a momentary index.lock / mid-gc, or the blob exists but was unreadable). The
        caller MUST then leave the finding OPEN — collapsing a transient failure into 'absent' would
        insert a false applied_fixed and permanently poison the outcomes ground-truth that gates autonomy.
    `run_git` is the injected callable(argv) -> stdout|None so this stays pure + unit-testable."""
    if run_git is None:
        return None                                 # no git injected -> can't determine -> don't close
    # `cat-file blob` (NOT `git show`): show SUCCEEDS on a non-blob — for a file replaced by a DIRECTORY
    # it prints the tree's child NAMES, which we'd then hash as "lines" and could false-keep a finding
    # open if a child name collides (kilabz). cat-file blob fails on any non-blob, so the content path
    # below only runs for a REAL file blob; every non-blob falls to the ls-tree type probe.
    out = run_git(["-C", repo_path, "cat-file", "blob", f"{tip_sha}:{path}"])
    if out is not None:
        hashes: set[str] = set()
        for content in out.split("\n"):
            if normalize_line(content):             # skip empty/ws-only (never a keyed finding line)
                hashes.add(line_hash(content))
        return hashes
    # blob read failed. Distinguish a GENUINE delete/replacement (close) from a TRANSIENT error (leave
    # open) by POSITIVELY probing the tree. NOT --name-only: we need the object TYPE (cat-file blob on a
    # blob-that-timed-out and on a file-replaced-by-a-submodule/dir BOTH fail, but only the first is
    # transient — the second means the file's lines are genuinely gone). ls-tree line =
    # "<mode> <type> <sha>\t<path>".
    # `:(literal)` pathspec magic: a bare path is a PATHSPEC — a filename starting with ':' would be
    # parsed as magic and match NOTHING (empirically: `ls-tree HEAD -- ':weird.py'` returns empty for
    # an EXISTING file), which reads as "absent" below and would FABRICATE an applied_fixed close on
    # a transiently-unreadable blob. literal magic probes the exact name.
    # Do NOT add `-d` here (a reviewer proposed it to "stop recursion into a dir at the path" —
    # REFUTED 2026-07-06: bare ls-tree on a dir path returns the tree ENTRY, not its children, so
    # there is no recursion to stop; and `-d` SUPPRESSES blob entries entirely, so an EXISTING file
    # would probe as absent -> set() -> fabricated close on every transient blob-read failure).
    listed = run_git(["-C", repo_path, "ls-tree", tip_sha, "--", f":(literal){path}"])
    if listed is None:
        return None                                 # ls-tree also failed -> transient/unknown -> don't close
    # rstrip newlines ONLY (not .strip()): the only trailing bytes we ever mean to trim are the line
    # terminator. (A reviewer claimed .strip() could collapse a whitespace-NAMED file's entry to ""
    # and fabricate a close — REFUTED 2026-07-06: the entry line always carries "<mode> <type> <sha>"
    # so it survives strip() non-empty and lands on the blob branch either way. rstrip kept as the
    # precise idiom; it also preserves the path field verbatim for any future parsing.)
    listed = listed.rstrip("\r\n")
    if listed == "":
        return set()                                # tree read OK + path absent -> genuinely deleted -> close (§6)
    fields = listed.split()
    obj_type = fields[1] if len(fields) >= 2 else ""
    if obj_type == "blob":
        return None                                 # exists as a FILE but blob read failed -> unreadable/transient -> don't close
    return set()                                    # non-blob at the path (commit=submodule/gitlink, tree=dir):
                                                    # the file's lines are genuinely gone -> close (kilabz r1)
