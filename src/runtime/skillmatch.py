"""Pure, DB-free logic for the +learning rung's review-skill selection + safety scan.

Kept separate from postgres_store / skillselect / controller so the matching + injection
logic is unit-testable WITHOUT a database (mirrors automerge.classify_diff being a pure,
adversarially-tested core). Imported by:
  - runtime.ledger.postgres_store.select_skills  — path-segment match + specificity ordering
  - runtime.skillselect                          — inject-time injection tripwire
  - runtime.controller (indexer lint)            — banned-trigger + injection scan at promotion

DESIGN: docs/learning-rung-plan.md Step 2/3/5 + design v0.3 #6 (path-segment matching) and
v0.3.2 (the injection tripwire, borrowed from openclaw and NARROWED for our context).
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase

__all__ = ["is_banned_trigger", "seg_match", "specificity", "scan_injection", "INJECTION_PATTERNS",
           "parse_skill_md", "lint_skill"]


# ---- path-segment trigger matching (design v0.3 #6) ------------------------------------
# A path_trigger matches a changed path by SEGMENT, never fnmatch-across-"/": split both on
# "/", require EQUAL segment count, fnmatch each segment. So `src/*.py` matches `src/a.py`
# but NOT `src/sub/a.py` (plain fnmatch's "*" would otherwise cross "/" and over-match).
#
# A trigger may carry MULTIPLE whitespace-separated alternatives ("tools/*.sh substrate/*.sh")
# — equal-depth matching means one pattern covers exactly one directory depth, so a skill
# aimed at "shell scripts wherever they live" needs one alternative per real location
# (guardrails review #125 finding 4: `*.sh` fired on nothing — every script is nested — and
# the obvious `**/*.sh` is deliberately banned). Each alternative obeys the same ban rules.
# FORMAT CONSTRAINT (#125 r2 #3): whitespace is the delimiter, so an alternative CANNOT
# target a path containing a space — such a trigger silently shatters. This repo's tracked
# tree contains no space-bearing paths (pinned by a test), and the capture pipeline rejects
# space-bearing proposals at the source; if a spaced path ever becomes legitimate, the
# format needs a structured delimiter FIRST.

def trigger_alternatives(trigger: str) -> list[str]:
    """Split a path_trigger into its whitespace-separated alternatives (may be one)."""
    return trigger.split()


def is_banned_trigger(trigger: str) -> bool:
    """A trigger too broad to allow — it would attach to ~every review and starve specific
    skills under the LIMIT 2 selection (Oracle/codex). Banned: empty; any `**` segment
    (cross-segment wildcard has no place in segment matching); and any BARE `*` segment, so
    `*`, `*/*`, `dir/*`, `src/*` are all rejected — every segment must carry a literal. A
    pattern like `src/*.py` (segment `*.py`) is allowed. A multi-alternative trigger is
    banned if ANY alternative is banned (fail-closed — one broad alternative would starve
    exactly like a broad single trigger)."""
    alts = trigger_alternatives(trigger)
    if not alts:
        return True
    for alt in alts:
        segs = alt.split("/")
        if any(s == "*" or s == "**" for s in segs):
            return True
    return False


def seg_match(trigger: str, path: str) -> bool:
    """True iff ANY alternative of `trigger` matches `path` by path-SEGMENT (equal depth,
    per-segment case-sensitive fnmatch, `*` never crossing "/"). Caller should have
    rejected the trigger via is_banned_trigger() first."""
    psegs = path.strip().split("/")
    for alt in trigger_alternatives(trigger):
        tsegs = alt.split("/")
        if len(tsegs) == len(psegs) and all(fnmatchcase(p, t) for t, p in zip(tsegs, psegs)):
            return True
    return False


def _alt_specificity(alt: str) -> int:
    return sum(0 if any(c in s for c in "*?[") else 1 for s in alt.split("/"))


def specificity(trigger: str) -> int:
    """A trigger's specificity = count of segments with NO wildcard char (more literal
    segments = more specific). Used by capture's glob chooser (single-glob context, where
    max == the only alternative). RANKING candidates at select time uses
    match_specificity() instead — ranking by an alternative that did not match would let
    a broad skill inflate its LIMIT-2 rank with a decorative specific alternative
    (#125 r2 HIGH-1)."""
    alts = trigger_alternatives(trigger)
    if not alts:
        return 0
    return max(_alt_specificity(a) for a in alts)


def match_specificity(trigger: str, paths) -> int | None:
    """Specificity of the MOST specific alternative that ACTUALLY MATCHES any of `paths`
    — None when nothing matches. This is the select-time rank: a skill scores by what
    matched, never by an unrelated sibling alternative (#125 r2 HIGH-1: a trigger like
    '*.md decorative/very/specific.md' matching README.md via '*.md' must rank 0, not 3)."""
    psegs_list = [p.strip().split("/") for p in paths]
    best: int | None = None
    for alt in trigger_alternatives(trigger):
        tsegs = alt.split("/")
        for psegs in psegs_list:
            if len(tsegs) == len(psegs) and all(fnmatchcase(p, t) for t, p in zip(tsegs, psegs)):
                s = _alt_specificity(alt)
                if best is None or s > best:
                    best = s
                break
    return best


# ---- injection tripwire (design v0.3.2, from openclaw — NARROWED for our context) -------
# Deterministic, fail-closed scan for prompt-injection FRAMING in a skill body. Defense in
# depth ON TOP of the nonce-fence: the fence makes the body DATA; this drops an obviously
# adversarial body before it reaches a reviewer (skillselect) or before it is ever promoted
# to `active` (controller index lint). Mirrors openclaw's hasReviewerDirective tripwire.
#
# DELIBERATELY NARROW: a legitimate REVIEW skill is DESCRIPTIVE about code review — it will
# naturally say "flag any `curl ... | sh`", "reject if it auto-approves", "check env vars".
# So we do NOT scan for those content words (openclaw's SKILL_CONTENT_RULES do, because their
# learned skills describe TASKS, not reviews) — they would false-positive the very skills we
# want and make the rung unusable. We catch ONLY directives that try to RE-FRAME the reviewer
# (role-override / "ignore instructions" / system-prompt spoof / fence break), which a genuine
# descriptive review skill has no reason to contain. [Patterns are a security judgment call —
# scrutinize in review; tune toward fewer false-negatives only with evidence.]
INJECTION_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # Tight on purpose — only the unambiguous injection targets (instruction/prompt/system
    # message), NOT descriptive words a review skill legitimately uses ("ignore the lint
    # rule", "check env vars", "reject if it auto-approves" must all stay CLEAN).
    ("ignore-instructions", re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,20}\b(instruction|prompt|system\s+message)s?\b", re.I)),
    ("role-override", re.compile(
        r"\byou\s+are\s+(now|actually|really)\b|\bact\s+as\b(?![^.\n]{0,20}\bnormal)|\bnew\s+instructions?\s*:|\bfrom\s+now\s+on\b", re.I)),
    ("system-prompt-spoof", re.compile(
        r"\bsystem\s+prompt\b|</?(system|instructions?)>|^\s*(system|assistant)\s*:", re.I | re.M)),
    ("fence-break", re.compile(r"===\s*(BEGIN|END)\s+UNTRUSTED", re.I)),
)


def scan_injection(body: str) -> str | None:
    """Return the NAME of the first injection-framing pattern matching `body`, else None.
    Fail-closed callers DROP (skillselect) / QUARANTINE + alert (controller) on a non-None
    result — a skill body should never try to re-frame the reviewer."""
    for name, pat in INJECTION_PATTERNS:
        if pat.search(body):
            return name
    return None


# ---- SKILL.md parse + lint (controller index-time promotion gate, design v0.3 #5) -------
# Pure so the promotion lint is unit-testable without git/gh/DB (the controller does the I/O:
# read the blob from the trusted owned ref, then call lint_skill). v1 skills are DESCRIPTIVE
# TEXT ONLY: any executable-affordance frontmatter key is REJECTED (we never silently ignore a
# declared capability — that would be a footgun), as is an over-cap/empty field, a banned
# trigger, or an injection-framing body. The name is the DIRECTORY name (path-derived, not
# artifact-controlled), so a forged frontmatter `name:` cannot impersonate another skill.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DESC_MAX = 60        # mirrors the DB CHECK length(description) <= 60 (characters)
_BODY_MAX = 2048      # mirrors the DB CHECK length(body) <= 2048 (characters)
_AFFORDANCE_KEYS = frozenset({
    "allowed-tools", "allowed_tools", "tools", "scripts", "script", "support_files",
    "support-files", "exec", "command", "commands", "run", "shell", "code",
})


def parse_skill_md(raw: str) -> dict | None:
    """Split a SKILL.md into {"meta": {k: v}, "body": str}, or None if there is no well-formed
    `---` frontmatter block. DUMB key:value parsing on purpose — NOT yaml.load (no arbitrary
    object construction, no dependency). A frontmatter line with no `:` is malformed -> None."""
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    meta: dict[str, str] = {}
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ":" not in ln:
            return None
        k, _, v = ln.partition(":")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":   # strip matched surrounding quotes
            v = v[1:-1]
        meta[k.strip().lower()] = v
    body = "\n".join(lines[end + 1:]).strip()
    return {"meta": meta, "body": body}


def lint_skill(name: str, raw: str) -> tuple[dict | None, str]:
    """Pure parse + lint of ONE SKILL.md. `name` is the directory name (unforgeable identity).
    Returns (skill, "") ready for index_skills, or (None, reason) on a fail-closed rejection.
    The returned skill has name/description/body/path_trigger; the caller stamps the shas and
    the server stamps provenance='promoted' (never copied from the artifact)."""
    if not _SKILL_NAME_RE.match(name):
        return None, f"bad skill name {name!r} (need ^[a-z0-9][a-z0-9._-]*$)"
    parsed = parse_skill_md(raw)
    if parsed is None:
        return None, "missing or malformed --- frontmatter ---"
    meta, body = parsed["meta"], parsed["body"]
    bad = sorted(k for k in meta if k in _AFFORDANCE_KEYS)
    if bad:
        return None, f"executable-affordance keys not allowed (v1 is text-only): {bad}"
    if "name" in meta and meta["name"] != name:
        return None, f"frontmatter name {meta['name']!r} != directory name {name!r}"
    desc = meta.get("description", "")
    if not desc:
        return None, "description is required"
    if len(desc) > _DESC_MAX:
        return None, f"description over {_DESC_MAX} chars ({len(desc)})"
    trig = meta.get("path_trigger", "")
    if is_banned_trigger(trig):
        return None, f"path_trigger {trig!r} is empty/too-broad (banned)"
    if not body:
        return None, "empty body"
    if len(body) > _BODY_MAX:
        return None, f"body over {_BODY_MAX} chars ({len(body)})"
    inj = scan_injection(body)
    if inj:
        return None, f"injection-framing in body ({inj})"
    return {"name": name, "description": desc, "body": body, "path_trigger": trig}, ""
