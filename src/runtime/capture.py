"""Pure, DB-free core for the auto-capture rung (the proposer) — v0.4.

Kept separate from the ledger/controller/proposer so the recurrence keying, slug/path isolation,
and deterministic drafting are unit-testable WITHOUT a DB or gh (mirrors runtime.skillmatch). NO
LLM decides recurrence or safety here: the recurrence class is a reviewer-emitted, ALLOWLISTED
`rule:<tag>` (S3), the skill body is rendered from a FIXED template over structured fields (S4),
and the proposal path is asserted to be EXACTLY skills/<slug>/SKILL.md (S1).

DESIGN: docs/auto-capture-design.md (v0.4 — REQUIRED SAFEGUARDS S1/S3/S4 live here). The DB state
machine (S6) lives in ledger.postgres_store; the gh/git side effects (S5/S7) live in the proposer.
"""
from __future__ import annotations

import hashlib
import re

from runtime import skillmatch

__all__ = [
    "RULE_TAG_TAXONOMY", "is_allowed_tag", "slug", "fingerprint", "is_hex_sha", "normalize_sha",
    "path_to_glob", "candidate_glob", "recurrence_ready", "reready_threshold",
    "skill_branch", "skill_path", "assert_only_skill_path",
    "sanitize_field", "render_skill_md", "draft_hash", "DEFAULTS",
    "parse_rule_tags", "agreed_tags", "pick_glob",
    "STUB_MARKER", "is_unauthored_stub",
]

# ---- feature-flagged defaults (v0.4 — the proposer reads env + passes these in) -----------
# Thresholds are DEFAULTS only; the proposer/controller reads $CAPTURE_* and passes the live value,
# so a cross-family review of the S3 recalibration is a config change, never a code rewrite.
DEFAULTS = {
    "MIN_RECUR": 3,        # distinct commit SHAs carrying the tag before a class is `ready`
    "MIN_EVENTS": 2,       # distinct review/push events (temporal independence — anti single-push)
    "MIN_AUTHORS": 1,      # distinct authors — per-repo dial; default 1 (solo-founder reality, v0.4)
    "MAX_OPEN": 3,         # at most N open auto-PRs at once (S8 anti-fatigue)
    "TTL_DAYS": 14,        # auto-close an un-acted auto-PR after N days (S8 anti-wedge)
    "REPROPOSE_MULT": 2,   # a declined class re-fires only at MIN_RECUR * (this ** decline_count)
}

# ---- S3: rule_tag is an ALLOWLISTED, version-controlled taxonomy (NOT free-form) ----------
# An off-list tag forms NO candidate (fail-closed). This set IS the version control — it tracks the
# recurring finding-CLASSES our reviewers flag (mirrors the global "Common Bugs to Prevent" list).
# Add a tag here (a reviewed PR) before reviewers may use it; never derive a path/slug from raw text.
RULE_TAG_TAXONOMY = frozenset({
    "fail-open",                 # a security/merge gate that defaults OPEN on the unhandled path
    "missing-file-lock",         # shared file/state mutated without flock or atomic mv
    "unsanitized-injection",     # external/LLM text into a prompt or shell without sanitizing
    "silent-error-suppression",  # swallowed error (2>/dev/null || true, empty catch, bare print)
    "missing-scoping",           # multi-tenant/multi-agent data not scoped (should fail-closed)
    "python-in-bash-interp",     # variables interpolated into python3 -c instead of sys.argv
    "macos-incompat",            # timeout/setsid/flock/date -jf assumed present on macOS
    "shared-marker-contention",  # one file used as state for multiple consumers
    "toctou-race",               # check-then-use race on fs/db state
    "migration-fail-open",       # in-place edit to a shipped migration (see migration-append-only)
    "swiftdata-thread-safety",   # SwiftData @Model touched off the main thread
    "swiftui-concurrency",       # Task{} in onAppear / missing @MainActor / dropped .task
})

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
# reserved device/path names that must never become a directory segment
_RESERVED = frozenset({"con", "prn", "aux", "nul", "com1", "lpt1", "skills", "auto", "."})
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")  # the wire form a reviewer may emit
_RAW_CTRL = re.compile(r"[\x00-\x1f\x7f]")           # ALL C0 (incl. \t \n \r) + DEL — strictest


def is_allowed_tag(rule_tag: str) -> bool:
    """True iff `rule_tag` is well-formed AND on the version-controlled taxonomy (S3, fail-closed).
    The wire-form check rejects junk before the membership check so a malformed tag can never match
    via normalization surprises."""
    t = (rule_tag or "").strip().lower()
    return bool(_TAG_RE.match(t)) and t in RULE_TAG_TAXONOMY


def slug(rule_tag: str) -> str | None:
    """The directory/branch slug for a tag, or None (fail-closed) if it is not a SAFE single
    segment (S1 — a bad slug DROPS the candidate, never opens a PR). Enforces
    ^[a-z0-9][a-z0-9-]{1,60}$: rejects dots, slashes, `..`, reserved names, and any non-ASCII
    (Unicode confusables). Because the tag is also allowlisted (S3), this is defense-in-depth."""
    t = (rule_tag or "").strip().lower()
    if not _SLUG_RE.match(t):       # also excludes "..", dotted, slashed, empty, over-long
        return None
    if t in _RESERVED:
        return None
    if not t.isascii():             # belt-and-suspenders vs confusables (regex is ASCII already)
        return None
    return t


def fingerprint(repo_scope: str, rule_tag: str) -> str:
    """Deterministic recurrence key for a (repo, rule_tag) CLASS (v0.4: keyed on the allowlisted
    tag, not the glob — Recon delta). NUL-separated so ('a','b-c') and ('a-b','c') can't collide."""
    return hashlib.sha256(f"{repo_scope}\x00{rule_tag}".encode()).hexdigest()


_HEX_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def normalize_sha(s: str) -> str:
    """The canonical commit_sha form (strip + lower). is_hex_sha validates THIS form, and the
    storage boundary (capturerecord.record) must store EXACTLY this form: the read side
    (capture_provenance's `commit_sha ~ '^[0-9a-f]{7,40}$'` — case-sensitive, true end-anchored)
    silently drops any noncanonical stored row, stranding the recurrence provenance the human
    needs (the draft renders finding_ids=n/a). Shared helper so validator and writer can't drift:
    is_hex_sha(s) is True ⟺ normalize_sha(s) fullmatches [0-9a-f]{7,40} ⟺ the stored form
    passes the SQL filter."""
    return (s or "").strip().lower()


def is_hex_sha(s: str) -> bool:
    """True iff `s` NORMALIZES (normalize_sha) to a plausible git commit SHA (7-40 lowercase hex).
    commit_sha reaches the rendered SKILL.md body + PR as provenance (finding_ids); the recorder
    validates on the way IN and provenance filters on the way OUT so an attacker-chosen non-hex
    string (attack-pass A3) can never carry injection payload into the human-reviewed draft.
    sanitize_field is the last belt.

    Uses fullmatch, NOT `...$`: Python's `$` also matches JUST BEFORE a terminal newline, so
    `^[0-9a-f]+$` would accept "deadbeef\\n" and let a trailing-newline injection through (kilabz
    cross-family review). fullmatch anchors the WHOLE string."""
    return bool(_HEX_SHA_RE.fullmatch(normalize_sha(s)))


# ---- secondary locality: a changed path -> the path_trigger a proposed skill would carry --------
def path_to_glob(path: str) -> str:
    """Normalize a changed file path to a path-glob: keep the directory, generalize the basename to
    `*.<ext>`. An extensionless/dotfile basename keeps its literal name. Returns "" for empty."""
    p = (path or "").strip().strip("/")
    if not p:
        return ""
    parts = p.split("/")
    base = parts[-1]
    dot = base.rfind(".")
    if dot > 0:
        parts[-1] = "*" + base[dot:]
    return "/".join(parts)


def candidate_glob(path: str) -> str | None:
    """The path-glob for `path` IF usable as a skill trigger (skillmatch wouldn't BAN it), else
    None. Fail-closed so auto-capture never proposes a trigger the promotion lint would reject.
    Rejects any control char / newline (a `git diff -z` filename CAN contain newlines, and a glob
    with a newline injected from a directory segment would forge extra SKILL.md frontmatter lines —
    cross-family review CRITICAL). The check runs on the RAW path FIRST (before path_to_glob's
    .strip() could normalize a leading/trailing control char away) and rejects tab too."""
    if not path or _RAW_CTRL.search(path):       # any C0/DEL incl. \t \n \r, on the raw input
        return None
    if any(ch.isspace() for ch in path):
        # whitespace is the trigger-ALTERNATIVES delimiter (#125 r2 #3): a space-bearing
        # glob would silently shatter into nonsense alternatives at match time. The
        # predicate mirrors the splitter EXACTLY — trigger_alternatives() uses str.split(),
        # which splits on ALL Unicode whitespace (NBSP, ideographic space, ...), so an
        # ASCII-only " " check would wave the non-ASCII shatter cases through (r3 #1).
        return None
    g = path_to_glob(path)
    if not g or _RAW_CTRL.search(g) or skillmatch.is_banned_trigger(g):
        return None
    return g


# ---- S3: multi-signal recurrence gate (pure; counts are computed in SQL, decided here) ----------
def recurrence_ready(distinct_commits: int, distinct_events: int, distinct_authors: int,
                     *, min_recur: int, min_events: int, min_authors: int) -> bool:
    """True iff a class has accrued ENOUGH independent signal to propose. ALL must hold (v0.4):
    distinct commit SHAs >= min_recur, distinct review/push events >= min_events, distinct authors
    >= min_authors. Cross-family agreement is enforced UPSTREAM (an occurrence is only recorded when
    BOTH families emitted the tag), so it is not re-checked here. A single bad push can satisfy none
    of commits/events on its own."""
    return (distinct_commits >= min_recur
            and distinct_events >= min_events
            and distinct_authors >= min_authors)


def reready_threshold(decline_count: int, *, min_recur: int, mult: int) -> int:
    """The commit threshold a DECLINED class must re-cross before it is proposed again (S8 anti-
    nag): min_recur * mult**decline_count. decline_count 0 -> min_recur (never-declined)."""
    return min_recur * (mult ** max(0, decline_count))


# ---- S1: path isolation — the proposal may touch ONLY skills/<slug>/SKILL.md -------------------
def skill_branch(s: str) -> str:
    return f"skill/auto/{s}"


def skill_path(s: str) -> str:
    return f"skills/{s}/SKILL.md"


def assert_only_skill_path(changed_paths: list[str], s: str) -> bool:
    """True iff the ONLY changed path is EXACTLY skills/<slug>/SKILL.md (S1 — used both before
    `gh pr create` and as the server-side check on any auto-proposed PR). Fail-closed: empty list,
    any extra path, any `..`/absolute/backslash, or a slug mismatch all return False."""
    if slug(s) != s:                      # an unsanitized slug invalidates the whole invariant
        return False
    want = skill_path(s)
    if not changed_paths or len(changed_paths) != 1:
        return False
    p = changed_paths[0]                   # NO strip: git reports the exact path; a trailing space
    if p != want:                         # is a DIFFERENT file that would dodge path-based gates
        return False
    # redundant hardening (want is already literal): no traversal / absolute / backslash slips in
    if ".." in p.split("/") or p.startswith("/") or "\\" in p:
        return False
    return True


# ---- S4: deterministic drafting — render from STRUCTURED fields, never raw reviewer text -------
_TAG_LIKE = re.compile(r"<[^>\n]{0,200}>")   # XML/HTML-ish tags where injection framing hides
_WS = re.compile(r"[ \t\r\f\v]+")


def sanitize_field(text: str, maxlen: int) -> str:
    """Make one structured field safe to embed in a SKILL.md body (S4): drop tag-like spans and
    control chars, collapse runs of whitespace, trim, and hard-cap length. This is a BEST-EFFORT
    filter, NOT the security boundary (the human merge is) — but it removes the obvious injection
    affordances before the body goes anywhere near a reviewer prompt as PR diff.

    Uses _RAW_CTRL (the STRICT [\\x00-\\x1f\\x7f], incl. \\n and \\t) — NOT a looser class: the
    attack pass found that letting a raw newline through here forges extra SKILL.md lines from a
    structured field (e.g. an injected finding_id / origin_repo), and _WS does not cover \\n. Map
    every C0/DEL to a space FIRST, then _WS collapses the run — a multi-line field becomes one line."""
    t = _TAG_LIKE.sub(" ", text or "")
    t = _RAW_CTRL.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return t[:maxlen].strip()


# Cross-family MAJOR (kilabz K4 + oracle): an un-authored auto-stub PASSES promotion lint, so if it
# were merged as-is the controller would INDEX it and inject a no-op "flag any change…" skill into
# every future review — actively degrading reviews (selection prefers unused skills + admits only a
# few, so a stub can DISPLACE real guidance). The render therefore stamps this MACHINE-DETECTABLE
# marker into the body of any draft whose problem-statement was not captured (every v1 draft). The
# promotion path (controller _index_skills) + CI REFUSE to index/merge a body that still carries it;
# authoring the real lesson deletes the marker line. A plain sentinel (no tags/paths) so it can't
# trip scan_injection and render still lints clean.
STUB_MARKER = "MDX-AUTOPROPOSED-STUB-UNAUTHORED"


def is_unauthored_stub(rendered: str) -> bool:
    """True iff `rendered` is an auto-proposed SKILL.md whose real lesson was never authored (the
    STUB_MARKER survives) — OR is empty/whitespace-only. The controller's index step + CI gate on
    this so a no-op stub can never reach the injected corpus even if a human merges it without
    filling it in.

    Degenerate content counts as unauthored because callers may use this function as their SOLE
    check (the CI skill-stub gate does): an empty or truncated-to-nothing blob must never pass as
    "authored" (oracle finding — the marker-in-content test alone is fail-open on such input). At
    the controller, lint_skill runs FIRST and already rejects empty blobs, so ordering and reject
    reasons there are unchanged; this guard is the belt for direct callers."""
    r = rendered or ""
    return (not r.strip()) or (STUB_MARKER in r)


def render_skill_md(s: str, rule_tag: str, path_trigger: str,
                    whats_wrong: str, preferred_pattern: str,
                    *, finding_ids: list[str], origin_repo: str) -> str | None:
    """Render ONE auto-proposed SKILL.md from a FIXED template over sanitized structured fields.
    Returns the markdown, or None (fail-closed) if the result would not pass the SAME
    skillmatch.lint_skill the controller runs at promotion — so auto-capture never opens a PR for a
    draft its own promotion gate would reject. NO raw reviewer comment text is pasted in.

    An un-authored draft (no captured whats_wrong — every v1 draft, since v1 does not capture the
    finding text) carries STUB_MARKER so the promotion/index gate refuses it until a human authors
    the body (cross-family K4)."""
    if slug(s) != s or not is_allowed_tag(rule_tag):
        return None
    if skillmatch.is_banned_trigger(path_trigger) or _RAW_CTRL.search(path_trigger):  # belt vs injection
        return None
    desc = sanitize_field(f"Recurring review finding: {rule_tag}", 60)
    wrong = sanitize_field(whats_wrong, 700)
    pref = sanitize_field(preferred_pattern, 700) or "(no preferred pattern captured)"
    ids = ", ".join(sanitize_field(i, 40) for i in (finding_ids or [])[:8]) or "n/a"
    origin = sanitize_field(origin_repo, 80) or "n/a"
    # a draft with no captured problem-statement is an unauthored STUB -> mark it (K4 gate).
    stub_line = ("" if wrong else
                 f"\n\n{STUB_MARKER}: replace the description + preferred-pattern with the real "
                 f"lesson and delete this line before merging (an unedited stub is refused at index).")
    body = (
        f"{wrong or '(no description captured)'}\n\n"
        f"Preferred pattern: {pref}\n\n"
        f"Flag any change matching `{path_trigger}` that repeats this class of issue.\n\n"
        f"(Auto-proposed from recurring reviewer findings [{rule_tag}]. "
        f"Provenance: origin_repo={origin}; finding_ids={ids}. "
        f"Drafted deterministically — review before merge.){stub_line}"
    )
    raw = (
        "---\n"
        f"name: {s}\n"
        f"description: {desc}\n"
        f"path_trigger: {path_trigger}\n"
        "---\n\n"
        f"{body}\n"
    )
    skill, reason = skillmatch.lint_skill(s, raw)
    if skill is None:
        return None
    return raw


def draft_hash(rendered: str) -> str:
    """sha256 of the rendered SKILL.md — pinned at CAS ready->proposing (S6) so a crash-retry can
    recognize an already-pushed branch/PR by content instead of opening a duplicate."""
    return hashlib.sha256(rendered.encode()).hexdigest()


# ---- instrumentation: parse reviewer-emitted `rule:<tag>` lines + cross-family agreement --------
_RULE_LINE = re.compile(r"(?mi)^[ \t]*rule:[ \t]*([a-z0-9][a-z0-9-]{1,60})[ \t\r]*$")


def parse_rule_tags(text: str) -> set[str]:
    """Extract the set of ALLOWLISTED rule_tags a reviewer emitted on their own `rule:<tag>` lines.
    Off-list or malformed tags are dropped (S3). A line must be EXACTLY `rule:<tag>` (optional
    surrounding spaces) so a tag mentioned mid-sentence in prose isn't mistaken for a signal."""
    return {m.group(1).lower() for m in _RULE_LINE.finditer(text or "")
            if is_allowed_tag(m.group(1))}


def agreed_tags(kilabz_text: str, oracle_text: str) -> list[str]:
    """The allowlisted rule_tags BOTH families emitted (cross-family agreement, S3) — the only tags
    that may advance recurrence. Sorted for determinism. If either review is missing/absent (e.g.
    oracle unavailable), the intersection is empty (fail-closed: no agreement possible)."""
    return sorted(parse_rule_tags(kilabz_text) & parse_rule_tags(oracle_text))


def pick_glob(paths: list[str]) -> str | None:
    """The single MOST-SPECIFIC usable path-glob across the changed files — the secondary locality
    hint a proposed skill carries as its path_trigger. None if no path yields a usable (non-banned)
    glob. Deterministic on ties (specificity desc, then the glob string)."""
    globs = {g for p in (paths or []) if (g := candidate_glob(p))}
    if not globs:
        return None
    return max(sorted(globs), key=skillmatch.specificity)
