"""capturerecord.py — auto-capture INSTRUMENTATION (observe-only): record a NEEDS-FIX review's
cross-family-agreed `rule:<tag>` signals into the recurrence ledger. It NEVER opens a PR or
promotes anything — that is the (not-yet-built) proposer's job. This rung exists so the recurrence
signal accrues from REAL reviews before we build the proposer, so thresholds are calibrated to
reality, not guessed (Mack+Jefe, 2026-06-28).

Two modes (routed via `mxr capture-record`, so it inherits the runtime venv + PYTHONPATH + DSN):
  - `mxr capture-record --list-tags`
        print the allowlisted taxonomy (one per line) for play-review to embed in reviewer prompts
        — single source of truth, so the prompt list can't drift from the python allowlist.
  - `mxr capture-record <repo_id> <commit_sha> <event_id> <author> --kilabz <text> --oracle <text>
        -- <changed_path>...`
        record one occurrence per tag BOTH families emitted; log (never act on) any that just
        became `ready`.

CONTRACT (mirrors skillselect): default OFF ($ORCH/CAPTURE_ENABLED gate); HARD no-op in gate mode;
FAIL-OPEN always — instrumentation must NEVER break or change a review. All output to stderr except
--list-tags (whose stdout IS the payload).

DESIGN: docs/auto-capture-design.md (v0.4). The recurrence math + fail-closed gates live in the
pure core (runtime.capture) and the ledger verb (record_capture); this file is just the I/O glue.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import os
import re
import sys
from pathlib import Path

from runtime import capture
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
ORCH = HOME / ".myndaix" / "orchestrator"
ENABLED_FLAG = ORCH / "CAPTURE_ENABLED"

_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def log(msg: str) -> None:
    """Diagnostics ALWAYS to stderr (stdout is reserved for --list-tags payload)."""
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [capturerecord] {msg}", file=sys.stderr, flush=True)


def _gate_mode() -> bool:
    """A merge-gating run (docs-only automerge) never feeds recurrence — there's no skill class to
    learn from a docs PR, and the gate must stay deterministic. Any non-empty PLAY_GATE hard-skips."""
    return bool(os.environ.get("PLAY_GATE"))


def _th(name: str) -> int:
    """A capture threshold from $CAPTURE_<NAME>, falling back to the pure-core default (so a future
    cross-family review of the recalibration is a config change, never code)."""
    return int(os.environ.get(f"CAPTURE_{name}", capture.DEFAULTS[name]))


async def record(repo_id: str, commit_sha: str, event_id: str, author: str,
                 tags: list[str], path_glob: str | None) -> int:
    """Record each cross-family-agreed tag as one occurrence; log any that JUST became ready.
    OBSERVE-ONLY: a 'ready' class is logged, never proposed. Fail-OPEN on any error."""
    # storage boundary: is_hex_sha validates the NORMALIZED form (strip+lower); store exactly that
    # form or the strict read-side hex filter (capture_provenance ~ '^[0-9a-f]{7,40}$') silently
    # drops the row and the proposed draft renders finding_ids=n/a. Also keeps case/whitespace
    # variants of one commit deduping on the (fingerprint, commit_sha) PK instead of double-counting
    # toward recurrence. Must run before ANY ledger write so every stored occurrence is canonical.
    commit_sha = capture.normalize_sha(commit_sha)
    # belt for callers that skip main()'s validation (a backfill/hook calling record() directly):
    # a stored non-hex value counts toward recurrence (the count(*) has no hex filter) yet is
    # unmatchable at read time — same stranded-provenance failure one layer down. Fail-open no-op
    # per the instrumentation contract; the caller's review must never break on this.
    if not capture.is_hex_sha(commit_sha):
        log(f"non-hex commit_sha {commit_sha!r} after normalize — recorded nothing"); return 0
    try:
        led = await PostgresLedger.connect(DSN)
    except Exception as e:
        log(f"ledger connect failed ({e}) — fail-open (recorded nothing)"); return 0
    th = dict(min_recur=_th("MIN_RECUR"), min_events=_th("MIN_EVENTS"),
              min_authors=_th("MIN_AUTHORS"), repropose_mult=_th("REPROPOSE_MULT"))
    try:
        for tag in tags:
            try:
                ready = await led.record_capture(repo_id, tag, path_glob, commit_sha,
                                                 event_id, author, True, **th)
            except Exception as e:
                log(f"record_capture({tag!r}) failed ({e}) — skipped"); continue
            if ready:
                log(f"OBSERVE: tag {tag!r} reached READY (commits={ready['commits']} "
                    f"events={ready['events']} authors={ready['authors']}) — would propose "
                    f"(proposer not built; no PR opened)")
            else:
                log(f"recorded tag {tag!r} (repo={repo_id} glob={path_glob})")
        return 0
    finally:
        try:
            await led.close()
        except Exception:
            pass


def main(argv: list) -> int:
    raw = argv[1:]
    if raw and raw[0] == "--list-tags":   # prompt source-of-truth; stdout IS the payload
        sys.stdout.write("\n".join(sorted(capture.RULE_TAG_TAXONOMY)) + "\n")
        return 0

    p = argparse.ArgumentParser(prog="capture-record", add_help=False)
    p.add_argument("repo_id")
    p.add_argument("commit_sha")
    p.add_argument("event_id")
    p.add_argument("author")
    p.add_argument("--kilabz", default="")
    p.add_argument("--oracle", default="")
    p.add_argument("paths", nargs="*")
    try:
        a = p.parse_args(raw)
    except SystemExit:
        log("usage: capture-record <repo_id> <commit_sha> <event_id> <author> "
            "--kilabz <text> --oracle <text> -- <path>...   |   capture-record --list-tags")
        return 0   # fail-open: a malformed call must never break the caller's review

    # --- the no-op ladder (each rung fails OPEN; instrumentation never blocks a review) ---
    if _gate_mode():
        log("PLAY_GATE set — recurrence never learns from a merge-gating run; no-op"); return 0
    if not ENABLED_FLAG.exists():
        log("CAPTURE_ENABLED absent — instrumentation OFF; no-op"); return 0
    if not _REPO_ID_RE.match(a.repo_id) or ".." in a.repo_id:
        log(f"unsafe repo_id {a.repo_id!r} — no-op"); return 0
    # commit_sha reaches the proposer's rendered SKILL.md body as provenance (finding_ids); reject a
    # non-hex value on the way IN so a forged/injected sha can never carry a payload into a
    # human-reviewed draft (attack-pass A3, way-in belt; provenance hex-filters on the way out too).
    if not capture.is_hex_sha(a.commit_sha):
        log(f"non-hex commit_sha {a.commit_sha!r} — no-op"); return 0
    # never learn from a diff that touches the corpus itself: pick_glob collapses a mixed diff to
    # ONE glob, so the per-glob skills/ reject in record_capture can miss a mixed push (cross-family
    # MAJOR). Fail-closed on ANY skills/ path here, with the full path context.
    if any(p.startswith("skills/") for p in a.paths):
        log("a changed path is under skills/** — no self-capture; no-op"); return 0

    tags = capture.agreed_tags(a.kilabz, a.oracle)   # allowlisted ∩ both families (S3, fail-closed)
    if not tags:
        log("no cross-family-agreed allowlisted rule:<tag> — nothing to record"); return 0
    glob = capture.pick_glob(a.paths)
    return asyncio.run(record(a.repo_id, a.commit_sha, a.event_id, a.author, tags, glob))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
