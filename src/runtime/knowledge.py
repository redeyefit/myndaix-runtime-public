"""knowledge.py — the curator rung's PURE core (no DB, no LLM, no subprocess): corpus walk +
parse + canonical-path policy for ingest, and the deterministic validation grammar the curate
guard uses at promote time. Design: docs/curator-design.md v0.4.

Files on disk are the SOURCE OF TRUTH; the knowledge_doc table is a derived, rebuildable index.
Everything here is deliberately deterministic and unit-testable without Postgres — the I/O verbs
live in knowledgerecord.py, the guard in curate.py.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# ---- scope -> root: a STATIC allowlist (design: never derive a path from a scope string) -------
# Extra scopes come from $MYNDAIX_KNOWLEDGE_SCOPES ("name=/abs/path,name2=/abs/path2") — an
# operator/deploy decision, never caller input. Unknown scope = hard error at every verb
# (fail-closed: misconfiguration must never read as "no knowledge").
_ENV_SCOPES = "MYNDAIX_KNOWLEDGE_SCOPES"


def known_scopes() -> dict[str, Path]:
    scopes = {"research": Path(os.environ.get("HOME", str(Path.home()))) / "research"}
    for entry in os.environ.get(_ENV_SCOPES, "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, root = entry.partition("=")
        name, root = name.strip(), root.strip()
        # the scope NAME is interpolated into lock keys/labels — keep it path-safe; the ROOT must
        # be absolute (a relative root would silently depend on the caller's cwd).
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name) and root.startswith("/"):
            scopes[name] = Path(root)
    return scopes


def resolve_scope(scope: str) -> Path:
    """The scope's corpus root, or ValueError (HARD error — all verbs fail closed on it)."""
    roots = known_scopes()
    if scope not in roots:
        raise ValueError(f"unknown scope {scope!r} (known: {', '.join(sorted(roots))})")
    return roots[scope]


# ---- corpus walk (ingest + stage-in share this file-set definition) ----------------------------
# Directory names pruned at any depth; secrets-bearing session logs (.playwright-mcp) and
# machine noise never reach the index OR the staged workspace (the read boundary IS the copy).
NOISE_DIRS = {"__pycache__", ".playwright-mcp", ".claude", ".git", "node_modules"}
_NOISE_DIR_PREFIXES = (".venv",)          # .venv, .venv-higgsfield, ...

BODY_CAP_BYTES = 900_000                  # tsvector hard limit is 1MB; cap with margin
TRUNCATION_MARKER = "\n\n[truncated for index — full file on disk]\n"

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_FM_DATE_RE = re.compile(r"^date:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_FM_TAGS_RE = re.compile(r"^tags:\s*(.+)$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_CTRL_IN_NAME_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class DocRecord:
    path: str                 # relative, NFC-normalized
    title: str
    tags: str
    doc_date: str | None      # ISO YYYY-MM-DD or None
    body: str
    content_sha: str
    lossy: bool


@dataclass
class WalkResult:
    docs: list[DocRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # every eligible file (md or not) — the stage-in MANIFEST + "un-indexed artifact" lint input
    artifacts: list[str] = field(default_factory=list)


def _is_noise_dir(name: str) -> bool:
    return name in NOISE_DIRS or name.startswith(_NOISE_DIR_PREFIXES)


def _eligible_file(root: Path, p: Path, warnings: list[str]) -> bool:
    """The shared file gate for ingest AND stage-in (fail-closed on anything odd):
    regular, single-linked, non-hidden, sane name, really inside root."""
    name = p.name
    if name.startswith("."):
        return False
    if _CTRL_IN_NAME_RE.search(name):
        warnings.append(f"skipped {name!r}: control chars in filename")
        return False
    try:
        st = p.lstat()
    except OSError as e:
        warnings.append(f"skipped {name!r}: lstat failed ({e})")
        return False
    if p.is_symlink() or not p.is_file():
        warnings.append(f"skipped {name!r}: not a regular file")
        return False
    if st.st_nlink > 1:      # a hardlink under an allowed name can alias content outside the corpus
        warnings.append(f"skipped {name!r}: hardlinked (st_nlink={st.st_nlink})")
        return False
    try:                     # traversal belt: the resolved path must stay inside the resolved root
        p.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        warnings.append(f"skipped {name!r}: escapes the corpus root")
        return False
    return True


def parse_doc(rel_path: str, raw: bytes) -> DocRecord:
    """Deterministic parse of one markdown file. Never raises on content."""
    lossy = False
    body = raw.decode("utf-8", errors="replace")
    if "�" in body and b"\xef\xbf\xbd" not in raw:   # replacement chars we introduced
        lossy = True
    if "\x00" in body:
        body = body.replace("\x00", "")
        lossy = True
    if len(body.encode("utf-8", errors="replace")) > BODY_CAP_BYTES:
        body = body.encode("utf-8", errors="replace")[:BODY_CAP_BYTES] \
                   .decode("utf-8", errors="ignore") + TRUNCATION_MARKER
        lossy = True

    m = _HEADING_RE.search(body)
    title = (m.group(1).strip() if m else Path(rel_path).stem)[:300]
    tm = _FM_TAGS_RE.search(body[:2000])
    tags = re.sub(r"[\[\]'\"#]", " ", tm.group(1)).strip()[:300] if tm else ""

    fname_date = _DATE_PREFIX_RE.match(Path(rel_path).name)
    fm_date = _FM_DATE_RE.search(body[:2000])
    doc_date = fname_date.group(1) if fname_date else (fm_date.group(1) if fm_date else None)

    return DocRecord(path=rel_path, title=title, tags=tags, doc_date=doc_date, body=body,
                     content_sha=hashlib.sha256(raw).hexdigest(), lossy=lossy)


def date_disagreement(rel_path: str, body: str) -> tuple[str, str] | None:
    """(filename_date, frontmatter_date) when BOTH exist and differ — filename wins, ingest WARNs
    (citation dates are trust-bearing; silent precedence hides stale copies)."""
    f = _DATE_PREFIX_RE.match(Path(rel_path).name)
    fm = _FM_DATE_RE.search(body[:2000])
    if f and fm and f.group(1) != fm.group(1):
        return (f.group(1), fm.group(1))
    return None


def walk_corpus(root: Path) -> WalkResult:
    """Walk a corpus root: parse every eligible *.md, list every eligible artifact. Deterministic
    order; case-insensitive duplicate basenames WARN (APFS is case-insensitive — first wins)."""
    res = WalkResult()
    if not root.is_dir():
        raise ValueError(f"corpus root {root} is not a directory")
    seen_ci: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_noise_dir(d) and not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if not _eligible_file(root, p, res.warnings):
                continue
            rel = unicodedata.normalize("NFC", str(p.relative_to(root)))
            ci = rel.lower()
            if ci in seen_ci:
                res.warnings.append(f"case-insensitive duplicate: {rel!r} vs {seen_ci[ci]!r} — first wins")
                continue
            seen_ci[ci] = rel
            res.artifacts.append(rel)
            if not name.lower().endswith(".md"):
                continue
            try:
                raw = p.read_bytes()
            except OSError as e:
                res.warnings.append(f"skipped {rel!r}: read failed ({e})")
                continue
            doc = parse_doc(rel, raw)
            dd = date_disagreement(rel, doc.body)
            if dd:
                res.warnings.append(f"{rel}: filename date {dd[0]} != frontmatter date {dd[1]} — filename wins")
            res.docs.append(doc)
    return res


# ---- promote-side validation grammar (the guard's deterministic rules) -------------------------
# New curator files: top-level, .md, conservative charset, no dot-segments, no hidden files.
NEW_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$")
PROMOTE_FILE_CAP_BYTES = 256_000

# [[name]] / [[name|label]] / [[name#section]]: existence checked on `name` vs .md basenames,
# case-insensitive, NFC, extension optional, #section ignored (design v0.4 grammar).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n|#]+)(?:#[^\[\]\n|]*)?(?:\|[^\[\]\n]*)?\]\]")

# Secret patterns: a GUARDRAIL, not a proof (design v0.4 — the stage-in filter is the real
# boundary; this catches the obvious classes in agent-authored output).
_SECRET_RES = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),            # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),          # Slack
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),                 # OpenAI-style
    re.compile(r"\beyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
]
# Fence-lookalike regions in a promoted file could forge the prompt-fencing protocol downstream.
_FENCE_LOOKALIKE_RE = re.compile(r"===(?:BEGIN|END)[^\n]*?===")


def valid_new_filename(name: str) -> bool:
    return bool(NEW_FILE_RE.fullmatch(unicodedata.normalize("NFC", name))) and ".." not in name


def wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)]


def link_resolves(target: str, md_basenames: set[str]) -> bool:
    """md_basenames: lowercase NFC basenames WITHOUT extension."""
    t = unicodedata.normalize("NFC", target).strip().lower()
    t = t[:-3] if t.endswith(".md") else t
    return t in md_basenames


def content_violations(name: str, data: bytes, md_basenames: set[str]) -> list[str]:
    """Deterministic content checks for ONE promoted file. Empty list = clean."""
    out: list[str] = []
    if len(data) > PROMOTE_FILE_CAP_BYTES:
        out.append(f"{name}: exceeds {PROMOTE_FILE_CAP_BYTES}B promote cap")
        return out
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{name}: not valid UTF-8"]
    if "\x00" in text:
        return [f"{name}: contains NUL"]
    for pat in _SECRET_RES:
        if pat.search(text):
            out.append(f"{name}: secret-pattern match ({pat.pattern[:30]}…)")
    if _FENCE_LOOKALIKE_RE.search(text):
        out.append(f"{name}: nonce-fence-lookalike region")
    for target in wikilinks(text):
        if not link_resolves(target, md_basenames):
            out.append(f"{name}: ghost wikilink [[{target}]]")
    return out


def index_violations(index_text: str, md_files: list[str]) -> list[str]:
    """index.md structural validation: non-empty, every corpus .md mentioned (completeness — a
    0-byte/gutted 'edit' fails here), every wikilink resolves. Non-md artifacts are free text."""
    out: list[str] = []
    if len(index_text.strip()) < 20:
        return ["index.md: empty/gutted (fails completeness)"]
    low = unicodedata.normalize("NFC", index_text).lower()
    for f in md_files:
        base = Path(f).name.lower()
        if base != "index.md" and base not in low:
            out.append(f"index.md: missing entry for {f}")
    bases = {Path(f).name[:-3].lower() for f in md_files}
    for target in wikilinks(index_text):
        if not link_resolves(target, bases):
            out.append(f"index.md: ghost wikilink [[{target}]]")
    return out


# ---- recall query helpers (the ladder's deterministic pieces) -----------------------------------
QUERY_CAP_CHARS = 512
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")


def prefix_tokens(query: str) -> list[str]:
    """Sanitized tokens for the to_tsquery('tok:* & …') rung. Empty list = skip the rung
    (all-stopword/punctuation-only queries must not error)."""
    return [t.lower() for t in _TOKEN_RE.findall(query[:QUERY_CAP_CHARS])][:8]


def ilike_pattern(query: str) -> str:
    """%-wrapped ILIKE pattern with %/_/\\ escaped (wildcards in the query are literal)."""
    q = query[:QUERY_CAP_CHARS].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


# ---- deterministic index skeleton (the design's v2 "computed index"; a base the curator enriches)
_LIST_MARKER_RE = re.compile(r"^(?:[-*+>]\s|\d+[.)]\s)")     # bullet/ordered/quote, trailing space
_MD_INLINE_UNSAFE = re.compile(r"[<>\[\]|]")                 # neutralize link/HTML/table breakers
_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")              # code-fence opener (char + length)
_LINE_SCAN_CAP = 2000                                       # bound per-line analysis (body is ≤900KB)
# metadata-label line (`**Date:** ...`) — pseudo-frontmatter these briefs open with; a useless
# hook. WHITELIST of known preamble keys ONLY (kilabz re-review: a broad `Label:` skip would erase
# real prose openers like `Conclusion:`/`Risk:`/`Thesis:`). Optional bold wrap; keeps real prose.
_META_LABEL_RE = re.compile(
    r"^\*{0,2}(?:Date|Status|Author|Updated|Source|Sources|Trigger|Tags|Owner|Related|"
    r"Created|Last updated|Repo|Branch|Run|Version):\*{0,2}(?:\s|$)", re.IGNORECASE)


def _md_oneline(text: str, cap: int = 140) -> str:
    """Sanitize an untrusted corpus/filename string for one-line markdown emission (kilabz+oracle):
    collapse ALL whitespace (incl. newlines) to single spaces, neutralize inline markdown that
    could break the list/wikilink/table or render as HTML (`< > [ ] |`), then cap. Cheap and
    deterministic — the output is read by a human/curator, never executed."""
    t = re.sub(r"\s+", " ", (text or "")[:cap * 3]).strip()
    return _MD_INLINE_UNSAFE.sub(" ", t)[:cap].strip()


def _first_prose_line(body: str) -> str:
    """First substantive line after the title/frontmatter — the deterministic one-line hook the
    curator later replaces with a judged summary. Skips: a LEADING `---` frontmatter block (only
    when it opens the file — a later `---` is a horizontal rule, not frontmatter: kilabz+oracle
    MAJOR); fenced code blocks (```/~~~); ATX headings; bullet/ordered/quote list markers (trailing
    space required so `-10`/`**bold**` prose is NOT skipped: oracle MINOR); table/setext rules."""
    in_fm = False
    fence: str | None = None                 # the open fence marker (chars), or None
    for i, raw in enumerate(body.splitlines()):
        line = raw.strip()[:_LINE_SCAN_CAP]  # bound analysis; the hook is capped again in _md_oneline
        if not line:
            continue
        if line == "---":
            if i == 0:                       # opens frontmatter ONLY at the very top
                in_fm = True
            elif in_fm:                      # closes it
                in_fm = False
            continue                         # a mid-doc `---` (horizontal rule) is just skipped
        if in_fm:
            continue
        fm = _FENCE_OPEN_RE.match(line)      # code-fence: track char+length (kilabz re-review)
        if fm:
            marker = fm.group(1)
            if fence is None:
                fence = marker               # open
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None                 # close only on same char, >= opener length
            continue
        if fence is not None:                # inside a fence -> not a hook
            continue
        if (line.startswith(("#", "===", "|")) or _LIST_MARKER_RE.match(line)
                or set(line) <= {"-", "="} or _META_LABEL_RE.match(line)):
            continue                         # heading / table-row / list / setext-hr / metadata
        hook = _md_oneline(line)
        if hook:
            return hook
    return ""


def build_index_md(walk: WalkResult, *, title: str = "Research Corpus Index") -> str:
    """Pure: render a deterministic map-of-content from a corpus walk. Docs grouped by YYYY-MM
    (newest first), then an Undated group, then a non-md Assets list. Wikilinks use the .md
    basename (matches the curator's link grammar). This is a SKELETON — the curator enriches each
    hook with judgment and re-groups by topic; it is explicitly NOT a substitute for that."""
    groups: dict[str, list[DocRecord]] = {}
    undated: list[DocRecord] = []
    for d in walk.docs:
        if d.doc_date:
            groups.setdefault(d.doc_date[:7], []).append(d)
        else:
            undated.append(d)
    def _link(d: DocRecord) -> str:
        return _md_oneline(Path(d.path).stem, 120)          # strips [ ] < > | from the stem

    # duplicate-stem warning (kilabz): wikilinks resolve by basename, so two docs sharing a stem
    # (a/foo.md + b/foo.md) produce an ambiguous [[foo]]. Key on the SANITIZED/emitted stem (kilabz
    # re-review — raw stems could differ while the emitted links collide). v1 corpus is flat so this
    # shouldn't fire; warn rather than silently emit a colliding link.
    stems: dict[str, str] = {}
    for d in walk.docs:
        s = _link(d).lower()
        if s in stems and stems[s] != d.path:
            walk.warnings.append(f"duplicate stem {s!r}: {d.path} vs {stems[s]} — [[{s}]] is ambiguous")
        stems[s] = d.path

    def _hook(d: DocRecord) -> str:
        return _first_prose_line(d.body) or _md_oneline(d.title)

    out = [f"# {_md_oneline(title, 120)}", "",
           "_Auto-generated skeleton (`mxr knowledge-index`). One-line hooks are the first prose "
           "line of each brief — the curator replaces them with judged summaries and regroups by "
           "topic._", ""]
    # newest date first WITHIN a month via (doc_date, path) desc — not path-only, so a
    # frontmatter-dated file with a non-date filename still sorts chronologically (kilabz MINOR).
    for ym in sorted(groups, reverse=True):
        out.append(f"## {ym}")
        for d in sorted(groups[ym], key=lambda x: (x.doc_date or "", x.path), reverse=True):
            out.append(f"- [[{_link(d)}]] ({d.doc_date}) — {_hook(d)}")
        out.append("")
    if undated:
        out.append("## Undated")
        for d in sorted(undated, key=lambda x: x.path):
            out.append(f"- [[{_link(d)}]] — {_hook(d)}")
        out.append("")
    assets = [a for a in walk.artifacts if not a.lower().endswith(".md")]
    if assets:
        out.append("## Assets (not full-text indexed)")
        for a in sorted(assets):
            out.append(f"- {_md_oneline(a, 200)}")           # strip newlines/brackets from names
        out.append("")
    if not walk.docs and not assets:                         # deterministic empty state (kilabz NIT)
        out.append("_No markdown briefs found._")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
