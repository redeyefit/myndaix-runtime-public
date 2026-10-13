"""outcomerecord.py — outcomes-ledger INSTRUMENTATION (the per-finding OUTCOME LABEL wiring). It
wires PR-A's pure core (runtime.outcomes) + append-only verbs (record_findings / human_dismiss /
expire_open / outcome_stats) into three `mxr` entry points. Like capturerecord it NEVER opens a PR,
NEVER acts on the data (v1 COLLECTS ONLY), and is FAIL-OPEN throughout — the review pipeline runs
over UNTRUSTED diffs, so a hung or erroring outcome path can never delay a verdict, wedge the review
lock, or change a gate decision.

Three verbs (routed via `mxr`, so each inherits the runtime venv + PYTHONPATH + DSN):
  - `mxr outcome-record --list-tags`
        print the allowlisted taxonomy (one per line) for play-review to embed in the `finding:`
        prompt — SINGLE source of truth (shared with capture), so the prompt list can't drift.
  - `mxr outcome-record --kilabz <text> --oracle <text> -- <repo_path> <base> <tip> <ref> <play>
        [changed_path...]`
        for EACH family separately: parse `finding:<tag> @ <path>:<line>` lines, resolve+hash each
        against git OBJECTS at <tip> (validating the line falls inside a changed hunk from
        `git diff <base> <tip> -- <path>`), build the open_findings list, compute present_hashes per
        changed path, call led.record_findings (CLOSE + OPEN), sweep expiries, then print the
        recorded finding keys (short 12-hex) + family so play-review can surface them.
  - `mxr outcome <finding_key_prefix> fp|wontfix`
        the human dismissal (fail-CLOSED on an ambiguous / <12-hex prefix — prints colliding keys).
  - `mxr outcome-stats`
        print the finding_precision_raw rows + open count (human-readable) for the morning brain-check.

CONTRACT (mirrors capturerecord): default OFF ($ORCH/OUTCOMES_ENABLED gate is enforced in the
play-review WIRING, not here — this verb records whatever it's handed); HARD no-op in gate mode
(PLAY_GATE); FAIL-OPEN always. All diagnostics to stderr; the recorded-keys / stats / --list-tags
payloads go to stdout.

DESIGN: docs/outcomes-ledger-design.md (v0.3). The identity/parser/resolve math is in the pure core
(runtime.outcomes); the append-only state machine lives in the ledger verbs.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import os
import re
import subprocess
import sys
from pathlib import Path

from runtime import outcomes
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
ORCH = HOME / ".myndaix" / "orchestrator"

# a git SHA / ref-ish arg validated before it is ever handed to git (defense-in-depth; git is
# invoked argv-form so this is belt-and-suspenders, never the boundary). repo_path is checked as a
# real directory below. base/tip are the diff endpoints; ref/play are opaque labels stored as data.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
# `@@ -a,b +c,d @@` hunk header: capture the NEW-side start(,count) — the `+` side is the tip we key
# findings against. count defaults to 1 when the `,count` is absent (git omits it for a 1-line hunk).
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def log(msg: str) -> None:
    """Diagnostics ALWAYS to stderr (stdout is reserved for the recorded-keys / stats / --list-tags
    payload)."""
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [outcomerecord] {msg}", file=sys.stderr, flush=True)


def _gate_mode() -> bool:
    """A merge-gating run (docs-only automerge) never feeds outcomes — the gate must stay
    deterministic and there is no delivered verdict to attach keys to. Any non-empty PLAY_GATE
    hard-skips. Belt-and-suspenders: the play-review wiring already omits the call in gate mode."""
    return bool(os.environ.get("PLAY_GATE"))


def _ttl_days() -> int:
    """The open-finding TTL from $OUTCOME_TTL_DAYS, defaulting to 30 (design §2) — a recalibration
    is a config change, never code. Fail-open to the default on a malformed value."""
    raw = os.environ.get("OUTCOME_TTL_DAYS") or ""
    try:
        return int(raw) if raw else 30
    except ValueError:
        return 30


def _run_git(argv: list[str]) -> str | None:
    """The injected git callable runtime.outcomes needs: run `git <argv...>` READ-ONLY and return
    stdout, or None on any non-zero exit / missing object / error. argv-form (never a shell string),
    so an untrusted path/sha can't inject. Bounded by a wall-clock timeout so a wedged git can't hang
    the recorder (the outer cap_run in play-review is the real bound, but be defensive here too)."""
    try:
        # errors="replace": a binary/invalid-UTF-8 blob (git show on a non-text file) would else raise
        # UnicodeDecodeError, which (OSError, SubprocessError) does NOT catch — breaking the verb's own
        # "never raises" contract (kilabz). Tolerant decode: such a line just won't line-hash-match, safe.
        r = subprocess.run(["git", *argv], capture_output=True, text=True,
                           errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _changed_hunks(repo_path: str, base: str, tip: str, path: str) -> list[tuple[int, int]]:
    """The NEW-side changed hunks for `path` across base..tip: a list of (start, count) 1-indexed
    inclusive ranges parsed from `git diff <base> <tip> -- <path>` hunk headers. runtime.outcomes'
    resolve_and_hash drops any finding whose line isn't INSIDE one of these — so a wrong-but-resolvable
    line number can't silently key a finding to unrelated code (design §3). Reads git OBJECTS via
    the diff (no worktree). Empty list on a missing/failed diff -> every finding on that path drops
    (fail-closed). `--` fences the pathspec so a path starting with `-` isn't read as a flag."""
    out = _run_git(["-C", repo_path, "diff", base, tip, "--", path])
    if out is None:
        return []
    hunks: list[tuple[int, int]] = []
    for line in out.split("\n"):
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        hunks.append((start, count))
    return hunks


def _resolve_family(repo_path: str, base: str, tip: str, review_text: str, family: str) -> list[dict]:
    """Parse one family's review, resolve+hash every finding against git OBJECTS at `tip`, and return
    the open_findings dicts record_findings expects: {"tag","path","line_hash","reviewer_family"}.
    Per-family, exactly as the design's precision measurement requires. A finding whose line can't be
    resolved (path/object missing, line outside a changed hunk, empty line) is DROPPED (never raised).
    Hunks are computed ONCE per distinct path (a diff per path, not per finding)."""
    findings, dropped = outcomes.parse_finding_lines(review_text or "")
    if dropped:
        log(f"{family}: dropped {dropped} malformed/over-cap finding line(s)")
    hunks_cache: dict[str, list[tuple[int, int]]] = {}
    resolved: list[dict] = []
    for f in findings:
        path = f["path"]
        if path not in hunks_cache:
            hunks_cache[path] = _changed_hunks(repo_path, base, tip, path)
        lh = outcomes.resolve_and_hash(repo_path, tip, path, f["line"],
                                       hunks_cache[path], run_git=_run_git)
        if lh is None:                       # unresolvable (line ∉ hunk / missing / empty) -> drop
            continue
        resolved.append({"tag": f["tag"], "path": path, "line_hash": lh,
                         "reviewer_family": family})
    return resolved


async def record(repo_path: str, base: str, tip: str, ref: str, play: str,
                 changed_paths: list[str], kilabz: str, oracle: str) -> list[dict]:
    """Run the CLOSE + OPEN recorder for BOTH families, sweep expiries, and return the list of
    recorded (opened) finding keys for play-review to surface: [{"key12","family","tag","path"}].
    FAIL-OPEN: any error -> log + return [] (records nothing, never raises). The CLOSE phase fires on
    EVERY delivered review (incl. PLAY_PASS with empty open_findings), so an applied fix lands even on
    a clean follow-up."""
    try:
        led = await PostgresLedger.connect(DSN)
    except Exception as e:
        log(f"ledger connect failed ({e}) — fail-open (recorded nothing)")
        return []
    try:
        # per-family resolve (precision is per-family); merge for the single record_findings call.
        open_findings = (_resolve_family(repo_path, base, tip, kilabz, "kilabz")
                         + _resolve_family(repo_path, base, tip, oracle, "oracle"))
        # present_hashes for the CLOSE phase: the SET of line_hashes currently in each changed file at
        # tip, read from git OBJECTS (never the worktree). Three-state (core-audit HIGH): a set (empty =
        # CONFIRMED-deleted -> close §6; populated = close only the vanished lines) OR None = presence
        # could NOT be determined (transient git error) -> record_findings leaves the finding OPEN, so a
        # git blip can't fabricate an applied_fixed and poison the ground truth.
        present: dict[str, set[str] | None] = {}
        for path in changed_paths:
            present[path] = outcomes.file_line_hashes(repo_path, tip, path, run_git=_run_git)
        try:
            res = await led.record_findings(os.path.basename(repo_path.rstrip("/")) or repo_path,
                                            ref, tip, play, changed_paths, open_findings, present)
        except Exception as e:
            log(f"record_findings failed ({e}) — fail-open"); return []
        log(f"recorded: opened={res['opened']} closed={res['closed']} "
            f"skipped_dismissed={res['skipped_dismissed']}")
        # TTL sweep piggybacks the same invocation (cheap SQL; deterministic per-UTC-day source_event,
        # so a re-run is a no-op). Its own try so a sweep error can't lose the recorded keys.
        try:
            expired = await led.expire_open(_ttl_days())
            if expired:
                log(f"expired {expired} over-TTL open finding(s)")
        except Exception as e:
            log(f"expire_open failed ({e}) — skipped (fail-open)")
        # the keys to surface = ONLY the rows record_findings actually INSERTED this review (kilabz:
        # building from open_findings would surface a sticky-dismissed or duplicate finding that
        # opened NO new row -> a spurious *-outcomes.md follow-up). The ledger reports the real inserts.
        keys: list[dict] = []
        for r in res.get("opened_rows", []):
            keys.append({"key12": r["finding_key"][:12], "family": r["reviewer_family"],
                         "tag": r["rule_tag"], "path": r["path"]})
        return keys
    finally:
        try:
            await led.close()
        except Exception:
            pass


_LABEL_KINDS = ("real", "fp", "wontfix")
_LABEL_BATCH_CAP = 200          # fat-finger guard on one invocation, not a quota (design §2b)


async def label_batch(led, kind: str, keys: list) -> dict:
    """Label 1..N findings as `kind` (design §2a/§2b). D-1: ALL kinds route through
    confirm_outcome — the ONLY writer of the gating human rows — so `real` finally has a caller
    and fp/wontfix gain the any-state semantics (post-hoc truth on auto-closed/expired findings;
    queue membership untouched — human rows are terminal for it). PER-KEY fail-closed (an
    ambiguous/junk key refuses THAT key, prints candidates, the loop continues), BATCH fail-open.
    Duplicates within one invocation are deduped (first occurrence kept). Returns the four-count
    summary {labeled, rows, refused, duplicates} — `rows` = ledger rows actually inserted this
    call (0-2 per key via family expansion; idempotent re-issues insert 0)."""
    seen, uniq, duplicates = set(), [], 0
    for k in keys:
        kk = (k or "").strip().lower()
        if kk in seen:
            duplicates += 1
            continue
        seen.add(kk)
        uniq.append(kk)
    if len(uniq) > _LABEL_BATCH_CAP:
        print(f"refused: {len(uniq)} keys exceeds the {_LABEL_BATCH_CAP}/invocation cap — split the batch")
        return {"labeled": 0, "rows": 0, "refused": len(uniq), "duplicates": duplicates}
    labeled = rows = refused = 0
    verb = "confirmed" if kind == "real" else "dismissed"
    for key in uniq:
        try:
            res = await led.confirm_outcome(key, "all", kind, principal_role="admin")
        except Exception as e:                   # per-key fail-open: one bad key never kills the batch
            log(f"confirm_outcome failed for {key} ({e})")
            print(f"refused: {key} — {e}")
            refused += 1
            continue
        if "error" in res:
            print(f"refused: {key} — {res['error']}")
            for c in res.get("candidates", []):
                print(f"  colliding key: {c}")
            refused += 1
            continue
        labeled += 1
        rows += res["written"]
        print(f"{verb} {res['written']} row(s) as {kind} — finding {res['finding_key'][:12]} "
              f"(full: {res['finding_key']})")
    return {"labeled": labeled, "rows": rows, "refused": refused, "duplicates": duplicates}


async def _label_cli(kind: str, keys: list, batch: bool) -> int:
    """Connect + run label_batch; print the four-count summary in batch form. FAIL-OPEN on infra
    (returns 0 always — a refusal is operator-retry, not a caller error)."""
    try:
        led = await PostgresLedger.connect(DSN)
    except Exception as e:
        log(f"ledger connect failed ({e})")
        return 0
    try:
        s = await label_batch(led, kind, keys)
    except Exception as e:
        log(f"label_batch failed ({e})"); return 0
    finally:
        try:
            await led.close()
        except Exception:
            pass
    if batch:
        print(f"labeled {s['labeled']} ({s['rows']} rows), refused {s['refused']}, "
              f"duplicates {s['duplicates']}")
    return 0


async def stats() -> int:
    """`mxr outcome-stats` — human-readable precision rows + open count for the morning brain-check.
    Parser-drift starvation is VISIBLE when open_count + every row stays 0."""
    try:
        led = await PostgresLedger.connect(DSN)
    except Exception as e:
        log(f"ledger connect failed ({e})")
        return 0
    try:
        s = await led.outcome_stats()
    except Exception as e:
        log(f"outcome_stats failed ({e})"); return 0
    finally:
        try:
            await led.close()
        except Exception:
            pass
    rows = s.get("precision", [])
    print(f"open findings: {s.get('open_count', 0)}")
    if not rows:
        print("no labelled findings yet (precision rows empty — nothing recorded, or all still open)")
        return 0
    print(f"{'rule_tag':<26} {'family':<8} {'fixed':>5} {'fp':>4} {'vol':>4} {'precision':>9}")
    for r in rows:
        prec = r.get("precision")
        prec_s = "n/a" if prec is None else f"{float(prec):.3f}"
        print(f"{str(r.get('rule_tag','')):<26} {str(r.get('reviewer_family','')):<8} "
              f"{r.get('applied_fixed',0):>5} {r.get('dismissed_false_positive',0):>4} "
              f"{r.get('volume',0):>4} {prec_s:>9}")
    return 0


def main(argv: list) -> int:
    raw = argv[1:]

    # `outcome-record --list-tags` — prompt source-of-truth; stdout IS the payload (mirrors
    # capture-record --list-tags). Special-cased above the parser so it never touches the DB/gate.
    if raw and raw[0] == "--list-tags":
        sys.stdout.write("\n".join(sorted(outcomes.RULE_TAG_TAXONOMY)) + "\n")
        return 0

    # HARD no-op in gate mode (belt-and-suspenders — the wiring already skips): a merge-gating run
    # never feeds outcomes. Fail-open (exit 0), no DB touch.
    if _gate_mode():
        log("PLAY_GATE set — outcomes never learns from a merge-gating run; no-op")
        return 0

    p = argparse.ArgumentParser(prog="outcome-record", add_help=False)
    p.add_argument("repo_path")
    p.add_argument("base")
    p.add_argument("tip")
    p.add_argument("ref")
    p.add_argument("play")
    p.add_argument("--kilabz", default="")
    p.add_argument("--oracle", default="")
    p.add_argument("paths", nargs="*")
    try:
        a = p.parse_args(raw)
    except SystemExit:
        log("usage: outcome-record <repo_path> <base> <tip> <ref> <play> "
            "--kilabz <text> --oracle <text> -- <path>...   |   outcome-record --list-tags")
        return 0   # fail-open: a malformed call must never break the caller's review

    # validate the git-facing args fail-CLOSED (record NOTHING rather than key a finding at a bad
    # tip/base): both endpoints must be sha-ish, and repo_path must be a real directory.
    if not _SHA_RE.match(a.base) or not _SHA_RE.match(a.tip):
        log(f"non-sha base/tip ({a.base!r}/{a.tip!r}) — no-op (fail-closed)"); return 0
    if not os.path.isdir(a.repo_path):
        log(f"repo_path {a.repo_path!r} is not a directory — no-op"); return 0

    keys = asyncio.run(record(a.repo_path, a.base, a.tip, a.ref, a.play,
                              list(a.paths), a.kilabz, a.oracle))
    # the recorded-keys payload -> stdout, one per line, for play-review to build the follow-up file.
    # Format: "<key12>\t<family>\t<tag>\t<path>". No keys -> empty stdout (clean-PASS / all-dropped).
    for k in keys:
        sys.stdout.write(f"{k['key12']}\t{k['family']}\t{k['tag']}\t{k['path']}\n")
    return 0


def label_main(argv: list) -> int:
    """`mxr outcome` — the human's per-finding label (label-throughput PR-A). Two forms:
        outcome <key12> <kind>              (single, back-compat — kind gains `real`)
        outcome <kind> <key12> [<key12>…]   (kind-first batch)
    kind ∈ real|fp|wontfix; a 12-hex key can never equal a kind word, so the forms can't collide.
    Routed separately from outcome-record because it's a DIFFERENT verb shape (a human command,
    not the recorder). FAIL-OPEN on infra + usage; per-key fail-closed inside the batch. Gate mode
    does NOT apply — the human runs this manually, off the review path."""
    raw = argv[1:]
    if len(raw) >= 2 and raw[0] in _LABEL_KINDS:
        return asyncio.run(_label_cli(raw[0], raw[1:], batch=True))
    if len(raw) == 2 and raw[1] in _LABEL_KINDS:
        return asyncio.run(_label_cli(raw[1], [raw[0]], batch=False))
    log("usage: outcome <key12> real|fp|wontfix   |   outcome real|fp|wontfix <key12> [<key12>...]")
    return 0


dismiss_main = label_main        # back-compat alias (cli.py + tests migrated; belt for stragglers)


def stats_main(argv: list) -> int:
    """`mxr outcome-stats` — the read surface for the morning brain-check. No args. FAIL-OPEN."""
    return asyncio.run(stats())


def format_label_queue(rows: list) -> str:
    """Pure formatter for `mxr labelqueue` (design §2c): cluster by (rule_tag, reviewer_family),
    largest first; per cluster the count, first path (+N more), the latest raise's ref/play, and
    the paste-ready 12-hex keys (VERBATIM round-trip into the batch verb is the contract)."""
    if not rows:
        return "label queue is empty"
    clusters: dict = {}
    for r in rows:
        clusters.setdefault((r["rule_tag"], r["reviewer_family"]), []).append(r)
    out = [f"{len(rows)} finding(s) awaiting a human label — "
           f"label: mxr outcome real|fp|wontfix <key12> [<key12>...]"]
    for (tag, fam), items in sorted(clusters.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        paths = []
        for it in items:
            if it["path"] not in paths:
                paths.append(it["path"])
        extra = f" +{len(paths) - 1} more paths" if len(paths) > 1 else ""
        latest = max(items, key=lambda r: r["raise_seq"])
        play = latest["source_event"] or ""
        play = play[len("review:"):] if play.startswith("review:") else play
        out.append(f"{tag}  {fam}  {len(items)}  {paths[0]}{extra}  "
                   f"(latest raise: {latest['ref']} play {play})")
        out.append(f"  keys: {'  '.join(it['key12'] for it in items)}")
    return "\n".join(out)


async def _labelqueue() -> int:
    try:
        led = await PostgresLedger.connect(DSN)
    except Exception as e:
        log(f"ledger unreachable ({e})")
        return 2
    try:
        rows = await led.label_queue()
    except Exception as e:
        log(f"label_queue failed ({e})")
        return 2
    finally:
        try:
            await led.close()
        except Exception:
            pass
    print(format_label_queue(rows))
    return 0


def labelqueue_main(argv: list) -> int:
    """`mxr labelqueue` — the read-only evidence surface (design §2c). OPERATOR command tier:
    FAIL-CLOSED (exit 2) on an unreachable ledger — misconfiguration must not read as an empty
    queue. No args."""
    return asyncio.run(_labelqueue())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
