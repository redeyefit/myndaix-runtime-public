"""skillselect.py — the +learning rung's READ path: pick <=2 review-skill hints for a diff
and emit them as nonce-fenced UNTRUSTED reference regions on stdout, for play-review to
staple under the OBJECTIVE (Step 4).

THE contract (DESIGN v0.3 governing sections + build plan Step 3):
  - FAIL-OPEN to EMPTY stdout, always. A missing/blocked/erroring hint must NEVER change a
    verdict — the reviewer judges the diff; a skill is only optional reference guidance.
  - HARD no-op in GATE mode (PLAY_GATE set): a skill is NEVER injected into a merge-gating
    review (v0.3 #2). Double-guarded with play-review's own `! gate` skip (Step 4).
  - OFF unless $ORCH/SKILLS_ENABLED exists (Step 6); per-repo fail-closed via
    $ORCH/state/skills-blocked-<repo_id> (written by the controller when branch protection
    drops/unreadable, Step 5).
  - stdout carries ONLY fenced regions. ALL diagnostics go to stderr — stdout is the prompt
    payload, so a stray log line would corrupt the reviewer's fence contract.

Invoked by play-review.sh (Step 4):
    PLAY_NONCE=<run-nonce> [PLAY_ID=<play>] MYNDAIX_DSN=... \
        python3 -m runtime.skillselect <repo_id> <changed-path>...
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import os
import re
import sys
import uuid
from pathlib import Path

from runtime import skillmatch
from runtime.ledger.postgres_store import PostgresLedger

# -- config (mirror automerge.py constants) ------------------------------------
DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
ORCH = HOME / ".myndaix" / "orchestrator"
STATE = ORCH / "state"
ENABLED_FLAG = ORCH / "SKILLS_ENABLED"
JEFE_INBOX = HOME / ".myndaix" / "bridge" / "inbox" / "jefe"

# <=2 is enforced in select_skills (LIMIT 2); this is the belt total-injected-byte ceiling.
SKILL_INJECT_MAX_BYTES = int(os.environ.get("MYNDAIX_SKILL_INJECT_MAX_BYTES", "6144"))  # ~6 KiB

# repo_id is interpolated into the block-flag filename -> a path-traversal surface. Conservative
# charset; a bad id fails OPEN (emit nothing) so we never inject when block status is unknowable.
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# play-review.sh:101 clean() reproduced EXACTLY: delete C0 (0x00-08,0B,0C,0E-1F) + DEL (0x7F),
# keep \t \n \r. The emitted region MUST be byte-identical to bash `fence "armed-skill"` for the
# same nonce+body (Step 7 coupling test) or the reviewer's "region ends ONLY at ===END..." breaks.
_C0_DEL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def log(msg: str) -> None:
    """Diagnostics ALWAYS to stderr — stdout is the fenced prompt payload (Step 4 discards stderr)."""
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [skillselect] {msg}", file=sys.stderr, flush=True)


def _clean(text: str) -> str:
    return _C0_DEL.sub("", text)


def _fence(label: str, body: str, nonce: str) -> str:
    """Reproduce play-review.sh:126-130 fence() byte-for-byte for label/body/nonce."""
    return (f"===BEGIN UNTRUSTED {label} nonce={nonce}===\n"
            + _clean(body)
            + f"\n===END UNTRUSTED nonce={nonce}===\n")


def _gate_mode() -> bool:
    """v0.3 #2: a skill is NEVER injected into a merge-gating review. Any non-empty PLAY_GATE
    hard-skips — strictly safer than matching only '1': if the gate flag is set at all, stay
    silent. (play-review already wraps the call in `! gate`, so this is belt, not the only guard.)"""
    return bool(os.environ.get("PLAY_GATE"))


def _alert_jefe(repo_id: str, drift: list[str], injected: list[str]) -> None:
    """LOUD, best-effort, atomic alert on an anomaly that should be impossible if the pipeline
    is intact: sha-drift (body != stored body_sha — tampered/half-written row) or injection-
    framing (scan_injection hit — a lint escape, or patterns tightened since promotion). Never
    raises — accounting must not break a review. Never a silent no-route (design v0.3 #6)."""
    if not drift and not injected:
        return
    try:
        JEFE_INBOX.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
        tok = uuid.uuid4().hex[:8]            # NOT just a 1-second ts — concurrent same-repo-second
        body = (                              # reviews would else os.replace to the SAME path and
            "---\n"                           # silently destroy one alert (oracle MAJOR).
            "from: skillselect\nto: jefe\ntype: alert\n"
            f"subject: review-skill anomaly DROPPED ({repo_id})\n"
            "---\n\n"
            "# review-skill DROPPED at inject time\n\n"
            f"repo: `{repo_id}`\n\n"
            f"- sha-drift (body != stored body_sha — tampered/half-written): {drift or 'none'}\n"
            f"- injection-framing (scan_injection hit — lint escape / tightened patterns): {injected or 'none'}\n\n"
            "These were NOT injected (fail-closed out of selection). A genuine review skill should\n"
            "never trip either check. Re-open the skill PR to re-promote a clean body, or revert it.\n"
        )
        tmp = JEFE_INBOX / f"{ts}-{tok}-skilldrift.md.tmp"
        final = JEFE_INBOX / f"{ts}-{tok}-skilldrift.md"
        tmp.write_text(body)
        os.replace(tmp, final)   # atomic publish; the daemon skips the brief .tmp
    except OSError as e:
        log(f"jefe alert write failed ({e}) — continuing fail-open")


async def select(repo_id: str, changed_paths: list[str], nonce: str) -> int:
    """Connect, select, fence-emit (<=2, byte-capped), account, alert. Fail-OPEN to empty."""
    try:
        led = await PostgresLedger.connect(DSN)
    except Exception as e:                       # DB down NEVER blocks a review (fail-open, logged)
        log(f"ledger connect failed ({e}) — fail-open empty"); return 0
    try:
        res = await led.select_skills(repo_id, changed_paths)
        regions: list[str] = []
        emitted: list[dict] = []
        dropped_inj: list[str] = []
        total = 0
        for s in res.get("skills", []):
            name, body = s["name"], s["body"]
            if nonce in body:                    # fence-breakout attempt -> drop
                log(f"skill {name!r}: run nonce present in body — dropped"); continue
            hit = skillmatch.scan_injection(body)
            if hit:                              # injection-framing tripwire -> drop + alert jefe
                log(f"skill {name!r}: injection pattern {hit!r} — dropped"); dropped_inj.append(name); continue
            region = _fence("armed-skill", body, nonce)
            rb = len(region.encode())
            if total + rb > SKILL_INJECT_MAX_BYTES:   # belt byte ceiling -> stop (emit what fits)
                log(f"skill {name!r}: would exceed {SKILL_INJECT_MAX_BYTES}B inject ceiling — stopping"); break
            regions.append(region); total += rb
            emitted.append({"name": name, "body_sha": hashlib.sha256(body.encode()).hexdigest()})
        if regions:
            sys.stdout.write("".join(regions)); sys.stdout.flush()
        if emitted:                              # best-effort, debounced accounting — never surfaces
            try:
                await led.record_skill_use(repo_id, os.environ.get("PLAY_ID", ""), emitted)
            except Exception as e:
                log(f"record_skill_use failed ({e}) — ignored")
        _alert_jefe(repo_id, res.get("drift", []), dropped_inj)
        return 0
    except Exception as e:                       # any selection error -> fail-open (logged, not silent)
        log(f"selection failed ({e}) — fail-open"); return 0
    finally:
        try:
            await led.close()
        except Exception:
            pass


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: python -m runtime.skillselect <repo_id> <changed-path>...", file=sys.stderr)
        return 2
    repo_id = argv[1]
    changed_paths = [p for p in argv[2:] if p]

    # --- the no-op ladder (each rung fails OPEN to empty stdout) ---
    if _gate_mode():
        log("PLAY_GATE set — never inject into a merge-gating review; no-op"); return 0
    if not ENABLED_FLAG.exists():
        log("SKILLS_ENABLED absent — selection OFF; no-op"); return 0
    if not _REPO_ID_RE.match(repo_id) or ".." in repo_id:
        log(f"unsafe repo_id {repo_id!r} — fail-open empty"); return 0
    if (STATE / f"skills-blocked-{repo_id}").exists():
        log(f"{repo_id}: skills-blocked flag present (branch protection) — no-op"); return 0
    nonce = os.environ.get("PLAY_NONCE", "")
    if not nonce:
        log("PLAY_NONCE absent — cannot fence safely; fail-open empty"); return 0
    if not changed_paths:
        return 0                                 # nothing changed to match
    return asyncio.run(select(repo_id, changed_paths, nonce))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
